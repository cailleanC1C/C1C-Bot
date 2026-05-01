"""Idle watcher for onboarding sessions stored in Sheets."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable

import discord
from discord.ext import commands

from modules.common import runtime as rt
from shared.config import get_recruitment_coordinator_role_ids
from shared.sheets import onboarding_sessions
from modules.placement import reservation_jobs

log = logging.getLogger("c1c.onboarding.idle_watcher")

FIRST_REMINDER_AFTER = timedelta(hours=3)
WARNING_AFTER = timedelta(hours=24)
AUTO_CLOSE_AFTER = timedelta(hours=36)
WATCHER_INTERVAL_SECONDS = int(FIRST_REMINDER_AFTER.total_seconds())
WATCHER_JOB_NAME = "onboarding_idle_watcher"
WATCHER_COMPONENT = "recruitment"
NO_PLACEMENT_TAG = "NONE"

_WATCHER_TASK: asyncio.Task | None = None


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        normalized = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(normalized)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed
    except Exception:
        return None


def _role_ping(role_ids: Iterable[int]) -> str:
    tokens = [f"<@&{rid}>" for rid in sorted(role_ids) if rid]
    return " ".join(tokens)


def _format_interval(delta: timedelta) -> float:
    return delta.total_seconds()


def _is_closed_ticket_name(thread: discord.Thread | None) -> bool:
    if thread is None:
        return False
    name = str(getattr(thread, "name", "") or "")
    return name.lower().startswith("closed-")


def _is_thread_archived_or_locked(thread: discord.Thread | None) -> bool:
    if thread is None:
        return False
    return bool(getattr(thread, "archived", False)) or bool(getattr(thread, "locked", False))


async def has_recruitment_summary(thread: discord.Thread, *, history_limit: int = 50) -> bool:
    markers = ("c1c • recruitment summary", "recruitment summary")
    try:
        async for message in thread.history(limit=history_limit):
            content = str(getattr(message, "content", "") or "").casefold()
            if any(marker in content for marker in markers):
                return True

            embeds = getattr(message, "embeds", []) or []
            for embed in embeds:
                title = str(getattr(embed, "title", "") or "").casefold()
                description = str(getattr(embed, "description", "") or "").casefold()
                if any(marker in title or marker in description for marker in markers):
                    return True
    except Exception:
        log.warning("failed checking thread for recruitment summary", exc_info=True)
    return False


def _has_truthy_state(row: dict, keys: Iterable[str]) -> bool:
    for key in keys:
        if key not in row:
            continue
        value = row.get(key)
        if isinstance(value, bool):
            if value:
                return True
            continue
        if value is None:
            continue
        token = str(value).strip().lower()
        if token in {"", "0", "false", "no", "none", "null"}:
            continue
        return True
    return False


async def _ticket_needs_onboarding_reminder(row: dict, thread: discord.Thread) -> bool:
    session_status = str(row.get("status") or "").strip().lower()
    if session_status in {"closed", "cancelled", "completed", "auto_closed"}:
        return False
    if _is_closed_ticket_name(thread):
        return False
    if _is_thread_archived_or_locked(thread):
        return False
    if row.get("completed"):
        return False

    durable_summary_keys = (
        "summary_message_id",
        "recruitment_summary_message_id",
        "summary_posted_at",
        "recruitment_summary_posted_at",
        "summary_exists",
        "recruitment_summary_exists",
        "summary_posted",
        "recruitment_summary_posted",
    )
    if _has_truthy_state(row, durable_summary_keys):
        return False

    return not await has_recruitment_summary(thread)


async def ensure_idle_watcher(bot: commands.Bot) -> None:
    global _WATCHER_TASK

    runtime = rt.get_active_runtime()
    if runtime is None:
        return

    if _WATCHER_TASK is not None and not _WATCHER_TASK.done():
        return

    job = runtime.scheduler.every(
        seconds=WATCHER_INTERVAL_SECONDS,
        jitter="small",
        tag="onboarding",
        name=WATCHER_JOB_NAME,
        component=WATCHER_COMPONENT,
    )

    _WATCHER_TASK = job.do(lambda: run_idle_scan(bot))


async def _resolve_thread(bot: commands.Bot, thread_id: int) -> discord.Thread | None:
    channel = bot.get_channel(thread_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(thread_id)
        except Exception:
            return None
    return channel if isinstance(channel, discord.Thread) else None


def _resolve_thread_parts(flow: str, name: str | None):
    from modules.onboarding import watcher_welcome as ww

    parser = ww.parse_promo_thread_name if flow.startswith("promo") else ww.parse_welcome_thread_name
    return parser(name), ww.build_closed_thread_name


async def _close_thread(
    thread: discord.Thread,
    *,
    bot: commands.Bot,
    flow: str,
    user_id: int,
    auto_close_at: datetime,
) -> None:
    name = getattr(thread, "name", None)
    parts, name_builder = _resolve_thread_parts(flow, name)
    closed_name = None
    if parts is not None:
        closed_name = name_builder(parts.ticket_code, parts.username, NO_PLACEMENT_TAG)

    coordinator_ping = _role_ping(get_recruitment_coordinator_role_ids())
    if flow.startswith("promo"):
        message = (
            f"{coordinator_ping} no response from <@{user_id}> within the timeout.\n"
            "The promo ticket has been closed."
        )
    else:
        message = (
            f"{coordinator_ping} no response from <@{user_id}> within the timeout.\n"
            "The onboarding ticket has been closed — please remove the user from the server."
        )

    handled = False
    if flow.startswith("promo"):
        watcher = bot.get_cog("PromoTicketWatcher") if bot else None
        if watcher is not None:
            context = await watcher._ensure_context(thread)
            if context:
                await watcher.auto_close_for_inactivity(
                    thread,
                    context,
                    notice=message,
                    closed_name=closed_name,
                )
                handled = True
    else:
        watcher = bot.get_cog("WelcomeTicketWatcher") if bot else None
        if watcher is not None:
            context = await watcher._ensure_context(thread)
            if context:
                await watcher.auto_close_for_inactivity(
                    thread,
                    context,
                    notice=message,
                    closed_name=closed_name,
                    final_tag=NO_PLACEMENT_TAG,
                )
                handled = True

    if not handled:
        if closed_name and name != closed_name:
            try:
                await thread.edit(name=closed_name)
            except Exception:
                log.warning("failed to rename onboarding thread for auto-close", exc_info=True)

        try:
            await thread.send(message)
        except Exception:
            log.warning("failed to post onboarding auto-close notice", exc_info=True)

        try:
            await thread.edit(archived=True, locked=True)
        except Exception:
            log.warning("failed to archive onboarding thread on auto-close", exc_info=True)

    await reservation_jobs.release_reservations_for_thread(thread.id, bot=bot)


async def _handle_row(
    bot: commands.Bot,
    row: dict,
    *,
    now: datetime,
) -> None:
    user_id = row.get("user_id")
    thread_id = row.get("thread_id")
    if not user_id or not thread_id:
        return

    if row.get("completed"):
        return
    if row.get("auto_closed_at"):
        return

    updated_at = _parse_iso(row.get("updated_at"))
    if updated_at is None:
        return

    age = now - updated_at
    thread = await _resolve_thread(bot, int(thread_id))
    if thread is None:
        onboarding_sessions.update_existing(
            thread_id,
            {
                **row,
                "auto_closed_at": now.isoformat(),
                "completed": True,
                "completed_at": now.isoformat(),
                "updated_at": now.isoformat(),
            },
        )
        return

    from modules.onboarding import welcome_flow

    resolution = welcome_flow.resolve_onboarding_flow(thread)
    flow = (resolution.flow or "welcome").lower()

    if not await _ticket_needs_onboarding_reminder(row, thread):
        return

    first_reminder_at = _parse_iso(row.get("first_reminder_at"))
    warning_sent_at = _parse_iso(row.get("warning_sent_at"))
    auto_closed_at = _parse_iso(row.get("auto_closed_at"))

    if auto_closed_at:
        return

    if age >= AUTO_CLOSE_AFTER:
        onboarding_sessions.update_existing(
            thread_id,
            {
                **row,
                "auto_closed_at": now.isoformat(),
                "completed": True,
                "completed_at": now.isoformat(),
                "updated_at": now.isoformat(),
            },
        )
        await _close_thread(thread, bot=bot, flow=flow, user_id=user_id, auto_close_at=now)
        return

    if age >= WARNING_AFTER and warning_sent_at is None:
        ping = _role_ping(get_recruitment_coordinator_role_ids())
        content = (
            f"<@{user_id}> we still don’t have your answers yet.\n"
            f"{ping} heads up — this ticket has been idle for a while.\n"
            "If we don’t hear back from the player in the next 12 hours, this ticket will be closed."
        )
        try:
            await thread.send(content)
        except Exception:
            log.warning("failed to post onboarding warning", exc_info=True)
        else:
            onboarding_sessions.update_existing(
                thread_id,
                {
                    **row,
                    "warning_sent_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                },
            )
        return

    if age >= FIRST_REMINDER_AFTER and first_reminder_at is None:
        content = (
            f"<@{user_id}> your onboarding questions are still waiting.\n"
            "Please hit “Open questions” on the panel below to start or resume so our recruiters can help you with placement."
        )
        try:
            await thread.send(content)
        except Exception:
            log.warning("failed to post onboarding reminder", exc_info=True)
        else:
            onboarding_sessions.update_existing(
                thread_id,
                {
                    **row,
                    "first_reminder_at": now.isoformat(),
                    "updated_at": now.isoformat(),
                },
            )


async def run_idle_scan(bot: commands.Bot, *, now: datetime | None = None) -> None:
    await bot.wait_until_ready()

    clock = now or _utc_now()
    rows = onboarding_sessions.load_all()
    for row in rows:
        try:
            await _handle_row(bot, row, now=clock)
        except Exception:
            log.exception("onboarding idle watcher row failed", extra={"thread_id": row.get("thread_id")})


__all__ = ["ensure_idle_watcher", "run_idle_scan"]
