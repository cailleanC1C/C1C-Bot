from __future__ import annotations

import datetime as dt
import logging
import math
import asyncio
import time
import io
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

import aiohttp
import discord
from discord.ext import commands

from modules.common.embeds import get_embed_colour
from modules.common import feature_flags
from modules.common import runtime as rt
from modules.community.reset_reminders.models import ResetReminder
from modules.community.reset_reminders.views import ResetReminderView
from shared.config import get_milestones_sheet_id
from shared.dedupe import EventDeduper
from shared.sheets.async_core import acall_with_backoff, afetch_values, aget_worksheet
from shared.sheets import milestones_config

if TYPE_CHECKING:
    from modules.common.runtime import Runtime

log = logging.getLogger("c1c.community.reset_reminders.scheduler")

_CUSTOM_EMOJI_RE = re.compile(r"^<a?:([A-Za-z0-9_]{2,32}):(\d+)>$")
_DIRECT_IMAGE_SCHEMES = {"http", "https"}
_RESET_IMAGE_DOWNLOAD_TIMEOUT_SEC = 10
_RESET_IMAGE_MAX_BYTES = 8 * 1024 * 1024
_SUPPORTED_IMAGE_CONTENT_TYPES = {
    "image/gif": "gif",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}

_RESET_REMINDER_TAB_KEY = "RESET_REMINDER_TAB"
_FEATURE_TOGGLE_KEY = "reset_reminders"
_INVALID_ROW_ALERT_DEDUPER = EventDeduper(window_s=900.0, max_keys=128)
_LOAD_FAILURE_ALERT_COOLDOWN_SEC = 1800.0
_LOAD_FAILURE_ALERT_THRESHOLD = 3
_LOAD_RETRY_ATTEMPTS = 2
_LOAD_RETRY_BACKOFF_SEC = 0.5
_DUE_JOB_RETRY_DELAY = dt.timedelta(minutes=15)
_load_failure_state: dict[str, Any] = {
    "key": None,
    "last_alert": 0.0,
    "failures": 0,
    "alert_sent": False,
}
_last_successful_load: dict[str, Any] = {
    "tab_name": None,
    "header_map": None,
    "records": None,
}
_next_sheet_load_after_utc: dt.datetime | None = None
_PROCESS_LOCK = asyncio.Lock()
_REQUIRED_COLUMNS: tuple[str, ...] = (
    "reset_id",
    "label",
    "status",
    "reference_date_utc",
    "cycle_days",
    "lead_minutes",
    "role_id",
    "channel_id",
    "thread_id",
    "embed_title",
    "embed_description",
    "embed_footer",
    "button_label_opt_in",
    "button_label_opt_out",
    "last_sent_for_reset_utc",
    "next_scheduled_post_utc",
    "last_message_id",
    "emojinameorid",
)


@dataclass(frozen=True, slots=True)
class _ResetReminderRecord:
    row_number: int
    reminder: ResetReminder


def _set_next_sheet_load_after_from_records(
    records: list[_ResetReminderRecord], now_utc: dt.datetime
) -> None:
    """Refresh the next safe sheet-load time from current in-memory records."""

    global _next_sheet_load_after_utc
    future_due_times = [
        record.reminder.next_scheduled_post_utc
        for record in records
        if record.reminder.next_scheduled_post_utc is not None
        and record.reminder.next_scheduled_post_utc > now_utc
    ]
    if future_due_times:
        _next_sheet_load_after_utc = min(future_due_times)
    elif records:
        _next_sheet_load_after_utc = now_utc + dt.timedelta(minutes=5)
    else:
        _next_sheet_load_after_utc = now_utc + dt.timedelta(minutes=15)


def _replace_cached_record(
    records: list[_ResetReminderRecord],
    row_number: int,
    reminder: ResetReminder,
    *,
    now_utc: dt.datetime,
) -> list[_ResetReminderRecord]:
    """Replace a mutated reminder in cache so due-cache ticks do not repeat work."""

    updated: list[_ResetReminderRecord] = []
    replaced = False
    for record in records:
        if record.row_number == row_number:
            updated.append(
                _ResetReminderRecord(row_number=row_number, reminder=reminder)
            )
            replaced = True
        else:
            updated.append(record)
    if not replaced:
        updated = records
    if _last_successful_load.get("records") is records or replaced:
        _last_successful_load["records"] = updated
    _set_next_sheet_load_after_from_records(updated, now_utc)
    return updated


def _record_reset_reminder_discord_send_in_memory(
    records: list[_ResetReminderRecord],
    record: _ResetReminderRecord,
    *,
    reset_time: dt.datetime,
    following_reminder_time: dt.datetime,
    message_id: int,
    now_utc: dt.datetime,
) -> list[_ResetReminderRecord]:
    """Suppress duplicate sends immediately after Discord accepts a reminder."""

    updated_reminder = replace(
        record.reminder,
        last_sent_for_reset_utc=reset_time,
        next_scheduled_post_utc=following_reminder_time,
        last_message_id=message_id,
    )
    return _replace_cached_record(
        records,
        record.row_number,
        updated_reminder,
        now_utc=now_utc,
    )


