from __future__ import annotations

import datetime as dt
import logging
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import commands

from modules.common.embeds import get_embed_colour
from modules.community.reset_reminders.models import ResetReminder
from modules.community.reset_reminders.views import ResetReminderView
from shared.config import cfg, get_milestones_sheet_id
from shared.sheets.async_core import acall_with_backoff, afetch_values, aget_worksheet

if TYPE_CHECKING:
    from modules.common.runtime import Runtime

log = logging.getLogger("c1c.community.reset_reminders.scheduler")

_RESET_REMINDER_TAB_KEY = "RESET_REMINDER_TAB"
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
    "last_message_id",
)


@dataclass(frozen=True, slots=True)
class _ResetReminderRecord:
    row_number: int
    reminder: ResetReminder


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
    tab_name = _tab_name()
    matrix = await afetch_values(_sheet_id(), tab_name)
    if not matrix:
        return tab_name, {}, []

    header_map = _resolve_header_map(matrix[0])
    records: list[_ResetReminderRecord] = []

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
                last_message_id=_parse_optional_int(_cell(row, header_map["last_message_id"])),
            )
            records.append(_ResetReminderRecord(row_number=row_number, reminder=reminder))
        except Exception:
            log.exception("invalid reset reminder row skipped", extra={"tab": tab_name, "row_number": row_number})
            continue

    return tab_name, header_map, records


def _utc_now(now: dt.datetime | None = None) -> dt.datetime:
    if now is None:
        return dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(dt.timezone.utc)


def _next_reset(reference_date_utc: dt.datetime, cycle_days: int, now_utc: dt.datetime) -> dt.datetime:
    cycle = dt.timedelta(days=cycle_days)
    delta = now_utc - reference_date_utc
    cycles_passed = math.floor(delta / cycle)
    return reference_date_utc + (cycles_passed + 1) * cycle


async def register_persistent_reset_views(bot: commands.Bot) -> None:
    _, _, records = await _load_reset_reminder_records(active_only=True)
    for record in records:
        reminder = record.reminder
        bot.add_view(
            ResetReminderView(
                role_id=reminder.role_id,
                label_opt_in=reminder.button_label_opt_in,
                label_opt_out=reminder.button_label_opt_out,
            )
        )


async def _resolve_target_channel(bot: commands.Bot, reminder: ResetReminder) -> discord.abc.Messageable | None:
    base_channel = bot.get_channel(reminder.channel_id)
    if base_channel is None:
        try:
            base_channel = await bot.fetch_channel(reminder.channel_id)
        except Exception:
            return None

    if reminder.thread_id:
        thread = bot.get_channel(reminder.thread_id)
        if thread is None:
            try:
                thread = await bot.fetch_channel(reminder.thread_id)
            except Exception:
                return None
        if isinstance(thread, discord.abc.Messageable):
            return thread
        return None

    if isinstance(base_channel, discord.abc.Messageable):
        return base_channel
    return None


async def _update_row_after_send(
    *,
    tab_name: str,
    header_map: dict[str, int],
    row_number: int,
    next_reset: dt.datetime,
    message_id: int,
) -> None:
    worksheet = await aget_worksheet(_sheet_id(), tab_name)

    sent_col = _column_label(header_map["last_sent_for_reset_utc"])
    message_col = _column_label(header_map["last_message_id"])

    await acall_with_backoff(
        worksheet.update,
        f"{sent_col}{row_number}:{message_col}{row_number}",
        [[next_reset.astimezone(dt.timezone.utc).isoformat(), str(message_id)]],
        value_input_option="RAW",
    )


async def process_reset_reminders(bot: commands.Bot, *, now: dt.datetime | None = None) -> None:
    is_closed = getattr(bot, "is_closed", None)
    is_ready = getattr(bot, "is_ready", None)
    if callable(is_closed) and is_closed():
        return
    if callable(is_ready) and not is_ready():
        return

    now_utc = _utc_now(now)

    try:
        tab_name, header_map, records = await _load_reset_reminder_records(active_only=True)
    except Exception:
        log.exception("failed to load reset reminders")
        return

    if not records:
        return

    for record in records:
        reminder = record.reminder

        if reminder.cycle_days <= 0:
            log.warning("reset reminder skipped; cycle_days must be > 0", extra={"reset_id": reminder.reset_id})
            continue

        if reminder.reference_date_utc > now_utc:
            continue

        if reminder.role_id <= 0 or reminder.channel_id <= 0:
            log.warning(
                "reset reminder skipped; missing role/channel",
                extra={"reset_id": reminder.reset_id, "role_id": reminder.role_id, "channel_id": reminder.channel_id},
            )
            continue

        try:
            next_reset = _next_reset(reminder.reference_date_utc, reminder.cycle_days, now_utc)
        except Exception:
            log.exception("reset reminder skipped; failed to compute cycle", extra={"reset_id": reminder.reset_id})
            continue

        trigger_time = next_reset - dt.timedelta(minutes=reminder.lead_minutes)
        if now_utc < trigger_time:
            continue

        if reminder.last_sent_for_reset_utc == next_reset:
            continue

        target = await _resolve_target_channel(bot, reminder)
        if target is None:
            log.warning("reset reminder skipped; target channel not found", extra={"reset_id": reminder.reset_id})
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
            description=reminder.embed_description,
            color=get_embed_colour("community"),
            timestamp=next_reset,
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
                content=f"<@&{reminder.role_id}>",
                embed=embed,
                view=view,
            )
        except Exception:
            log.exception("reset reminder send failed", extra={"reset_id": reminder.reset_id})
            continue

        try:
            await _update_row_after_send(
                tab_name=tab_name,
                header_map=header_map,
                row_number=record.row_number,
                next_reset=next_reset,
                message_id=message.id,
            )
        except Exception:
            log.exception("reset reminder sheet update failed", extra={"reset_id": reminder.reset_id})


def schedule_reset_reminder_jobs(runtime: "Runtime") -> None:
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
]
