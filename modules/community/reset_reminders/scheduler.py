from __future__ import annotations

import datetime as dt
import logging
import math
import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from modules.common.embeds import get_embed_colour
from modules.common import feature_flags
from modules.common import runtime as rt
from modules.community.reset_reminders.models import ResetReminder
from modules.community.reset_reminders.views import ResetReminderView
from shared.config import cfg, get_milestones_sheet_id
from shared.dedupe import EventDeduper
from shared.sheets.async_core import acall_with_backoff, afetch_values, aget_worksheet

if TYPE_CHECKING:
    from modules.common.runtime import Runtime

log = logging.getLogger("c1c.community.reset_reminders.scheduler")

_RESET_REMINDER_TAB_KEY = "RESET_REMINDER_TAB"
_FEATURE_TOGGLE_KEY = "reset_reminders"
_INVALID_ROW_ALERT_DEDUPER = EventDeduper(window_s=900.0, max_keys=128)
_LOAD_FAILURE_ALERT_COOLDOWN_SEC = 1800.0
_load_failure_state: dict[str, Any] = {"key": None, "last_alert": 0.0, "failures": 0}
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
)


@dataclass(frozen=True, slots=True)
class _ResetReminderRecord:
    row_number: int
    reminder: ResetReminder


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


def _tab_name() -> str:
    tab_name = str(cfg.get(_RESET_REMINDER_TAB_KEY) or "").strip()
    if not tab_name:
        raise RuntimeError(f"{_RESET_REMINDER_TAB_KEY} missing in milestones Config tab")
    return tab_name


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


