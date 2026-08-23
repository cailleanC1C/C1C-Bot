from __future__ import annotations

import datetime as dt
import logging
from types import ModuleType
from typing import Any

import discord

from modules.common.embeds import get_embed_colour
from modules.community.reset_reminders.views import ResetReminderView

log = logging.getLogger("c1c.community.reset_reminders.panel_rollover")

_INSTALLED_ATTR = "_panel_rollover_installed"
_COMPLETED_ATTR = "_panel_rollover_completed"
_ORIGINAL_PROCESS_ATTR = "_panel_rollover_original_process"
_ORIGINAL_EARLIEST_ATTR = "_panel_rollover_original_earliest_due"


def _rollover_key(reminder: Any) -> tuple[str, int, int] | None:
    last_sent = getattr(reminder, "last_sent_for_reset_utc", None)
    message_id = getattr(reminder, "last_message_id", None)
    if last_sent is None or not message_id:
        return None
    return (
        str(getattr(reminder, "reset_id", "") or ""),
        int(last_sent.astimezone(dt.timezone.utc).timestamp()),
        int(message_id),
    )


def _first_future_reset(
    displayed_reset_utc: dt.datetime,
    *,
    cycle_days: int,
    now_utc: dt.datetime,
) -> dt.datetime:
    displayed = displayed_reset_utc.astimezone(dt.timezone.utc)
    if displayed > now_utc:
        return displayed
    cycle = dt.timedelta(days=cycle_days)
    elapsed = now_utc - displayed
    cycles_ahead = elapsed // cycle + 1
    return displayed + cycles_ahead * cycle


def _message_matches_reset(
    scheduler: ModuleType,
    message: Any,
    reminder: Any,
    reset_time_utc: dt.datetime,
) -> bool:
    expected_title = reminder.embed_title or reminder.label
    expected_description = scheduler._next_reset_description(
        reminder.embed_description, reset_time_utc
    )
    expected_footer = scheduler._reset_reminder_footer(
        reminder.embed_footer,
        reset_time_utc,
        reminder.cycle_days,
    )
    for embed in getattr(message, "embeds", []) or []:
        footer = getattr(getattr(embed, "footer", None), "text", None)
        if (
            getattr(embed, "title", None) == expected_title
            and getattr(embed, "description", None) == expected_description
            and footer == expected_footer
        ):
            return True
    return False


def _build_rollover_embed(
    scheduler: ModuleType,
    reminder: Any,
    reset_time_utc: dt.datetime,
) -> discord.Embed:
    embed = discord.Embed(
        title=reminder.embed_title or reminder.label,
        description=scheduler._next_reset_description(
            reminder.embed_description, reset_time_utc
        ),
        color=get_embed_colour("community"),
    )
    embed.set_footer(
        text=scheduler._reset_reminder_footer(
            reminder.embed_footer,
            reset_time_utc,
            reminder.cycle_days,
        )
    )
    return embed


def _completed_keys(scheduler: ModuleType) -> set[tuple[str, int, int]]:
    completed = getattr(scheduler, _COMPLETED_ATTR, None)
    if not isinstance(completed, set):
        completed = set()
        setattr(scheduler, _COMPLETED_ATTR, completed)
    return completed


def _prune_completed_keys(scheduler: ModuleType, records: list[Any]) -> None:
    live_keys = {
        key
        for record in records
        if (key := _rollover_key(record.reminder)) is not None
    }
    completed = _completed_keys(scheduler)
    completed.intersection_update(live_keys)


