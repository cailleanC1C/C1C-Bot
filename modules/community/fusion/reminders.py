"""Fusion grouped daily reminder engine."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import time

import discord
from discord.ext import commands

from modules.community.fusion import logs as fusion_logs
from modules.community.fusion.announcements import ensure_fusion_announcement
from modules.community.fusion.opt_in_view import build_fusion_opt_in_view
from shared.sheets import fusion as fusion_sheets

log = logging.getLogger("c1c.community.fusion.reminders")

_DEDUP_TIMEOUT_BACKOFF_SEC = max(60, int(os.getenv("FUSION_REMINDER_DEDUPE_BACKOFF_SEC", "300")))
_DEDUP_TIMEOUT_SEC = max(1.0, float(os.getenv("FUSION_REMINDER_DEDUPE_TIMEOUT_SEC", "10")))
_GROUPED_REMINDER_TYPE = "grouped_daily"
_GROUPED_EVENT_ID_PREFIX = "grouped_daily"

_DEDUP_BACKOFF_UNTIL_MONOTONIC: float = 0.0
_MEMORY_SENT_KEYS: set[tuple[str, str, str]] = set()
_DEDUP_DEGRADED_SINCE_MONOTONIC: float = 0.0
_DEDUP_DEGRADED_ALERTED_KEYS: set[tuple[str, str]] = set()
_DEDUP_DEGRADED_ALERT_AFTER_SEC = 600.0


def _utc_now(now: dt.datetime | None = None) -> dt.datetime:
    if now is None:
        return dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(dt.timezone.utc)


def _interpolate_template(template: str, values: dict[str, str]) -> str:
    text = str(template or "")
    for key, value in values.items():
        text = text.replace("{" + key + "}", value)
    return text


def _build_grouped_embed(
    *,
    settings: fusion_sheets.FusionReminderSettings,
    fusion_title: str,
    jump_url: str,
    live_events: list[fusion_sheets.FusionEventRow],
    upcoming_events: list[fusion_sheets.FusionEventRow],
    ending_events: list[fusion_sheets.FusionEventRow],
) -> discord.Embed:
    jump_link = f"[{settings.grouped_jump_label}]({jump_url})"
    placeholders = {
        "fusion_title": fusion_title,
        "jump_url": jump_url,
        "jump_link": jump_link,
        "live_count": str(len(live_events)),
        "upcoming_count": str(len(upcoming_events)),
        "ending_count": str(len(ending_events)),
    }
    embed = discord.Embed(
        title=_interpolate_template(settings.grouped_embed_title, placeholders),
        description=_interpolate_template(settings.grouped_embed_description, placeholders),
        color=discord.Color.blurple(),
    )

    def _section(events: list[fusion_sheets.FusionEventRow], label: str) -> None:
        value = "\n".join(f"• {event.event_name}" for event in events[:10]) if events else settings.grouped_empty_value
        embed.add_field(
            name=_interpolate_template(label, placeholders),
            value=_interpolate_template(value, placeholders),
            inline=False,
        )

    _section(live_events, settings.grouped_live_label)
    _section(upcoming_events, settings.grouped_upcoming_label)
    _section(ending_events, settings.grouped_ending_label)
    return embed


def _format_setting_value(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.10g}"
    text = str(value or "")
    if len(text) > 80:
        text = text[:77] + "..."
    return repr(text)


def _grouped_settings_diagnostics(settings: fusion_sheets.FusionReminderSettings) -> dict[str, object]:
    raw_values: dict[str, object] = {}
    raw_value_types: dict[str, str] = {}
    for key in sorted(settings.settings_raw_values):
        value = settings.settings_raw_values[key]
        raw_name = settings.settings_raw_key_names.get(key, key)
        raw_values[raw_name] = _format_setting_value(value)
        raw_value_types[raw_name] = settings.settings_raw_types.get(key, type(value).__name__)
    return {
        "settings_sheet_id_tail": settings.settings_sheet_id_tail or "missing",
        "settings_source_tab": settings.settings_source_tab or "missing",
        "settings_headers": tuple(settings.settings_headers),
        "settings_resolved_headers": {
            "key": settings.settings_key_header or "missing",
            "value": settings.settings_value_header or "missing",
        },
        "settings_cache": settings.settings_cache_status or "unknown",
        "raw_grouped_reminder_settings": raw_values or "empty",
        "raw_value_types": raw_value_types or "empty",
    }


def _format_bool_status(value: object) -> str:
    if value is None:
        return "n/a"
    return "yes" if bool(value) else "no"


def _configured_local_post_time(settings: fusion_sheets.FusionReminderSettings) -> str:
    raw_value = settings.settings_raw_values.get("grouped_daily_post_time")
    text = str(raw_value or "").strip()
    if not text:
        return "missing"
    timezone = str(fusion_sheets.cfg.get("TIMEZONE") or "Europe/Vienna").strip() or "Europe/Vienna"
    return f"{text} {timezone}"


def _missing_grouped_copy_fields(settings: fusion_sheets.FusionReminderSettings) -> list[str]:
    required = {
        "grouped_embed_title": settings.grouped_embed_title,
        "grouped_embed_description": settings.grouped_embed_description,
        "grouped_live_label": settings.grouped_live_label,
        "grouped_upcoming_label": settings.grouped_upcoming_label,
        "grouped_ending_label": settings.grouped_ending_label,
        "grouped_empty_value": settings.grouped_empty_value,
        "grouped_jump_label": settings.grouped_jump_label,
    }
    return [name for name, value in required.items() if not str(value or "").strip()]



def _group_events_config_fields(settings: fusion_sheets.FusionReminderSettings) -> dict[str, object]:
    source = settings.group_events_source
    fields: dict[str, object] = {
        "group_events_resolved": "true" if settings.group_events else "false",
        "group_events_raw_value": source.raw_value or "missing",
        "group_events_source_tab": source.tab_name or "missing",
        "group_events_key_header": source.key_header or "missing",
        "group_events_value_header": source.value_header or "missing",
    }
    if source.duplicate_count:
        fields["group_events_duplicate_count"] = source.duplicate_count
    return fields


def _parse_grouped_post_time_utc(raw: object) -> dt.time | None:
    text = str(raw or "").strip()
    if not text:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            parsed = dt.datetime.strptime(text, fmt).time()
            return parsed.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
    return None


def _grouped_event_id_for_date(day: dt.date) -> str:
    return f"{_GROUPED_EVENT_ID_PREFIX}:{day.isoformat()}"


def _format_due(value: dt.datetime | None) -> str:
    if value is None:
        return "none"
    return value.astimezone(dt.timezone.utc).replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M UTC")


def _format_sent(value: dt.datetime | None) -> str:
    return _format_due(value)


def _grouped_due_for_day(day: dt.date, post_time: dt.time) -> dt.datetime:
    return dt.datetime.combine(day, post_time, tzinfo=dt.timezone.utc)


def _next_grouped_due(
    *,
    now: dt.datetime,
    post_time: dt.time,
    sent_keys: set[tuple[str, str]],
) -> dt.datetime:
    today_key = (_grouped_event_id_for_date(now.date()), _GROUPED_REMINDER_TYPE)
    today_due = _grouped_due_for_day(now.date(), post_time)
    if today_key not in sent_keys and now <= today_due:
        return today_due
    if today_key not in sent_keys and now > today_due:
        return now
    tomorrow = now.date() + dt.timedelta(days=1)
    return _grouped_due_for_day(tomorrow, post_time)


def _is_grouped_due(
    *,
    now: dt.datetime,
    post_time: dt.time,
    sent_keys: set[tuple[str, str]],
) -> bool:
    today_key = (_grouped_event_id_for_date(now.date()), _GROUPED_REMINDER_TYPE)
    if today_key in sent_keys:
        return False
    return now >= _grouped_due_for_day(now.date(), post_time)


def _append_skip(skipped: dict[str, int], reason: str) -> None:
    skipped[reason] = skipped.get(reason, 0) + 1


def _render_skips(skipped: dict[str, int]) -> str:
    if not skipped:
        return "none"
    return ", ".join(f"{reason}={count}" for reason, count in sorted(skipped.items()))


def _select_grouped_events(
    *,
    settings: fusion_sheets.FusionReminderSettings,
    events: list[fusion_sheets.FusionEventRow],
    reference: dt.datetime,
) -> tuple[list[fusion_sheets.FusionEventRow], list[fusion_sheets.FusionEventRow], list[fusion_sheets.FusionEventRow], dict[str, int]]:
    live_events: list[fusion_sheets.FusionEventRow] = []
    upcoming_events: list[fusion_sheets.FusionEventRow] = []
    ending_events: list[fusion_sheets.FusionEventRow] = []
    skipped: dict[str, int] = {}
    for event in events:
        timing = fusion_sheets.get_valid_event_timing(event, for_helper="fusion_grouped_reminders")
        if timing is None:
            _append_skip(skipped, "invalid_timing")
            continue
        start_at, end_at = timing
        if settings.include_start_events and start_at <= reference and (end_at is None or reference < end_at):
            live_events.append(event)
        if settings.include_upcoming_events:
            upcoming_horizon = reference + dt.timedelta(days=settings.upcoming_window_days)
            if reference < start_at <= upcoming_horizon:
                upcoming_events.append(event)
        if settings.include_ending_events and end_at is not None and reference <= end_at <= reference + dt.timedelta(hours=settings.end_lookahead_hours):
            ending_events.append(event)
    return live_events, upcoming_events, ending_events, skipped


async def _load_grouped_sent_keys(target: fusion_sheets.FusionRow) -> tuple[set[tuple[str, str]], bool]:
    dedupe_meta = fusion_sheets.reminder_dedupe_backend_metadata()
    dedupe_backend = dedupe_meta.get("backend_type", "unknown")
    dedupe_tab = dedupe_meta.get("tab_name", "")
    dedupe_config_key = dedupe_meta.get("config_key", "")
    durable_dedupe_available = time.monotonic() >= _DEDUP_BACKOFF_UNTIL_MONOTONIC
    if not durable_dedupe_available:
        _register_dedupe_degraded_mode()
        log.warning(
            "fusion grouped reminder durable dedupe in backoff window; using in-memory fallback",
            extra={
                "fusion_id": target.fusion_id,
                "retry_backoff_sec": _DEDUP_TIMEOUT_BACKOFF_SEC,
                "dedupe_backend": dedupe_backend,
                "dedupe_tab": dedupe_tab,
                "dedupe_config_key": dedupe_config_key,
                "operation": "read_sent_reminder_keys",
            },
        )
        return set(), False
    try:
        sent_keys = await asyncio.wait_for(
            fusion_sheets.get_sent_reminder_keys(target.fusion_id),
            timeout=_DEDUP_TIMEOUT_SEC,
        )
        _recover_from_dedupe_backoff()
        return sent_keys, True
    except TimeoutError as exc:
        _register_dedupe_timeout_backoff()
        context = {
            "fusion_id": target.fusion_id,
            "timeout_sec": _DEDUP_TIMEOUT_SEC,
            "retry_backoff_sec": _DEDUP_TIMEOUT_BACKOFF_SEC,
            "dedupe_backend": dedupe_backend,
            "dedupe_tab": dedupe_tab,
            "dedupe_config_key": dedupe_config_key,
            "operation": "read_sent_reminder_keys",
        }
        log.exception("fusion grouped reminder durable dedupe timed out; using in-memory fallback", extra=context)
        await fusion_logs.send_ops_alert(
            component="reminders",
            summary="grouped_dedupe_unavailable_degraded_mode",
            dedupe_key=f"fusion:grouped_reminders:dedupe:{target.fusion_id}",
            error=exc,
            fields=context,
        )
        return set(), False
    except Exception as exc:
        _register_dedupe_degraded_mode()
        context = {
            "fusion_id": target.fusion_id,
            "dedupe_backend": dedupe_backend,
            "dedupe_tab": dedupe_tab,
            "dedupe_config_key": dedupe_config_key,
            "operation": "read_sent_reminder_keys",
        }
        log.exception("fusion grouped reminder failed to load durable dedupe; using in-memory fallback", extra=context)
        await fusion_logs.send_ops_alert(
            component="reminders",
            summary="grouped_dedupe_unavailable_degraded_mode",
            dedupe_key=f"fusion:grouped_reminders:dedupe:{target.fusion_id}",
            error=exc,
            fields=context,
        )
        return set(), False


async def _resolve_channel_role_status(bot: commands.Bot, target: fusion_sheets.FusionRow) -> dict[str, object]:
    channel_id = target.announcement_channel_id
    role_id = target.opt_in_role_id
    channel = None
    if channel_id:
        get_channel = getattr(bot, "get_channel", None)
        if callable(get_channel):
            channel = get_channel(int(channel_id))
        if channel is None:
            fetch_channel = getattr(bot, "fetch_channel", None)
            if callable(fetch_channel):
                try:
                    channel = await fetch_channel(int(channel_id))
                except Exception:
                    channel = None
    role_resolved = False
    if role_id:
        for guild in getattr(bot, "guilds", []) or []:
            get_role = getattr(guild, "get_role", None)
            if callable(get_role) and get_role(int(role_id)) is not None:
                role_resolved = True
                break
    return {
        "channel_id": channel_id,
        "channel_resolved": channel is not None,
        "thread_id": getattr(channel, "id", None) if isinstance(channel, discord.Thread) else None,
        "thread_resolved": isinstance(channel, discord.Thread),
        "role_id": role_id,
        "role_resolved": role_resolved if role_id else None,
    }


async def collect_fusion_reminder_startup_summary(
    bot: commands.Bot,
    *,
    scheduler_started: bool,
    now: dt.datetime | None = None,
) -> list[str]:
    """Build non-secret grouped Fusion reminder scheduler diagnostics for startup logs."""

    reference = _utc_now(now)
    lines = ["🧬 Fusion grouped reminders"]
    lines.append(f"• scheduler_started={'yes' if scheduler_started else 'no'}")
    try:
        target = await fusion_sheets.get_publishable_fusion()
    except Exception as exc:
        await fusion_logs.send_ops_alert(
            component="grouped_reminders_startup",
            summary="load_target_fusion_failed",
            dedupe_key="fusion:grouped_reminders_startup:target",
            error=exc,
        )
        lines.extend(["• enabled=no", "• skipped=load_target_failed"])
        return lines
    if target is None:
        lines.extend(["• enabled=no", "• skipped=no_publishable_fusion"])
        return lines

    try:
        settings = await fusion_sheets.get_fusion_reminder_settings(now=reference)
    except Exception as exc:
        await fusion_logs.send_ops_alert(
            component="grouped_reminders_startup",
            summary="load_settings_failed",
            dedupe_key=f"fusion:grouped_reminders_startup:settings:{target.fusion_id}",
            error=exc,
            fields={"fusion_id": target.fusion_id},
        )
        lines.extend(["• enabled=no", "• skipped=load_settings_failed"])
        return lines

    log.debug("fusion grouped reminder startup settings diagnostics", extra=_grouped_settings_diagnostics(settings))
    post_time = _parse_grouped_post_time_utc(settings.grouped_post_time_utc)
    enabled = settings.group_events and post_time is not None
    lines.append(f"• enabled={'yes' if enabled else 'no'}")
    lines.append(f"• configured_local_post_time={_configured_local_post_time(settings)}")
    parsed_post_time_text = settings.grouped_post_time_utc if post_time is not None else "missing_or_invalid"
    lines.append(f"• parsed_utc_post_time={parsed_post_time_text}")
    resolve_status = await _resolve_channel_role_status(bot, target)
    lines.append(
        "• resolved channel={channel} thread={thread} role={role}".format(
            channel=_format_bool_status(resolve_status["channel_resolved"]),
            thread=_format_bool_status(resolve_status["thread_resolved"]),
            role=_format_bool_status(resolve_status["role_resolved"]),
        )
    )

    if not settings.group_events:
        lines.append("• skipped=grouped_reminders_disabled reason=group_events_resolved_false")
        return lines
    if post_time is None:
        lines.append("• skipped=missing_or_invalid_grouped_post_time_utc")
        return lines

    try:
        sent_keys, _durable = await _load_grouped_sent_keys(target)
        last_sent = await fusion_sheets.get_last_reminder_sent_at(target.fusion_id, reminder_type=_GROUPED_REMINDER_TYPE)
    except Exception as exc:
        await fusion_logs.send_ops_alert(
            component="grouped_reminders_startup",
            summary="load_dedupe_failed",
            dedupe_key=f"fusion:grouped_reminders_startup:dedupe:{target.fusion_id}",
            error=exc,
            fields={"fusion_id": target.fusion_id},
        )
        sent_keys = set()
        last_sent = None
    next_due = _next_grouped_due(now=reference, post_time=post_time, sent_keys=sent_keys)

    try:
        events = await fusion_sheets.get_fusion_events(target.fusion_id)
        live_events, upcoming_events, ending_events, skipped = _select_grouped_events(
            settings=settings,
            events=events,
            reference=reference,
        )
        active_count = len(live_events) + len(upcoming_events) + len(ending_events)
        skip_text = _render_skips(skipped) if active_count else "no_grouped_events"
        log.debug(
            "fusion grouped reminder startup event diagnostics",
            extra={
                "fusion_id": target.fusion_id,
                "rows_loaded": len(events),
                "grouped_events": active_count,
                "last_grouped_sent": _format_sent(last_sent),
                "event_skip_details": skip_text,
            },
        )
        lines.append(f"• next_due={_format_due(next_due)}")
        if active_count == 0:
            lines.append("• skipped=no_grouped_events")
    except Exception as exc:
        await fusion_logs.send_ops_alert(
            component="grouped_reminders_startup",
            summary="load_events_failed",
            dedupe_key=f"fusion:grouped_reminders_startup:events:{target.fusion_id}",
            error=exc,
            fields={"fusion_id": target.fusion_id},
        )
        lines.append("• skipped=load_events_failed")
    return lines


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

    try:
        target = await fusion_sheets.get_publishable_fusion()
    except Exception as exc:
        log.exception("fusion grouped reminder failed to load target fusion")
        await fusion_logs.send_ops_alert(
            component="reminders",
            summary="grouped_load_target_fusion_failed",
            dedupe_key="fusion:grouped_reminders:load_target",
            error=exc,
        )
        return
    if target is None:
        return

    try:
        settings = await fusion_sheets.get_fusion_reminder_settings(now=reference)
    except Exception as exc:
        context = {"fusion_id": target.fusion_id}
        log.exception("fusion grouped reminder failed to load settings", extra=context)
        await fusion_logs.send_ops_alert(
            component="reminders",
            summary="grouped_load_settings_failed",
            dedupe_key=f"fusion:grouped_reminders:settings:{target.fusion_id}",
            error=exc,
            fields=context,
        )
        return

    if not settings.group_events:
        await fusion_logs.send_ops_alert(
            component="reminders",
            summary="grouped_reminders_disabled",
            dedupe_key=f"fusion:grouped_reminders:disabled:{target.fusion_id}",
            reason="group_events_resolved_false",
            fields={"fusion_id": target.fusion_id, **_group_events_config_fields(settings)},
        )
        return

    post_time = _parse_grouped_post_time_utc(settings.grouped_post_time_utc)
    if post_time is None:
        await fusion_logs.send_ops_alert(
            component="reminders",
            summary="grouped_post_time_missing_or_invalid",
            dedupe_key=f"fusion:grouped_reminders:post_time:{target.fusion_id}",
            fields={"fusion_id": target.fusion_id, "configured_post_time_utc": settings.grouped_post_time_utc or "missing"},
        )
        return

    sent_keys, durable_dedupe_available = await _load_grouped_sent_keys(target)
    grouped_event_id = _grouped_event_id_for_date(reference.date())
    grouped_key = (grouped_event_id, _GROUPED_REMINDER_TYPE)
    memory_key = (target.fusion_id, grouped_event_id, _GROUPED_REMINDER_TYPE)
    await _maybe_alert_prolonged_dedupe_degradation(
        target.fusion_id,
        reminder_type=_GROUPED_REMINDER_TYPE,
        backend=fusion_sheets.reminder_dedupe_backend_metadata().get("backend_type", "unknown"),
    )
    if grouped_key in sent_keys or memory_key in _MEMORY_SENT_KEYS:
        return
    if not _is_grouped_due(now=reference, post_time=post_time, sent_keys=sent_keys):
        return

    try:
        events = await fusion_sheets.get_fusion_events(target.fusion_id)
    except Exception as exc:
        context = {"fusion_id": target.fusion_id}
        log.exception("fusion grouped reminder failed to load events", extra=context)
        await fusion_logs.send_ops_alert(
            component="reminders",
            summary="grouped_load_events_failed",
            dedupe_key=f"fusion:grouped_reminders:events:{target.fusion_id}",
            error=exc,
            fields=context,
        )
        return

    live_events, upcoming_events, ending_events, skipped = _select_grouped_events(
        settings=settings,
        events=events,
        reference=reference,
    )
    if not (live_events or upcoming_events or ending_events):
        context = {"fusion_id": target.fusion_id, "skipped": _render_skips(skipped) or "no_grouped_events"}
        log.warning("fusion grouped reminder skipped; no grouped events selected", extra=context)
        await fusion_logs.send_ops_alert(
            component="reminders",
            summary="grouped_no_events_selected",
            dedupe_key=f"fusion:grouped_reminders:no_events:{target.fusion_id}:{grouped_event_id}",
            fields=context,
        )
        return

    missing = _missing_grouped_copy_fields(settings)
    if missing:
        context = {"fusion_id": target.fusion_id, "missing_fields": ",".join(missing)}
        log.warning("fusion grouped reminder skipped; missing required sheet copy fields", extra=context)
        await fusion_logs.send_ops_alert(
            component="reminders",
            summary="grouped_copy_missing",
            dedupe_key=f"fusion:grouped_reminders:copy:{target.fusion_id}",
            fields=context,
        )
        return

    try:
        announcement_message = await ensure_fusion_announcement(bot, target)
    except Exception as exc:
        context = {"fusion_id": target.fusion_id, "announcement_channel_id": target.announcement_channel_id}
        log.exception("fusion grouped reminder failed to resolve announcement", extra=context)
        await fusion_logs.send_ops_alert(
            component="reminders",
            summary="grouped_announcement_resolve_failed",
            dedupe_key=f"fusion:grouped_reminders:announcement:{target.fusion_id}",
            error=exc,
            fields=context,
        )
        return
    if announcement_message is None:
        context = {"fusion_id": target.fusion_id, "announcement_channel_id": target.announcement_channel_id}
        log.warning("fusion grouped reminder skipped; announcement unavailable", extra=context)
        await fusion_logs.send_ops_alert(
            component="reminders",
            summary="grouped_announcement_unavailable",
            dedupe_key=f"fusion:grouped_reminders:announcement_unavailable:{target.fusion_id}",
            fields=context,
        )
        return

    embed = _build_grouped_embed(
        settings=settings,
        fusion_title=target.fusion_name,
        jump_url=announcement_message.jump_url,
        live_events=live_events,
        upcoming_events=upcoming_events,
        ending_events=ending_events,
    )
    mention_content = f"<@&{target.opt_in_role_id}>" if target.opt_in_role_id else None
    try:
        await announcement_message.channel.send(content=mention_content, embed=embed, view=build_fusion_opt_in_view(target))
    except Exception as exc:
        context = {
            "fusion_id": target.fusion_id,
            "event_id": grouped_event_id,
            "reminder_type": _GROUPED_REMINDER_TYPE,
            "channel_id": getattr(getattr(announcement_message, "channel", None), "id", None),
            "thread_id": getattr(getattr(announcement_message, "channel", None), "id", None)
            if isinstance(getattr(announcement_message, "channel", None), discord.Thread)
            else None,
        }
        log.exception("fusion grouped reminder send failed", extra=context)
        await fusion_logs.send_ops_alert(
            component="reminders",
            summary="grouped_send_failed",
            dedupe_key=f"fusion:grouped_reminders:send:{target.fusion_id}:{grouped_event_id}",
            error=exc,
            fields=context,
        )
        return

    _MEMORY_SENT_KEYS.add(memory_key)
    if durable_dedupe_available:
        try:
            await fusion_sheets.mark_reminder_sent(
                target.fusion_id,
                event_id=grouped_event_id,
                reminder_type=_GROUPED_REMINDER_TYPE,
                sent_at=reference,
            )
        except Exception as exc:
            context = {
                "fusion_id": target.fusion_id,
                "event_id": grouped_event_id,
                "reminder_type": _GROUPED_REMINDER_TYPE,
            }
            log.exception("fusion grouped reminder sent but dedupe write failed", extra=context)
            await fusion_logs.send_ops_alert(
                component="reminders",
                summary="grouped_dedupe_write_failed_after_send",
                dedupe_key=f"fusion:grouped_reminders:dedupe_write:{target.fusion_id}:{grouped_event_id}",
                error=exc,
                fields=context,
            )


__all__ = ["process_fusion_reminders", "collect_fusion_reminder_startup_summary"]


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
        summary="grouped_dedupe_unavailable_degraded_mode",
        dedupe_key=f"fusion:grouped_reminders:dedupe_degraded:{fusion_id}:{reminder_type}",
        fields={
            "fusion_id": fusion_id,
            "reminder_type": reminder_type,
            "backend": backend,
            "degraded_duration_sec": round(duration_sec, 1),
        },
    )