async def _load_reset_reminder_records(*, active_only: bool) -> tuple[str, dict[str, int], list[_ResetReminderRecord]]:
    stage = "config_load"
    started = time.monotonic()
    tab_name = "unknown"
    try:
        tab_name = _tab_name()
        sheet_id = _sheet_id()
        stage = "sheet_fetch"
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
        log.exception(
            "reset reminder load failed",
            extra={
                "scheduler": "reset_reminders",
                "config_key": _RESET_REMINDER_TAB_KEY,
                "tab": tab_name,
                "operation": stage,
                "timeout_type": type(exc).__name__ if isinstance(exc, TimeoutError) else "",
                "elapsed_s": round(elapsed, 3),
            },
        )
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
                reference_date_utc=_parse_dt(_cell(row, header_map["reference_date_utc"])),
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
                last_sent_for_reset_utc=_parse_dt_optional(_cell(row, header_map["last_sent_for_reset_utc"])),
                next_scheduled_post_utc=_parse_dt_optional(_cell(row, header_map["next_scheduled_post_utc"])),
                last_message_id=_parse_optional_int(_cell(row, header_map["last_message_id"])),
                emoji_name_or_id=_cell(row, header_map["emojinameorid"]) if "emojinameorid" in header_map else None,
            )
            records.append(_ResetReminderRecord(row_number=row_number, reminder=reminder))
        except Exception as exc:
            reason = type(exc).__name__
            invalid_rows.append(
                {
                    "row_number": row_number,
                    "reset_id": _cell(row, header_map.get("reset_id", -1)),
                    "reason": reason,
                }
            )
            log.exception("invalid reset reminder row skipped", extra={"tab": tab_name, "row_number": row_number})
            continue

    valid_active_count = len(records) if active_only else sum(1 for r in records if r.reminder.status.lower() == "active")
    log.info(
        "reset reminder rows loaded",
        extra={
            "tab": tab_name,
            "active_only": active_only,
            "valid_active_rows": valid_active_count,
            "invalid_rows": len(invalid_rows),
            "invalid_row_details": invalid_rows,
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
                log.warning("failed to send reset reminder invalid-row ops alert", exc_info=True)

    return tab_name, header_map, records


def _next_reset_description(base_description: str, reset_time_utc: dt.datetime | None) -> str:
    if reset_time_utc is None:
        return base_description
    unix_seconds = int(reset_time_utc.astimezone(dt.timezone.utc).timestamp())
    countdown = f"Next reset: <t:{unix_seconds}:F>\nTime left: <t:{unix_seconds}:R>"
    base = str(base_description or "").rstrip()
    return f"{base}\n\n{countdown}" if base else countdown


def _resolve_custom_emoji(target: discord.abc.Messageable | None, value: str | None) -> discord.Emoji | None:
    text = str(value or "").strip()
    if not text:
        return None

    guild = getattr(target, "guild", None)
    if guild is None:
        return None

    emojis = getattr(guild, "emojis", []) or []
    if text.isdigit():
        wanted_id = int(text)
        for emoji in emojis:
            if getattr(emoji, "id", None) == wanted_id:
                return emoji
        return None

    for emoji in emojis:
        if getattr(emoji, "name", None) == text:
            return emoji
    return None


def _message_content_for_reminder(target: discord.abc.Messageable | None, reminder: ResetReminder) -> str:
    role_mention = f"<@&{reminder.role_id}>"
    configured = str(reminder.emoji_name_or_id or "").strip()
    if not configured:
        return role_mention

    if getattr(target, "guild", None) is None:
        log.warning(
            "reset reminder emoji could not be resolved; target guild unavailable",
            extra={"reset_id": reminder.reset_id, "emoji_name_or_id": configured},
        )
        return role_mention

    emoji = _resolve_custom_emoji(target, configured)
    if emoji is None:
        log.warning(
            "reset reminder emoji could not be resolved",
            extra={"reset_id": reminder.reset_id, "emoji_name_or_id": configured},
        )
        return role_mention
    return f"{emoji} {role_mention}"


def _utc_now(now: dt.datetime | None = None) -> dt.datetime:
    if now is None:
        return dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(dt.timezone.utc)


def _reminder_time(reset_time_utc: dt.datetime, lead_minutes: int) -> dt.datetime:
    return reset_time_utc - dt.timedelta(minutes=lead_minutes)


def _reset_cycle_for_reminder(
    reference_date_utc: dt.datetime,
    cycle_days: int,
    lead_minutes: int,
    now_utc: dt.datetime,
) -> dt.datetime:
    cycle = dt.timedelta(days=cycle_days)
    first_reminder_time = _reminder_time(reference_date_utc, lead_minutes)
    if now_utc < first_reminder_time:
        return reference_date_utc
    cycles_passed = math.floor((now_utc - first_reminder_time) / cycle)
    return reference_date_utc + cycles_passed * cycle


def _following_reset(reset_time_utc: dt.datetime, cycle_days: int) -> dt.datetime:
    return reset_time_utc + dt.timedelta(days=cycle_days)


async def register_persistent_reset_views(bot: commands.Bot) -> None:
    if not _is_feature_enabled():
        log.info("reset reminders disabled via feature toggle; skipping persistent view registration")
        return
    try:
        _, _, records = await _load_reset_reminder_records(active_only=True)
    except Exception:
        log.exception("failed to load reset reminders for persistent views; skipping reset reminder views")
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
    return f"{type(exc).__name__}:{stage}:{tab}"


async def _record_load_failure(exc: Exception) -> None:
    state = _load_failure_state
    key = _load_failure_key(exc)
    now = time.monotonic()
    previous_key = state.get("key")
    state["key"] = key
    state["failures"] = (int(state.get("failures") or 0) + 1) if previous_key == key else 1

    stage = getattr(exc, "reset_reminder_stage", "unknown")
    tab = getattr(exc, "reset_reminder_tab", "unknown")
    elapsed = getattr(exc, "reset_reminder_elapsed", None)
    log.exception(
        "failed to load reset reminders; scheduler tick skipped",
        extra={
            "scheduler": "reset_reminders",
            "config_key": _RESET_REMINDER_TAB_KEY,
            "tab": tab,
            "operation": stage,
            "timeout_type": type(exc).__name__ if isinstance(exc, TimeoutError) else "",
            "elapsed_s": round(float(elapsed), 3) if isinstance(elapsed, (int, float)) else None,
            "failure_count": state["failures"],
        },
    )

    last_alert = float(state.get("last_alert") or 0.0)
    if previous_key != key or now - last_alert >= _LOAD_FAILURE_ALERT_COOLDOWN_SEC:
        state["last_alert"] = now
        await _send_ops_log(
            "⚠️ Reset reminders failed to load; scheduler tick skipped. "
            f"See app logs. error={type(exc).__name__}"
        )


async def _record_load_success() -> None:
    failures = int(_load_failure_state.get("failures") or 0)
    if failures > 0:
        await _send_ops_log(f"✅ Reset reminders loaded again after {failures} failed tick(s).")
    _load_failure_state.update({"key": None, "last_alert": 0.0, "failures": 0})


async def _resolve_target_channel(bot: commands.Bot, reminder: ResetReminder) -> discord.abc.Messageable | None:
    base_channel = bot.get_channel(reminder.channel_id)
    if base_channel is None:
        try:
            base_channel = await bot.fetch_channel(reminder.channel_id)
        except Exception as exc:
            log.exception(
                "reset reminder target channel fetch failed",
                extra={"reset_id": reminder.reset_id, "channel_id": reminder.channel_id},
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
            extra={"reset_id": reminder.reset_id, "channel_id": reminder.channel_id, "thread_id": reminder.thread_id},
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


async def process_reset_reminders(bot: commands.Bot, *, now: dt.datetime | None = None) -> None:
    if not _is_feature_enabled():
        log.debug("reset reminders disabled via feature toggle; skipping scheduler tick")
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

        try:
            tab_name, header_map, records = await _load_reset_reminder_records(active_only=True)
        except Exception as exc:
            await _record_load_failure(exc)
            return
        await _record_load_success()

        if not records:
            return

        for record in records:
            reminder = record.reminder

            if reminder.cycle_days <= 0:
                log.warning("reset reminder skipped; cycle_days must be > 0", extra={"reset_id": reminder.reset_id})
                continue

            if reminder.role_id <= 0 or reminder.channel_id <= 0:
                log.warning(
                    "reset reminder skipped; missing role/channel",
                    extra={"reset_id": reminder.reset_id, "role_id": reminder.role_id, "channel_id": reminder.channel_id},
                )
                continue

            try:
                reset_time = _reset_cycle_for_reminder(
                    reminder.reference_date_utc,
                    reminder.cycle_days,
                    reminder.lead_minutes,
                    now_utc,
                )
                reminder_time = _reminder_time(reset_time, reminder.lead_minutes)
            except Exception:
                log.exception("reset reminder skipped; failed to compute cycle", extra={"reset_id": reminder.reset_id})
                continue

            if reminder.last_sent_for_reset_utc == reset_time:
                following_reminder_time = _reminder_time(
                    _following_reset(reset_time, reminder.cycle_days),
                    reminder.lead_minutes,
                )
                if reminder.next_scheduled_post_utc != following_reminder_time:
                    try:
                        await _update_next_scheduled_post(
                            tab_name=tab_name,
                            header_map=header_map,
                            row_number=record.row_number,
                            reminder_time=following_reminder_time,
                        )
                    except Exception:
                        log.exception(
                            "reset reminder next scheduled update failed",
                            extra={"reset_id": reminder.reset_id, "row_number": record.row_number},
                        )
                continue

            if reminder.next_scheduled_post_utc != reminder_time:
                try:
                    await _update_next_scheduled_post(
                        tab_name=tab_name,
                        header_map=header_map,
                        row_number=record.row_number,
                        reminder_time=reminder_time,
                    )
                except Exception:
                    log.exception(
                        "reset reminder next scheduled update failed",
                        extra={"reset_id": reminder.reset_id, "row_number": record.row_number},
                    )

            if now_utc < reminder_time:
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
                        extra={"reset_id": reminder.reset_id, "last_message_id": reminder.last_message_id},
                    )

            embed = discord.Embed(
                title=reminder.embed_title or reminder.label,
                description=_next_reset_description(reminder.embed_description, reset_time),
                color=get_embed_colour("community"),
                timestamp=reset_time,
            )
            if reminder.embed_footer:
                embed.set_footer(text=reminder.embed_footer)

            view = ResetReminderView(
                role_id=reminder.role_id,
                label_opt_in=reminder.button_label_opt_in,
                label_opt_out=reminder.button_label_opt_out,
            )

            try:
                message = await target.send(
                    content=_message_content_for_reminder(target, reminder),
                    embed=embed,
                    view=view,
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

            following_reset = _following_reset(reset_time, reminder.cycle_days)
            following_reminder_time = _reminder_time(following_reset, reminder.lead_minutes)
            try:
                await _update_row_after_send(
                    tab_name=tab_name,
                    header_map=header_map,
                    row_number=record.row_number,
                    reset_time=reset_time,
                    next_scheduled_post=following_reminder_time,
                    message_id=message.id,
                )
            except Exception:
                log.exception("reset reminder sheet update failed", extra={"reset_id": reminder.reset_id})


def schedule_reset_reminder_jobs(runtime: "Runtime") -> None:
    if not _is_feature_enabled():
        log.info("reset reminders disabled via feature toggle; scheduler job not registered")
        return
    if any(getattr(job, "name", None) == "reset_reminders" for job in runtime.scheduler.jobs):
        log.info("reset reminder scheduler already registered; skipping duplicate job")
        return

    job = runtime.scheduler.every(minutes=1.0, tag="community", name="reset_reminders")

    async def _runner() -> None:
        await process_reset_reminders(runtime.bot)

    job.do(_runner)


__all__ = [
    "ResetReminder",
    "ResetReminderView",
    "process_reset_reminders",
    "register_persistent_reset_views",
    "schedule_reset_reminder_jobs",
    "_load_failure_state",
]