async def _rollover_due_panels(
    scheduler: ModuleType,
    bot: Any,
    *,
    now_utc: dt.datetime,
) -> None:
    records = scheduler._last_successful_load.get("records")
    if not isinstance(records, list) or not records:
        return

    _prune_completed_keys(scheduler, records)
    completed = _completed_keys(scheduler)

    for record in records:
        reminder = record.reminder
        key = _rollover_key(reminder)
        if key is None or key in completed or reminder.cycle_days <= 0:
            continue

        displayed_reset = reminder.last_sent_for_reset_utc.astimezone(dt.timezone.utc)
        if now_utc < displayed_reset:
            continue

        target_reset = _first_future_reset(
            displayed_reset,
            cycle_days=reminder.cycle_days,
            now_utc=now_utc,
        )
        target = await scheduler._resolve_target_channel(bot, reminder)
        if target is None:
            continue

        try:
            message = await target.fetch_message(reminder.last_message_id)
        except discord.NotFound:
            completed.add(key)
            log.info(
                "reset reminder standing panel no longer exists; rollover marked complete",
                extra={
                    "reset_id": reminder.reset_id,
                    "last_message_id": reminder.last_message_id,
                },
            )
            continue
        except Exception as exc:
            log.exception(
                "reset reminder standing panel fetch failed",
                extra={
                    "reset_id": reminder.reset_id,
                    "last_message_id": reminder.last_message_id,
                },
            )
            try:
                await scheduler._send_ops_log(
                    "⚠️ Reset reminder panel rollover fetch failed "
                    f"• reset_id={reminder.reset_id} • message_id={reminder.last_message_id} "
                    f"• error={type(exc).__name__}"
                )
            except Exception:
                pass
            continue

        if _message_matches_reset(scheduler, message, reminder, target_reset):
            completed.add(key)
            continue

        embed = _build_rollover_embed(scheduler, reminder, target_reset)
        view = ResetReminderView(
            role_id=reminder.role_id,
            label_opt_in=reminder.button_label_opt_in,
            label_opt_out=reminder.button_label_opt_out,
        )
        try:
            # Intentionally edit only the embed/view. The existing role mention and
            # icon attachment stay untouched, and editing does not create a new ping.
            await message.edit(embed=embed, view=view)
        except Exception as exc:
            log.exception(
                "reset reminder standing panel rollover edit failed",
                extra={
                    "reset_id": reminder.reset_id,
                    "last_message_id": reminder.last_message_id,
                    "target_reset": target_reset.isoformat(),
                },
            )
            try:
                await scheduler._send_ops_log(
                    "⚠️ Reset reminder panel rollover edit failed "
                    f"• reset_id={reminder.reset_id} • message_id={reminder.last_message_id} "
                    f"• target_reset={target_reset.isoformat()} • error={type(exc).__name__}"
                )
            except Exception:
                pass
            continue

        completed.add(key)
        log.info(
            "reset reminder standing panel rolled forward",
            extra={
                "reset_id": reminder.reset_id,
                "last_message_id": reminder.last_message_id,
                "display_reset": target_reset.isoformat(),
            },
        )


def install_reset_reminder_panel_rollover(scheduler: ModuleType) -> None:
    """Extend reset-reminder scheduling with an in-place post-reset panel rollover."""

    if getattr(scheduler, _INSTALLED_ATTR, False):
        return

    original_process = scheduler.process_reset_reminders
    original_earliest_due = scheduler._earliest_cached_due
    setattr(scheduler, _ORIGINAL_PROCESS_ATTR, original_process)
    setattr(scheduler, _ORIGINAL_EARLIEST_ATTR, original_earliest_due)
    setattr(scheduler, _COMPLETED_ATTR, set())

    async def process_reset_reminders_with_panel_rollover(
        bot: Any, *, now: dt.datetime | None = None
    ) -> None:
        await original_process(bot, now=now)

        if not scheduler._is_feature_enabled():
            return
        if scheduler._PROCESS_LOCK.locked():
            return
        is_closed = getattr(bot, "is_closed", None)
        is_ready = getattr(bot, "is_ready", None)
        if callable(is_closed) and is_closed():
            return
        if callable(is_ready) and not is_ready():
            return

        await _rollover_due_panels(
            scheduler,
            bot,
            now_utc=scheduler._utc_now(now),
        )

    def earliest_cached_due_with_panel_rollover() -> dt.datetime | None:
        values: list[dt.datetime] = []
        existing_due = original_earliest_due()
        if existing_due is not None:
            values.append(existing_due)

        records = scheduler._last_successful_load.get("records")
        if isinstance(records, list):
            _prune_completed_keys(scheduler, records)
            completed = _completed_keys(scheduler)
            for record in records:
                reminder = record.reminder
                key = _rollover_key(reminder)
                if (
                    key is not None
                    and key not in completed
                    and reminder.cycle_days > 0
                ):
                    values.append(
                        reminder.last_sent_for_reset_utc.astimezone(dt.timezone.utc)
                    )

        return min(values) if values else None

    scheduler.process_reset_reminders = process_reset_reminders_with_panel_rollover
    scheduler._earliest_cached_due = earliest_cached_due_with_panel_rollover
    setattr(scheduler, _INSTALLED_ATTR, True)


__all__ = [
    "install_reset_reminder_panel_rollover",
]
