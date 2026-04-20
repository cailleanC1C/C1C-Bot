"""Fusion reminder engine for pre-start and start notifications."""

from __future__ import annotations

import datetime as dt
import logging
import os

import discord
from discord.ext import commands

from modules.community.fusion.announcements import ensure_fusion_announcement
from modules.community.fusion import logs as fusion_logs
from modules.community.fusion.opt_in_view import build_fusion_opt_in_view
from shared.sheets import fusion as fusion_sheets

log = logging.getLogger("c1c.community.fusion.reminders")

_PRESTART_HOURS = max(1, int(os.getenv("FUSION_PRESTART_REMINDER_HOURS", "6")))
_LOOKBACK_MINUTES = max(5, int(os.getenv("FUSION_REMINDER_LOOKBACK_MIN", "30")))


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
) -> discord.Embed:
    jump_link = f"[Open Fusion Overview]({jump_url})"
    if reminder_type == "start":
        title = "Fusion Reminder"
        description = (
            f"⚠️ **{event_name} is live**\n"
            "Time to put in some work — fragments won’t collect themselves.\n\n"
            f"🔗 {jump_link}"
        )
    else:
        title = f"⏳ {event_name} starts soon"
        description = f"Starts in {_PRESTART_HOURS}h. Plan accordingly."

    embed = discord.Embed(
        title=title,
        description=description,
        color=discord.Color.blurple(),
        timestamp=start_at,
    )
    if reminder_type != "start":
        embed.add_field(name="Fusion", value=jump_link, inline=False)
    return embed


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

    try:
        sent_keys = await fusion_sheets.get_sent_reminder_keys(target.fusion_id)
    except Exception as exc:
        log.exception(
            "fusion reminder failed to load durable dedupe state; continuing fail-open "
            "(requires tab from FUSION_REMINDER_TAB with headers fusion_id, event_id, "
            "reminder_type; details=%s)",
            exc,
            extra={"fusion_id": target.fusion_id},
        )
        await fusion_logs.send_ops_alert(
            component="reminders",
            summary="durable_dedupe_unavailable_fail_open",
            dedupe_key=f"fusion:reminders:dedupe:{target.fusion_id}",
            error=exc,
            fields={"fusion_id": target.fusion_id},
        )
        sent_keys = set()

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

    for event in events:
        timing = fusion_sheets.get_valid_event_timing(event, for_helper="fusion_reminders")
        if timing is None:
            continue
        start_at, _ = timing

        triggers: list[tuple[str, dt.datetime]] = [
            ("prestart_6h", start_at - dt.timedelta(hours=_PRESTART_HOURS)),
            ("start", start_at),
        ]

        for reminder_type, trigger_at in triggers:
            key = (event.event_id, reminder_type)
            if key in sent_keys:
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
                )
                mention_content = f"<@&{target.opt_in_role_id}>" if target.opt_in_role_id else None
                reminder_view = build_fusion_opt_in_view(target)
                await announcement_message.channel.send(content=mention_content, embed=embed, view=reminder_view)
                await fusion_sheets.mark_reminder_sent(
                    target.fusion_id,
                    event_id=event.event_id,
                    reminder_type=reminder_type,
                    sent_at=reference,
                )
                sent_keys.add(key)
            except Exception as exc:
                context = {
                    "fusion_id": target.fusion_id,
                    "event_id": event.event_id,
                    "reminder_type": reminder_type,
                }
                log.exception(
                    "fusion reminder send failed",
                    extra=context,
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
