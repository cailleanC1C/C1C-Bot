"""Fusion reminder engine for pre-start and start notifications."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import time
import asyncio

import discord
from discord.ext import commands

from modules.community.fusion.announcements import ensure_fusion_announcement
from modules.community.fusion import logs as fusion_logs
from modules.community.fusion.opt_in_view import build_fusion_opt_in_view
from shared.sheets import fusion as fusion_sheets

log = logging.getLogger("c1c.community.fusion.reminders")

_LOOKBACK_MINUTES = max(5, int(os.getenv("FUSION_REMINDER_LOOKBACK_MIN", "30")))
_DEDUP_TIMEOUT_BACKOFF_SEC = max(60, int(os.getenv("FUSION_REMINDER_DEDUPE_BACKOFF_SEC", "300")))
_DEDUP_TIMEOUT_SEC = max(1.0, float(os.getenv("FUSION_REMINDER_DEDUPE_TIMEOUT_SEC", "10")))

_DEDUP_BACKOFF_UNTIL_MONOTONIC: float = 0.0
_FALLBACK_SENT_KEYS: set[tuple[str, str, str]] = set()
_DEDUP_DEGRADED_SINCE_MONOTONIC: float = 0.0
_DEDUP_DEGRADED_ALERTED_KEYS: set[tuple[str, str]] = set()
_DEDUP_DEGRADED_ALERT_AFTER_SEC = 600.0


def _utc_now(now: dt.datetime | None = None) -> dt.datetime:
    if now is None:
        return dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(dt.timezone.utc)


def _within_window(*, trigger_at: dt.datetime, now: dt.datetime, lookback: dt.timedelta) -> bool:
    return trigger_at <= now <= (trigger_at + lookback)


def _build_reminder_embed(
    *,
    event_name: str,
    reminder_type: str,
    start_at: dt.datetime,
    jump_url: str,
    reward_unit: str,
) -> discord.Embed:
    jump_link = f"[Open Fusion Overview]({jump_url})"
    prestart_hours = max(1, int(os.getenv("FUSION_PRESTART_REMINDER_HOURS", "6")))
    if reminder_type == "start":
        title = "Fusion Reminder"
        description = (
            f"⚠️ **{event_name} is live**\n"
            f"Time to put in some work — {reward_unit} won’t collect themselves.\n\n"
            f"🔗 {jump_link}"
        )
    else:
        title = f"⏳ {event_name} starts soon"
        description = f"Starts in {prestart_hours}h. Plan accordingly."

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.blurple(),
        timestamp=start_at,
    )
    if reminder_type != "start":
        embed.add_field(name="Fusion", value=jump_link, inline=False)
    return embed


def _build_grouped_embed(
    *,
    jump_url: str,
    live_events: list[fusion_sheets.FusionEventRow],
    starting_soon_events: list[fusion_sheets.FusionEventRow],
    upcoming_events: list[fusion_sheets.FusionEventRow],
    ending_events: list[fusion_sheets.FusionEventRow],
) -> discord.Embed:
    embed = discord.Embed(
        title="Fusion Reminder",
        description=f"🔗 [Open Fusion Overview]({jump_url})",
        color=discord.Color.blurple(),
    )
    if live_events:
        embed.add_field(
            name="Live now",
            value="\n".join(f"• {event.event_name}" for event in live_events[:10]),
            inline=False,
        )
    if starting_soon_events:
        embed.add_field(
            name="Starting soon",
            value="\n".join(f"• {event.event_name}" for event in starting_soon_events[:10]),
            inline=False,
        )
    if upcoming_events:
        embed.add_field(
            name="Upcoming / planning",
            value="\n".join(f"• {event.event_name}" for event in upcoming_events[:10]),
            inline=False,
        )
    if ending_events:
        embed.add_field(
            name="Ending soon",
            value="\n".join(f"• {event.event_name}" for event in ending_events[:10]),
            inline=False,
        )
    return embed


def _grouped_event_bucket_key(
    *,
    live_events: list[fusion_sheets.FusionEventRow],
    starting_soon_events: list[fusion_sheets.FusionEventRow],
    ending_events: list[fusion_sheets.FusionEventRow],
) -> str:
    tokens: list[str] = []
    tokens.extend(f"live:{event.event_id}" for event in sorted(live_events, key=lambda e: e.event_id))
    tokens.extend(f"starting_soon:{event.event_id}" for event in sorted(starting_soon_events, key=lambda e: e.event_id))
    tokens.extend(f"ending:{event.event_id}" for event in sorted(ending_events, key=lambda e: e.event_id))
    digest = hashlib.sha1("|".join(tokens).encode("utf-8")).hexdigest()[:12] if tokens else "empty"
    return f"grouped:{digest}"


async def process_fusion_reminders(
    bot: commands.Bot,
    *,
    now: dt.datetime | None = None,
) -> None:
    is_closed = getattr(bot, "is_closed", None)
    is_ready = getattr(bot, "is_ready", None)
    if callable(is_closed) and is_closed():
        return
    if callable(is_ready) and not is_ready():
        return

    reference = _utc_now(now)
    lookback = dt.timedelta(minutes=_LOOKBACK_MINUTES)

    try:
        target = await fusion_sheets.get_publishable_fusion()
    except Exception as exc:
        log.exception("fusion reminder failed to load target fusion")
        await fusion_logs.send_ops_alert(
            component="reminders",
            summary="load_target_fusion_failed",
            dedupe_key="fusion:reminders:load_target",
            error=exc,
        )
        return

    if target is None:
        return

    dedupe_meta = fusion_sheets.reminder_dedupe_backend_metadata()
    dedupe_backend = dedupe_meta.get("backend_type", "unknown")
    dedupe_tab = dedupe_meta.get("tab_name", "")
    dedupe_config_key = dedupe_meta.get("config_key", "")
    now_monotonic = time.monotonic()
    durable_dedupe_available = now_monotonic >= _DEDUP_BACKOFF_UNTIL_MONOTONIC
    sent_keys: set[tuple[str, str]] = set()
    if durable_dedupe_available:
        try:
            sent_keys = await asyncio.wait_for(
                fusion_sheets.get_sent_reminder_keys(target.fusion_id),
                timeout=_DEDUP_TIMEOUT_SEC,
            )
            _recover_from_dedupe_backoff()
        except TimeoutError as exc:
            _register_dedupe_timeout_backoff()
            durable_dedupe_available = False
            context = {
                "fusion_id": target.fusion_id,
                "timeout_sec": _DEDUP_TIMEOUT_SEC,
                "retry_backoff_sec": _DEDUP_TIMEOUT_BACKOFF_SEC,
                "dedupe_backend": dedupe_backend,
                "dedupe_tab": dedupe_tab,
                "dedupe_config_key": dedupe_config_key,
                "operation": "read_sent_reminder_keys",
            }
            log.exception(
                "fusion reminder durable dedupe timed out; using in-memory single-send fallback",
                extra=context,
                exc_info=True,
            )
            await fusion_logs.send_ops_alert(
                component="reminders",
                summary="durable_dedupe_unavailable_degraded_mode",
                dedupe_key=f"fusion:reminders:dedupe:{target.fusion_id}",
                error=exc,
                fields=context,
            )
        except Exception as exc:
            _register_dedupe_degraded_mode()
            log.exception(
                "fusion reminder failed to load durable dedupe state; using in-memory single-send fallback",
                extra={
                    "fusion_id": target.fusion_id,
                    "dedupe_backend": dedupe_backend,
                    "dedupe_tab": dedupe_tab,
                    "dedupe_config_key": dedupe_config_key,
                    "operation": "read_sent_reminder_keys",
                },
                exc_info=True,
            )
            await fusion_logs.send_ops_alert(
                component="reminders",
                summary="durable_dedupe_unavailable_degraded_mode",
                dedupe_key=f"fusion:reminders:dedupe:{target.fusion_id}",
                error=exc,
                fields={
                    "fusion_id": target.fusion_id,
                    "dedupe_backend": dedupe_backend,
                    "dedupe_tab": dedupe_tab,
                    "dedupe_config_key": dedupe_config_key,
                    "operation": "read_sent_reminder_keys",
                },
            )
            durable_dedupe_available = False
    else:
        _register_dedupe_degraded_mode()
        log.warning(
            "fusion reminder durable dedupe in backoff window; using in-memory single-send fallback",
            extra={
                "fusion_id": target.fusion_id,
                "retry_backoff_sec": _DEDUP_TIMEOUT_BACKOFF_SEC,
                "dedupe_backend": dedupe_backend,
                "dedupe_tab": dedupe_tab,
                "dedupe_config_key": dedupe_config_key,
                "operation": "read_sent_reminder_keys",
            },
        )

    try:
        events = await fusion_sheets.get_fusion_events(target.fusion_id)
    except Exception as exc:
        context = {"fusion_id": target.fusion_id}
        log.exception("fusion reminder failed to load events", extra=context)
        await fusion_logs.send_ops_alert(
            component="reminders",
            summary="load_events_failed",
            dedupe_key=f"fusion:reminders:events:{target.fusion_id}",
            error=exc,
            fields=context,
        )
        return

    settings = await fusion_sheets.get_fusion_reminder_settings()
    if settings.group_events:
        live_events: list[fusion_sheets.FusionEventRow] = []
        starting_soon_events: list[fusion_sheets.FusionEventRow] = []
        upcoming_events: list[fusion_sheets.FusionEventRow] = []
        ending_events: list[fusion_sheets.FusionEventRow] = []
        for event in events:
            timing = fusion_sheets.get_valid_event_timing(event, for_helper="fusion_reminders")
            if timing is None:
                continue
            start_at, end_at = timing
            if settings.include_start_events and start_at <= reference and (end_at is None or reference < end_at):
                live_events.append(event)
            if settings.include_upcoming_events:
                start_due_at = start_at - dt.timedelta(minutes=settings.start_offset_minutes)
                upcoming_horizon = reference + dt.timedelta(days=settings.upcoming_window_days)
                if start_due_at <= reference < start_at:
                    starting_soon_events.append(event)
                elif reference < start_at <= upcoming_horizon:
                    upcoming_events.append(event)
            if settings.include_ending_events and end_at is not None:
                if reference <= end_at <= reference + dt.timedelta(hours=settings.end_lookahead_hours):
                    ending_events.append(event)
        due_trigger_events = bool(live_events or starting_soon_events or ending_events)
        if due_trigger_events:
            reminder_type = "grouped"
            group_event_key = _grouped_event_bucket_key(
                live_events=live_events,
                starting_soon_events=starting_soon_events,
                ending_events=ending_events,
            )
            group_key = (group_event_key, reminder_type)
            if group_key not in sent_keys:
                announcement_message = await ensure_fusion_announcement(bot, target)
                if announcement_message is not None:
                    embed = _build_grouped_embed(
                        jump_url=announcement_message.jump_url,
                        live_events=live_events,
                        starting_soon_events=starting_soon_events,
                        upcoming_events=upcoming_events,
                        ending_events=ending_events,
                    )
                    mention_content = f"<@&{target.opt_in_role_id}>" if target.opt_in_role_id else None
                    await announcement_message.channel.send(
                        content=mention_content,
                        embed=embed,
                        view=build_fusion_opt_in_view(target),
                    )
                    if durable_dedupe_available:
                        await fusion_sheets.mark_reminder_sent(
                            target.fusion_id,
                            event_id=group_event_key,
                            reminder_type=reminder_type,
                            sent_at=reference,
                        )
                    sent_keys.add(group_key)
        return

    for event in events:
        timing = fusion_sheets.get_valid_event_timing(event, for_helper="fusion_reminders")
        if timing is None:
            continue
        start_at, _ = timing
        prestart_hours = max(1, int(os.getenv("FUSION_PRESTART_REMINDER_HOURS", "6")))

        triggers: list[tuple[str, dt.datetime]] = [
            ("prestart_6h", start_at - dt.timedelta(hours=prestart_hours)),
            ("start", start_at),
        ]

        for reminder_type, trigger_at in triggers:
            key = (event.event_id, reminder_type)
            if key in sent_keys:
                continue
            memory_key = (target.fusion_id, event.event_id, reminder_type)
            await _maybe_alert_prolonged_dedupe_degradation(
                target.fusion_id,
                reminder_type=reminder_type,
                backend=dedupe_backend,
            )
            if memory_key in _FALLBACK_SENT_KEYS:
                continue
            if not _within_window(trigger_at=trigger_at, now=reference, lookback=lookback):
                continue

            try:
                announcement_message = await ensure_fusion_announcement(bot, target)
                if announcement_message is None:
                    context = {
                        "fusion_id": target.fusion_id,
                        "event_id": event.event_id,
                        "reminder_type": reminder_type,
                    }
                    log.warning(
                        "fusion reminder skipped; announcement unavailable",
                        extra=context,
                    )
                    continue

                embed = _build_reminder_embed(
                    event_name=event.event_name,
                    reminder_type=reminder_type,
                    start_at=start_at,
                    jump_url=announcement_message.jump_url,
                    reward_unit=(str(target.reward_type or "").strip() or "rewards"),
                )
                mention_content = f"<@&{target.opt_in_role_id}>" if target.opt_in_role_id else None
                reminder_view = build_fusion_opt_in_view(target)
                await announcement_message.channel.send(content=mention_content, embed=embed, view=reminder_view)
                if durable_dedupe_available:
                    await fusion_sheets.mark_reminder_sent(
                        target.fusion_id,
                        event_id=event.event_id,
                        reminder_type=reminder_type,
                        sent_at=reference,
                    )
                sent_keys.add(key)
                _FALLBACK_SENT_KEYS.add(memory_key)
            except Exception as exc:
                context = {
                    "fusion_id": target.fusion_id,
                    "event_id": event.event_id,
                    "reminder_type": reminder_type,
                    "channel_id": getattr(getattr(announcement_message, "channel", None), "id", None)
                    if "announcement_message" in locals()
                    else None,
                    "thread_id": getattr(getattr(announcement_message, "channel", None), "id", None)
                    if "announcement_message" in locals()
                    and isinstance(getattr(announcement_message, "channel", None), discord.Thread)
                    else None,
                }
                log.exception(
                    "fusion reminder send failed",
                    extra=context,
                    exc_info=True,
                )
                await fusion_logs.send_ops_alert(
                    component="reminders",
                    summary="send_failed",
                    dedupe_key=(
                        f"fusion:reminders:send:{target.fusion_id}:{event.event_id}:{reminder_type}"
                    ),
                    error=exc,
                    fields=context,
                )


__all__ = ["process_fusion_reminders"]


def _register_dedupe_timeout_backoff() -> None:
    global _DEDUP_BACKOFF_UNTIL_MONOTONIC
    _DEDUP_BACKOFF_UNTIL_MONOTONIC = time.monotonic() + _DEDUP_TIMEOUT_BACKOFF_SEC
    _register_dedupe_degraded_mode()


def _register_dedupe_degraded_mode() -> None:
    global _DEDUP_DEGRADED_SINCE_MONOTONIC
    if _DEDUP_DEGRADED_SINCE_MONOTONIC <= 0:
        _DEDUP_DEGRADED_SINCE_MONOTONIC = time.monotonic()


def _recover_from_dedupe_backoff() -> None:
    global _DEDUP_BACKOFF_UNTIL_MONOTONIC, _DEDUP_DEGRADED_SINCE_MONOTONIC
    if _DEDUP_BACKOFF_UNTIL_MONOTONIC <= 0:
        return
    _DEDUP_BACKOFF_UNTIL_MONOTONIC = 0.0
    _DEDUP_DEGRADED_SINCE_MONOTONIC = 0.0
    _DEDUP_DEGRADED_ALERTED_KEYS.clear()


async def _maybe_alert_prolonged_dedupe_degradation(
    fusion_id: str,
    *,
    reminder_type: str,
    backend: str,
) -> None:
    if _DEDUP_DEGRADED_SINCE_MONOTONIC <= 0:
        return
    duration_sec = time.monotonic() - _DEDUP_DEGRADED_SINCE_MONOTONIC
    if duration_sec < _DEDUP_DEGRADED_ALERT_AFTER_SEC:
        return
    alert_key = (fusion_id, reminder_type)
    if alert_key in _DEDUP_DEGRADED_ALERTED_KEYS:
        return
    _DEDUP_DEGRADED_ALERTED_KEYS.add(alert_key)
    await fusion_logs.send_ops_alert(
        component="reminders",
        summary="durable_dedupe_unavailable_degraded_mode",
        dedupe_key=f"fusion:reminders:dedupe_degraded:{fusion_id}:{reminder_type}",
        fields={
            "fusion_id": fusion_id,
            "reminder_type": reminder_type,
            "backend": backend,
            "degraded_duration_sec": round(duration_sec, 1),
        },
    )