def _is_feature_enabled() -> bool:
    return feature_flags.is_enabled(_FEATURE_TOGGLE_KEY)


def _normalize(value: Any) -> str:
    return str(value or "").strip().lower()


def _column_label(index: int) -> str:
    value = index + 1
    label = ""
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        label = chr(65 + remainder) + label
    return label or "A"


def _sheet_id() -> str:
    sheet_id = get_milestones_sheet_id().strip()
    if not sheet_id:
        raise RuntimeError("MILESTONES_SHEET_ID not set")
    return sheet_id


async def _tab_name() -> str:
    return await milestones_config.arequire_value(_RESET_REMINDER_TAB_KEY)


def _cell(row: list[Any], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    return str(row[index] or "").strip()


def _parse_dt(value: object) -> dt.datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("missing datetime")
    parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _parse_dt_optional(value: object) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return _parse_dt(raw)
    except ValueError:
        return None


def _parse_int(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    return int(text)


def _parse_optional_int(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    return int(text)


def _resolve_header_map(header: list[Any]) -> dict[str, int]:
    normalized = [_normalize(cell) for cell in header]
    indices = {name: idx for idx, name in enumerate(normalized) if name}
    missing = [column for column in _REQUIRED_COLUMNS if column not in indices]
    if missing:
        raise RuntimeError(f"Reset reminder tab missing required columns: {missing}")
    return indices


async def _load_reset_reminder_records(
    *, active_only: bool
) -> tuple[str, dict[str, int], list[_ResetReminderRecord]]:
    stage = "config_load"
    started = time.monotonic()
    tab_name = "unknown"
    try:
        tab_name = await _tab_name()
        sheet_id = _sheet_id()
        stage = "sheet_fetch"
        log.info(
            "reset reminder sheet load started",
            extra={"tab": tab_name, "active_only": active_only},
        )
        matrix = await afetch_values(sheet_id, tab_name)
        if not matrix:
            return tab_name, {}, []

        stage = "parse_rows"
        header_map = _resolve_header_map(matrix[0])
    except Exception as exc:
        elapsed = time.monotonic() - started
        setattr(exc, "reset_reminder_stage", stage)
        setattr(exc, "reset_reminder_tab", tab_name)
        setattr(exc, "reset_reminder_elapsed", elapsed)
        raise
    records: list[_ResetReminderRecord] = []
    invalid_rows: list[dict[str, Any]] = []

    for row_number, row in enumerate(matrix[1:], start=2):
        try:
            status = _cell(row, header_map["status"])
            if active_only and status.lower() != "active":
                continue

            role_id = _parse_int(_cell(row, header_map["role_id"]))
            channel_id = _parse_int(_cell(row, header_map["channel_id"]))
            cycle_days = _parse_int(_cell(row, header_map["cycle_days"]))
            lead_minutes = _parse_int(_cell(row, header_map["lead_minutes"]))
            reminder = ResetReminder(
                reset_id=_cell(row, header_map["reset_id"]),
                label=_cell(row, header_map["label"]),
                status=status,
                reference_date_utc=_parse_dt(
                    _cell(row, header_map["reference_date_utc"])
                ),
                cycle_days=cycle_days,
                lead_minutes=lead_minutes,
                role_id=role_id,
                channel_id=channel_id,
                thread_id=_parse_optional_int(_cell(row, header_map["thread_id"])),
                embed_title=_cell(row, header_map["embed_title"]),
                embed_description=_cell(row, header_map["embed_description"]),
                embed_footer=_cell(row, header_map["embed_footer"]),
                button_label_opt_in=_cell(row, header_map["button_label_opt_in"]),
                button_label_opt_out=_cell(row, header_map["button_label_opt_out"]),
                last_sent_for_reset_utc=_parse_dt_optional(
                    _cell(row, header_map["last_sent_for_reset_utc"])
                ),
                next_scheduled_post_utc=_parse_dt_optional(
                    _cell(row, header_map["next_scheduled_post_utc"])
                ),
                last_message_id=_parse_optional_int(
                    _cell(row, header_map["last_message_id"])
                ),
                emoji_name_or_id=_cell(row, header_map["emojinameorid"]),
            )
            records.append(
                _ResetReminderRecord(row_number=row_number, reminder=reminder)
            )
        except Exception as exc:
            reason = type(exc).__name__
            invalid_rows.append(
                {
                    "row_number": row_number,
                    "reset_id": _cell(row, header_map.get("reset_id", -1)),
                    "reason": reason,
                }
            )
            log.exception(
                "invalid reset reminder row skipped",
                extra={"tab": tab_name, "row_number": row_number},
            )
            continue

    valid_active_count = (
        len(records)
        if active_only
        else sum(1 for r in records if r.reminder.status.lower() == "active")
    )
    log.info(
        "reset reminder rows loaded",
        extra={
            "tab": tab_name,
            "active_only": active_only,
            "valid_active_rows": valid_active_count,
            "invalid_rows": len(invalid_rows),
            "invalid_row_details": invalid_rows,
            "load_duration_ms": round((time.monotonic() - started) * 1000, 1),
        },
    )
    if active_only and invalid_rows:
        detail = ", ".join(
            f"row={entry['row_number']} reset_id={entry.get('reset_id') or '-'} reason={entry.get('reason') or '-'}"
            for entry in invalid_rows
        )
        dedupe_key = f"reset_reminders:invalid_active_rows:{tab_name}:{len(invalid_rows)}:{detail[:120]}"
        if _INVALID_ROW_ALERT_DEDUPER.should_emit(dedupe_key):
            try:
                await rt.send_log_message(
                    f"⚠️ Reset reminders — invalid active rows skipped • tab={tab_name} • count={len(invalid_rows)} • {detail}"
                )
            except Exception:
                log.warning(
                    "failed to send reset reminder invalid-row ops alert", exc_info=True
                )

    return tab_name, header_map, records


def _next_reset_description(
    base_description: str, reset_time_utc: dt.datetime | None
) -> str:
    if reset_time_utc is None:
        return base_description
    unix_seconds = int(reset_time_utc.astimezone(dt.timezone.utc).timestamp())
    countdown = f"Current cycle resets at: <t:{unix_seconds}:F>\nTime left: <t:{unix_seconds}:R>"
    base = str(base_description or "").rstrip()
    return f"{base}\n\n{countdown}" if base else countdown


def _reset_reminder_footer(
    configured_footer: str,
    reset_time_utc: dt.datetime,
    cycle_days: int,
) -> str:
    following_cycle_reset_time = reset_time_utc.astimezone(
        dt.timezone.utc
    ) + dt.timedelta(days=cycle_days)
    following_cycle_text = (
        "Following cycle reset: "
        f"{following_cycle_reset_time.strftime('%Y-%m-%d %H:%M UTC')}"
    )
    footer = str(configured_footer or "").strip()
    return f"{footer} • {following_cycle_text}" if footer else following_cycle_text


def _custom_emoji_id(value: str) -> int | None:
    match = _CUSTOM_EMOJI_RE.match(value.strip())
    if not match:
        return None
    return int(match.group(2))


def _resolve_custom_emoji(
    target: discord.abc.Messageable | None, value: str | None
) -> discord.Emoji | None:
    text = str(value or "").strip()
    if not text:
        return None

    guild = getattr(target, "guild", None)
    if guild is None:
        return None

    emojis = getattr(guild, "emojis", []) or []
    wanted_id = int(text) if text.isdigit() else _custom_emoji_id(text)
    if wanted_id is not None:
        for emoji in emojis:
            if getattr(emoji, "id", None) == wanted_id:
                return emoji
        return None

    for emoji in emojis:
        if getattr(emoji, "name", None) == text:
            return emoji
    return None


def _message_content_for_reminder(reminder: ResetReminder) -> str:
    return f"<@&{reminder.role_id}>"


def _is_direct_image_url(value: str) -> bool:
    parsed = urlparse(value.strip())
    return parsed.scheme.lower() in _DIRECT_IMAGE_SCHEMES and bool(parsed.netloc)


def _reset_image_filename(reminder: ResetReminder, extension: str) -> str:
    safe_reset_id = re.sub(
        r"[^A-Za-z0-9_.-]+", "_", reminder.reset_id or "reset_reminder"
    ).strip("._")
    return f"{safe_reset_id or 'reset_reminder'}_icon.{extension}"


async def _download_image_url_to_file(
    url: str, *, reminder: ResetReminder
) -> discord.File | None:
    timeout = aiohttp.ClientTimeout(total=_RESET_IMAGE_DOWNLOAD_TIMEOUT_SEC)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status < 200 or response.status >= 300:
                    log.warning(
                        "reset reminder image URL download failed",
                        extra={
                            "reset_id": reminder.reset_id,
                            "image_value": url,
                            "status": response.status,
                        },
                    )
                    return None

                content_type = (
                    str(response.headers.get("Content-Type") or "")
                    .split(";", 1)[0]
                    .strip()
                    .lower()
                )
                extension = _SUPPORTED_IMAGE_CONTENT_TYPES.get(content_type)
                if extension is None:
                    log.warning(
                        "reset reminder image URL rejected; non-image or unsupported content type",
                        extra={
                            "reset_id": reminder.reset_id,
                            "image_value": url,
                            "content_type": content_type,
                        },
                    )
                    return None

                raw = await response.content.read(_RESET_IMAGE_MAX_BYTES + 1)
                if len(raw) > _RESET_IMAGE_MAX_BYTES:
                    log.warning(
                        "reset reminder image URL rejected; image too large",
                        extra={"reset_id": reminder.reset_id, "image_value": url},
                    )
                    return None
                return discord.File(
                    io.BytesIO(raw), filename=_reset_image_filename(reminder, extension)
                )
    except Exception:
        log.warning(
            "reset reminder image URL download failed",
            extra={"reset_id": reminder.reset_id, "image_value": url},
            exc_info=True,
        )
        return None


async def _reset_image_to_file(
    target: discord.abc.Messageable | None,
    reminder: ResetReminder,
) -> discord.File | None:
    configured = str(reminder.emoji_name_or_id or "").strip()
    if not configured:
        return None

    if _is_direct_image_url(configured):
        return await _download_image_url_to_file(configured, reminder=reminder)

    if getattr(target, "guild", None) is None:
        log.warning(
            "reset reminder image emoji could not be resolved; target guild unavailable",
            extra={"reset_id": reminder.reset_id, "image_value": configured},
        )
        return None

    emoji = _resolve_custom_emoji(target, configured)
    if emoji is None:
        log.warning(
            "reset reminder image emoji could not be resolved",
            extra={"reset_id": reminder.reset_id, "image_value": configured},
        )
        return None

    try:
        raw = await emoji.read()
    except Exception:
        log.warning(
            "reset reminder image emoji download failed",
            extra={"reset_id": reminder.reset_id, "image_value": configured},
            exc_info=True,
        )
        return None

    extension = "gif" if bool(getattr(emoji, "animated", False)) else "png"
    return discord.File(
        io.BytesIO(raw), filename=_reset_image_filename(reminder, extension)
    )


def _utc_now(now: dt.datetime | None = None) -> dt.datetime:
    if now is None:
        return dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(dt.timezone.utc)


def _reminder_time(reset_time_utc: dt.datetime, lead_minutes: int) -> dt.datetime:
    return reset_time_utc - dt.timedelta(minutes=lead_minutes)


def _next_reminder_from_anchor(
    reference_date_utc: dt.datetime,
    cycle_days: int,
    lead_minutes: int,
    now_utc: dt.datetime,
) -> dt.datetime:
    cycle = dt.timedelta(days=cycle_days)
    first_reminder_time = _reminder_time(reference_date_utc, lead_minutes)
    if now_utc <= first_reminder_time:
        return first_reminder_time
    cycles_ahead = math.ceil((now_utc - first_reminder_time) / cycle)
    return first_reminder_time + cycles_ahead * cycle


def _reset_time_for_scheduled_post(
    reminder_time_utc: dt.datetime, lead_minutes: int
) -> dt.datetime:
    return reminder_time_utc + dt.timedelta(minutes=lead_minutes)


def _following_reminder_time(
    reminder_time_utc: dt.datetime, cycle_days: int
) -> dt.datetime:
    return reminder_time_utc + dt.timedelta(days=cycle_days)


def _advance_reminder_until_reset_future(
    reminder_time_utc: dt.datetime,
    *,
    cycle_days: int,
    lead_minutes: int,
    now_utc: dt.datetime,
) -> dt.datetime:
    advanced = reminder_time_utc
    while _reset_time_for_scheduled_post(advanced, lead_minutes) <= now_utc:
        advanced = _following_reminder_time(advanced, cycle_days)
    return advanced


async def register_persistent_reset_views(bot: commands.Bot) -> None:
    if not _is_feature_enabled():
        log.info(
            "reset reminders disabled via feature toggle; skipping persistent view registration"
        )
        return
    try:
        _, _, records = await _load_reset_reminder_records(active_only=True)
    except Exception:
        log.exception(
            "failed to load reset reminders for persistent views; skipping reset reminder views"
        )
        return
    for record in records:
        reminder = record.reminder
        bot.add_view(
            ResetReminderView(
                role_id=reminder.role_id,
                label_opt_in=reminder.button_label_opt_in,
                label_opt_out=reminder.button_label_opt_out,
            )
        )


async def _send_ops_log(message: str) -> None:
    try:
        await rt.send_log_message(message)
    except Exception:
        log.warning("failed to send reset reminder ops alert", exc_info=True)


def _load_failure_key(exc: Exception) -> str:
    stage = getattr(exc, "reset_reminder_stage", "unknown")
    tab = getattr(exc, "reset_reminder_tab", "unknown")
    return f"reset_reminders:{_RESET_REMINDER_TAB_KEY}:{stage}:{tab}:{type(exc).__name__}:{str(exc)}"


async def _record_load_failure(exc: Exception) -> None:
    state = _load_failure_state
    key = _load_failure_key(exc)
    now = time.monotonic()
    previous_key = state.get("key")
    state["key"] = key
    state["failures"] = (
        (int(state.get("failures") or 0) + 1) if previous_key == key else 1
    )

    stage = getattr(exc, "reset_reminder_stage", "unknown")
    tab = getattr(exc, "reset_reminder_tab", "unknown")
    elapsed = getattr(exc, "reset_reminder_elapsed", None)
    log_extra = {
        "scheduler": "reset_reminders",
        "config_key": _RESET_REMINDER_TAB_KEY,
        "tab": tab,
        "operation": stage,
        "timeout_type": type(exc).__name__ if isinstance(exc, TimeoutError) else "",
        "elapsed_s": (
            round(float(elapsed), 3) if isinstance(elapsed, (int, float)) else None
        ),
        "failure_count": state["failures"],
        "exception_type": type(exc).__name__,
        "exception_message": str(exc),
    }
    if previous_key != key or int(state["failures"]) == 1:
        log.error(
            "failed to load reset reminders; scheduler tick skipped",
            extra=log_extra,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    else:
        log.info("repeated reset reminder load failure suppressed", extra=log_extra)

    last_alert = float(state.get("last_alert") or 0.0)
    should_send = int(state["failures"]) >= _LOAD_FAILURE_ALERT_THRESHOLD and (
        not bool(state.get("alert_sent"))
        or previous_key != key
        or now - last_alert >= _LOAD_FAILURE_ALERT_COOLDOWN_SEC
    )
    if should_send:
        state["last_alert"] = now
        state["alert_sent"] = True
        log.warning(
            "reset reminder Discord warning sent",
            extra={"consecutive_failures": state["failures"]},
        )
        await _send_ops_log(
            "⚠️ Reset reminders failed to load after repeated ticks; scheduler tick skipped. "
            f"See app logs. error={type(exc).__name__} consecutive_failures={state['failures']}"
        )
    else:
        log.info(
            "reset reminder Discord warning suppressed",
            extra={
                "consecutive_failures": state["failures"],
                "threshold": _LOAD_FAILURE_ALERT_THRESHOLD,
            },
        )


async def _record_load_success() -> None:
    failures = int(_load_failure_state.get("failures") or 0)
    alert_sent = bool(_load_failure_state.get("alert_sent"))
    if failures > 0 and alert_sent:
        log.info(
            "reset reminder recovery message sent",
            extra={"consecutive_failures": failures},
        )
        await _send_ops_log(
            f"✅ Reset reminders loaded again after {failures} failed tick(s)."
        )
    elif failures > 0:
        log.info(
            "reset reminder recovery message suppressed",
            extra={"consecutive_failures": failures},
        )
    _load_failure_state.update(
        {"key": None, "last_alert": 0.0, "failures": 0, "alert_sent": False}
    )


async def _resolve_target_channel(
    bot: commands.Bot, reminder: ResetReminder
) -> discord.abc.Messageable | None:
    base_channel = bot.get_channel(reminder.channel_id)
    if base_channel is None:
        try:
            base_channel = await bot.fetch_channel(reminder.channel_id)
        except Exception as exc:
            log.exception(
                "reset reminder target channel fetch failed",
                extra={
                    "reset_id": reminder.reset_id,
                    "channel_id": reminder.channel_id,
                },
            )
            await _send_ops_log(
                "⚠️ Reset reminder target fetch failed "
                f"• reset_id={reminder.reset_id} • channel_id={reminder.channel_id} • error={type(exc).__name__}"
            )
            return None

    if reminder.thread_id:
        thread = bot.get_channel(reminder.thread_id)
        if thread is None:
            try:
                thread = await bot.fetch_channel(reminder.thread_id)
            except Exception as exc:
                log.exception(
                    "reset reminder target thread fetch failed",
                    extra={
                        "reset_id": reminder.reset_id,
                        "channel_id": reminder.channel_id,
                        "thread_id": reminder.thread_id,
                    },
                )
                await _send_ops_log(
                    "⚠️ Reset reminder thread fetch failed "
                    f"• reset_id={reminder.reset_id} • channel_id={reminder.channel_id} "
                    f"• thread_id={reminder.thread_id} • error={type(exc).__name__}"
                )
                return None
        if isinstance(thread, discord.abc.Messageable):
            return thread
        log.warning(
            "reset reminder target thread is not messageable",
            extra={
                "reset_id": reminder.reset_id,
                "channel_id": reminder.channel_id,
                "thread_id": reminder.thread_id,
            },
        )
        await _send_ops_log(
            "⚠️ Reset reminder thread is not messageable "
            f"• reset_id={reminder.reset_id} • channel_id={reminder.channel_id} • thread_id={reminder.thread_id}"
        )
        return None

    if isinstance(base_channel, discord.abc.Messageable):
        return base_channel
    log.warning(
        "reset reminder target channel is not messageable",
        extra={"reset_id": reminder.reset_id, "channel_id": reminder.channel_id},
    )
    await _send_ops_log(
        "⚠️ Reset reminder channel is not messageable "
        f"• reset_id={reminder.reset_id} • channel_id={reminder.channel_id}"
    )
    return None


async def _update_next_scheduled_post(
    *,
    tab_name: str,
    header_map: dict[str, int],
    row_number: int,
    reminder_time: dt.datetime,
) -> None:
    worksheet = await aget_worksheet(_sheet_id(), tab_name)
    next_col = _column_label(header_map["next_scheduled_post_utc"])
    await acall_with_backoff(
        worksheet.update,
        f"{next_col}{row_number}",
        [[reminder_time.astimezone(dt.timezone.utc).isoformat()]],
        value_input_option="RAW",
    )


async def _update_row_after_send(
    *,
    tab_name: str,
    header_map: dict[str, int],
    row_number: int,
    reset_time: dt.datetime,
    next_scheduled_post: dt.datetime,
    message_id: int,
) -> None:
    worksheet = await aget_worksheet(_sheet_id(), tab_name)

    sent_col = _column_label(header_map["last_sent_for_reset_utc"])
    next_col = _column_label(header_map["next_scheduled_post_utc"])
    message_col = _column_label(header_map["last_message_id"])

    await acall_with_backoff(
        worksheet.update,
        f"{sent_col}{row_number}",
        [[reset_time.astimezone(dt.timezone.utc).isoformat()]],
        value_input_option="RAW",
    )
    await acall_with_backoff(
        worksheet.update,
        f"{next_col}{row_number}",
        [[next_scheduled_post.astimezone(dt.timezone.utc).isoformat()]],
        value_input_option="RAW",
    )
    await acall_with_backoff(
        worksheet.update,
        f"{message_col}{row_number}",
        [[str(message_id)]],
        value_input_option="RAW",
    )


async def _load_reset_reminder_records_resilient(
    *, active_only: bool
) -> tuple[str, dict[str, int], list[_ResetReminderRecord], bool]:
    last_exc: Exception | None = None
    for attempt in range(1, _LOAD_RETRY_ATTEMPTS + 1):
        started = time.monotonic()
        try:
            tab_name, header_map, records = await _load_reset_reminder_records(
                active_only=active_only
            )
        except TimeoutError as exc:
            last_exc = exc
            log.warning(
                "reset reminder sheet load timed out; retrying",
                extra={
                    "attempt": attempt,
                    "max_attempts": _LOAD_RETRY_ATTEMPTS,
                    "exception_type": type(exc).__name__,
                    "load_duration_ms": round((time.monotonic() - started) * 1000, 1),
                },
            )
            if attempt < _LOAD_RETRY_ATTEMPTS:
                await asyncio.sleep(_LOAD_RETRY_BACKOFF_SEC * attempt)
                continue
            break
        except Exception:
            raise
        _last_successful_load.update(
            {"tab_name": tab_name, "header_map": header_map, "records": records}
        )
        log.info(
            "reset reminder sheet load succeeded",
            extra={
                "attempt": attempt,
                "load_duration_ms": round((time.monotonic() - started) * 1000, 1),
            },
        )
        return tab_name, header_map, records, False

    cached_tab = _last_successful_load.get("tab_name")
    cached_header = _last_successful_load.get("header_map")
    cached_records = _last_successful_load.get("records")
    if (
        cached_tab
        and isinstance(cached_header, dict)
        and all(column in cached_header for column in _REQUIRED_COLUMNS)
        and isinstance(cached_records, list)
    ):
        log.warning(
            "reset reminder sheet load failed after retries; using cached rows",
            extra={
                "exception_type": type(last_exc).__name__ if last_exc else "unknown",
                "cached_rows": len(cached_records),
                "used_cached_rows": True,
            },
        )
        return str(cached_tab), cached_header, cached_records, True
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("reset reminder sheet load failed")


async def process_reset_reminders(
    bot: commands.Bot, *, now: dt.datetime | None = None
) -> None:
    global _next_sheet_load_after_utc
    if not _is_feature_enabled():
        log.debug(
            "reset reminders disabled via feature toggle; skipping scheduler tick"
        )
        return
    if _PROCESS_LOCK.locked():
        log.info("reset reminder processing already running; skipping overlapping tick")
        return
    # In-process lock prevents duplicate sends from overlapping scheduler ticks in one process.
    # Multi-instance deployments sharing the same sheet still need a stronger cross-process guard.
    async with _PROCESS_LOCK:
        is_closed = getattr(bot, "is_closed", None)
        is_ready = getattr(bot, "is_ready", None)
        if callable(is_closed) and is_closed():
            return
        if callable(is_ready) and not is_ready():
            return

        now_utc = _utc_now(now)

        used_cached_rows = False
        used_due_cache = False
        cached_tab = _last_successful_load.get("tab_name")
        cached_header = _last_successful_load.get("header_map")
        cached_records = _last_successful_load.get("records")
        if (
            _next_sheet_load_after_utc is not None
            and now_utc < _next_sheet_load_after_utc
            and cached_tab
            and isinstance(cached_header, dict)
            and isinstance(cached_records, list)
        ):
            tab_name = str(cached_tab)
            header_map = cached_header
            records = cached_records
            used_cached_rows = True
            used_due_cache = True
            log.debug(
                "reset reminder scheduler using cached rows before next due load",
                extra={
                    "cached_rows": len(records),
                    "next_sheet_load_after_utc": _next_sheet_load_after_utc.isoformat(),
                },
            )
        else:
            try:
                tab_name, header_map, records, used_cached_rows = (
                    await _load_reset_reminder_records_resilient(active_only=True)
                )
            except Exception as exc:
                await _record_load_failure(exc)
                return
            _set_next_sheet_load_after_from_records(records, now_utc)

        if used_cached_rows and not used_due_cache:
            synthetic = TimeoutError(
                "reset reminder sheet load failed; cached rows used"
            )
            setattr(synthetic, "reset_reminder_stage", "sheet_fetch")
            setattr(synthetic, "reset_reminder_tab", tab_name)
            await _record_load_failure(synthetic)
            log.info(
                "reset reminder scheduler continuing with cached rows",
                extra={
                    "cached_rows": len(records),
                    "consecutive_failed_ticks": _load_failure_state.get("failures"),
                },
            )
        elif not used_due_cache:
            await _record_load_success()

        if not records:
            return

        for record in records:
            reminder = record.reminder

            if reminder.cycle_days <= 0:
                log.warning(
                    "reset reminder skipped; cycle_days must be > 0",
                    extra={"reset_id": reminder.reset_id},
                )
                continue

            if reminder.role_id <= 0 or reminder.channel_id <= 0:
                log.warning(
                    "reset reminder skipped; missing role/channel",
                    extra={
                        "reset_id": reminder.reset_id,
                        "role_id": reminder.role_id,
                        "channel_id": reminder.channel_id,
                    },
                )
                continue

            try:
                if reminder.next_scheduled_post_utc is None:
                    reminder_time = _next_reminder_from_anchor(
                        reminder.reference_date_utc,
                        reminder.cycle_days,
                        reminder.lead_minutes,
                        now_utc,
                    )
                    await _update_next_scheduled_post(
                        tab_name=tab_name,
                        header_map=header_map,
                        row_number=record.row_number,
                        reminder_time=reminder_time,
                    )
                    updated_reminder = replace(
                        reminder,
                        next_scheduled_post_utc=reminder_time,
                    )
                    records = _replace_cached_record(
                        records,
                        record.row_number,
                        updated_reminder,
                        now_utc=now_utc,
                    )
                    # First write the authoritative schedule back to the sheet. A
                    # later tick will send only after that persisted time is due.
                    continue

                reminder_time = reminder.next_scheduled_post_utc
                reset_time = _reset_time_for_scheduled_post(
                    reminder_time, reminder.lead_minutes
                )
            except Exception:
                log.exception(
                    "reset reminder skipped; failed to compute cycle",
                    extra={"reset_id": reminder.reset_id},
                )
                continue

            if now_utc < reminder_time:
                continue

            if now_utc >= reset_time:
                advanced_reminder_time = _advance_reminder_until_reset_future(
                    reminder_time,
                    cycle_days=reminder.cycle_days,
                    lead_minutes=reminder.lead_minutes,
                    now_utc=now_utc,
                )
                log.warning(
                    "stale reset reminder skipped; next scheduled post advanced",
                    extra={
                        "reset_id": reminder.reset_id,
                        "row_number": record.row_number,
                        "stale_reminder_time": reminder_time.isoformat(),
                        "stale_reset_time": reset_time.isoformat(),
                        "advanced_reminder_time": advanced_reminder_time.isoformat(),
                        "now_utc": now_utc.isoformat(),
                    },
                )
                try:
                    await _update_next_scheduled_post(
                        tab_name=tab_name,
                        header_map=header_map,
                        row_number=record.row_number,
                        reminder_time=advanced_reminder_time,
                    )
                    updated_reminder = replace(
                        reminder,
                        next_scheduled_post_utc=advanced_reminder_time,
                    )
                    records = _replace_cached_record(
                        records,
                        record.row_number,
                        updated_reminder,
                        now_utc=now_utc,
                    )
                except Exception:
                    log.exception(
                        "reset reminder next scheduled update failed",
                        extra={
                            "reset_id": reminder.reset_id,
                            "row_number": record.row_number,
                        },
                    )
                continue

            following_reminder_time = _following_reminder_time(
                reminder_time, reminder.cycle_days
            )
            if reminder.last_sent_for_reset_utc == reset_time:
                if reminder.next_scheduled_post_utc != following_reminder_time:
                    try:
                        await _update_next_scheduled_post(
                            tab_name=tab_name,
                            header_map=header_map,
                            row_number=record.row_number,
                            reminder_time=following_reminder_time,
                        )
                        updated_reminder = replace(
                            reminder,
                            next_scheduled_post_utc=following_reminder_time,
                        )
                        records = _replace_cached_record(
                            records,
                            record.row_number,
                            updated_reminder,
                            now_utc=now_utc,
                        )
                    except Exception:
                        log.exception(
                            "reset reminder next scheduled update failed",
                            extra={
                                "reset_id": reminder.reset_id,
                                "row_number": record.row_number,
                            },
                        )
                continue

            target = await _resolve_target_channel(bot, reminder)
            if target is None:
                log.warning(
                    "reset reminder skipped; target channel/thread not found",
                    extra={
                        "reset_id": reminder.reset_id,
                        "channel_id": reminder.channel_id,
                        "thread_id": reminder.thread_id,
                    },
                )
                await _send_ops_log(
                    "⚠️ Reset reminder target unavailable "
                    f"• reset_id={reminder.reset_id} • channel_id={reminder.channel_id} • thread_id={reminder.thread_id or '-'}"
                )
                continue

            if reminder.last_message_id:
                try:
                    old_message = await target.fetch_message(reminder.last_message_id)
                    await old_message.delete()
                except Exception:
                    log.debug(
                        "failed to delete old reset reminder",
                        extra={
                            "reset_id": reminder.reset_id,
                            "last_message_id": reminder.last_message_id,
                        },
                    )

            embed = discord.Embed(
                title=reminder.embed_title or reminder.label,
                description=_next_reset_description(
                    reminder.embed_description, reset_time
                ),
                color=get_embed_colour("community"),
            )
            embed.set_footer(
                text=_reset_reminder_footer(
                    reminder.embed_footer,
                    reset_time,
                    reminder.cycle_days,
                )
            )

            view = ResetReminderView(
                role_id=reminder.role_id,
                label_opt_in=reminder.button_label_opt_in,
                label_opt_out=reminder.button_label_opt_out,
            )

            try:
                icon_file = await _reset_image_to_file(target, reminder)
                files = [icon_file] if icon_file else []
                message = await target.send(
                    content=_message_content_for_reminder(reminder),
                    files=files,
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions(
                        everyone=False,
                        roles=True,
                        users=False,
                    ),
                )
            except Exception as exc:
                log.exception(
                    "reset reminder send failed",
                    extra={
                        "reset_id": reminder.reset_id,
                        "channel_id": reminder.channel_id,
                        "thread_id": reminder.thread_id,
                        "reset_time": reset_time.isoformat(),
                    },
                )
                await _send_ops_log(
                    "⚠️ Reset reminder send failed "
                    f"• reset_id={reminder.reset_id} • channel_id={reminder.channel_id} "
                    f"• thread_id={reminder.thread_id or '-'} • reset_time={reset_time.isoformat()} "
                    f"• error={type(exc).__name__}"
                )
                continue

            following_reminder_time = _following_reminder_time(
                reminder_time, reminder.cycle_days
            )
            records = _record_reset_reminder_discord_send_in_memory(
                records,
                record,
                reset_time=reset_time,
                following_reminder_time=following_reminder_time,
                message_id=message.id,
                now_utc=now_utc,
            )
            try:
                await _update_row_after_send(
                    tab_name=tab_name,
                    header_map=header_map,
                    row_number=record.row_number,
                    reset_time=reset_time,
                    next_scheduled_post=following_reminder_time,
                    message_id=message.id,
                )
            except Exception as exc:
                log.exception(
                    "reset reminder Discord send succeeded but sheet update failed",
                    extra={
                        "reset_id": reminder.reset_id,
                        "row_number": record.row_number,
                        "reset_time": reset_time.isoformat(),
                        "next_scheduled_post": following_reminder_time.isoformat(),
                        "message_id": getattr(message, "id", None),
                    },
                )
                await _send_ops_log(
                    "⚠️ Reset reminder posted but sheet update failed "
                    f"• reset_id={reminder.reset_id} • row={record.row_number} "
                    f"• reset_time={reset_time.isoformat()} • message_id={getattr(message, 'id', '-')} "
                    f"• error={type(exc).__name__} • action=manual_sheet_reconcile"
                )


def _earliest_cached_due() -> dt.datetime | None:
    records = _last_successful_load.get("records")
    if not isinstance(records, list):
        return None
    values = [
        record.reminder.next_scheduled_post_utc
        for record in records
        if record.reminder.next_scheduled_post_utc is not None
    ]
    return min(values) if values else None


async def reconcile_reset_reminder_jobs(runtime: "Runtime") -> None:
    """Load active rows, fill missing due values, and arm the earliest due job."""
    global _next_sheet_load_after_utc
    _next_sheet_load_after_utc = None
    await process_reset_reminders(runtime.bot)
    due = _earliest_cached_due()
    job = runtime.scheduler.at(
        due,
        tag="community",
        component="community",
        name="reset_reminders",
        cadence_label="next reset reminder",
    )

    async def _runner() -> None:
        try:
            await process_reset_reminders(runtime.bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            # _DueJob clears next_run before invoking the owner. Preserve a
            # bounded retry when an unexpected failure bypasses normal cache
            # reconciliation instead of leaving this job silently disarmed.
            job.reschedule(_utc_now() + _DUE_JOB_RETRY_DELAY)
            raise
        now_utc = _utc_now()
        next_due = _earliest_cached_due()
        if next_due is not None and next_due <= now_utc:
            next_due = now_utc + _DUE_JOB_RETRY_DELAY
        job.reschedule(next_due)

    job.do(_runner)


def schedule_reset_reminder_jobs(runtime: "Runtime") -> None:
    if not _is_feature_enabled():
        record_skip = getattr(runtime, "_record_scheduler_skip", None)
        if callable(record_skip):
            record_skip("reset_reminders", "feature toggle is disabled")
        log.info(
            "reset reminders disabled via feature toggle; scheduler job not registered"
        )
        return
    if any(
        getattr(job, "name", None) == "reset_reminders_reconcile"
        for job in runtime.scheduler.jobs
    ):
        log.info("reset reminder scheduler already registered; skipping duplicate job")
        return
    reconcile = runtime.scheduler.every(
        hours=24,
        tag="community",
        component="community",
        name="reset_reminders_reconcile",
    )
    reconcile.cadence_label = "daily reconcile"
    reconcile.do(lambda: reconcile_reset_reminder_jobs(runtime))
    runtime.scheduler.spawn(
        reconcile_reset_reminder_jobs(runtime), name="reset_reminders_initial_reconcile"
    )


__all__ = [
    "ResetReminder",
    "ResetReminderView",
    "process_reset_reminders",
    "register_persistent_reset_views",
    "schedule_reset_reminder_jobs",
    "reconcile_reset_reminder_jobs",
    "_load_failure_state",
    "_last_successful_load",
]
