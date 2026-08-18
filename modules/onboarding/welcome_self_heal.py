"""Self-healing boundary for recruitment welcome onboarding.

The legacy WelcomeWatcher is installed from the app's on_ready path. A cog added while
an on_ready dispatch is already in progress is not guaranteed to receive that same
event, which can leave WelcomeWatcher.channel_id unset for the whole process. In that
state both automatic and reaction wake-up paths silently ignore tickets.

This module makes initialization explicit and adds an idempotent reconciliation path
for open welcome tickets so one missed gateway event cannot strand a recruit.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable

import discord
from discord import RawReactionActionEvent
from discord.ext import commands

from modules.common import feature_flags
from modules.onboarding.controllers.welcome_controller import locate_welcome_message
from modules.onboarding.watcher_welcome import (
    is_welcome_ticket_thread_name,
    post_open_questions_panel,
    resolve_subject_user_id,
)
from shared.config import get_ticket_tool_bot_id, get_welcome_channel_id

log = logging.getLogger("c1c.onboarding.welcome_self_heal")

_SUPPORTED_WAKE_EMOJIS = {"👍", "🎫", "🎟", "🎟️"}
_RECONCILE_TASK_ATTR = "_c1c_welcome_self_heal_task"
_COG_NAME = "WelcomeOnboardingSelfHeal"


def _feature_enabled() -> bool:
    return feature_flags.is_enabled("recruitment_welcome") and feature_flags.is_enabled(
        "welcome_dialog"
    )


def _configured_parent_id() -> int | None:
    raw = get_welcome_channel_id()
    try:
        return int(raw) if raw else None
    except (TypeError, ValueError):
        return None


def _is_target_thread(thread: object) -> bool:
    if not isinstance(thread, discord.Thread):
        return False
    parent_id = _configured_parent_id()
    if parent_id is None or getattr(thread, "parent_id", None) != parent_id:
        return False
    if not is_welcome_ticket_thread_name(getattr(thread, "name", None)):
        return False
    if bool(getattr(thread, "archived", False)) or bool(getattr(thread, "locked", False)):
        return False
    return True


async def _initialize_legacy_watcher(bot: commands.Bot) -> bool:
    """Ensure WelcomeWatcher receives its ready initialization even on first startup."""

    watcher = bot.get_cog("WelcomeWatcher")
    if watcher is None:
        log.error("welcome watcher initialization failed • reason=cog_missing")
        return False

    channel_id = getattr(watcher, "channel_id", None)
    registered = bool(getattr(watcher, "_onb_registered", False))
    if channel_id is not None and registered:
        log.info(
            "welcome watcher initialization verified • channel_id=%s • result=already_ready",
            channel_id,
        )
        return True

    ready = getattr(watcher, "on_ready", None)
    if not callable(ready):
        log.error("welcome watcher initialization failed • reason=on_ready_missing")
        return False

    # The watcher protects its own ready method with _announced. A first-start cog
    # added during the current on_ready event has _announced=False, so explicitly
    # invoking it here closes the dispatch-order race.
    try:
        await ready()
    except Exception as exc:
        log.exception(
            "welcome watcher initialization failed • error=%s: %s",
            type(exc).__name__,
            exc,
        )
        return False

    channel_id = getattr(watcher, "channel_id", None)
    registered = bool(getattr(watcher, "_onb_registered", False))
    ok = channel_id is not None and registered
    log.info(
        "welcome watcher initialization verified • channel_id=%s • registered=%s • result=%s",
        channel_id,
        registered,
        "ready" if ok else "not_ready",
    )
    return ok


async def ensure_onboarding_started(
    bot: commands.Bot,
    thread: discord.Thread,
    *,
    source: str,
    actor: discord.abc.User | None = None,
    trigger_message: discord.Message | None = None,
) -> str:
    """Idempotently ensure the welcome question panel exists in ``thread``."""

    thread_id = getattr(thread, "id", None)
    thread_name = getattr(thread, "name", None)
    log.info(
        "welcome ticket candidate seen • source=%s • thread_id=%s • thread=%s",
        source,
        thread_id,
        thread_name,
    )

    if not _feature_enabled():
        log.info(
            "welcome ticket candidate ignored • source=%s • thread_id=%s • reason=feature_disabled",
            source,
            thread_id,
        )
        return "feature_disabled"
    if not _is_target_thread(thread):
        log.info(
            "welcome ticket candidate ignored • source=%s • thread_id=%s • reason=not_welcome_ticket",
            source,
            thread_id,
        )
        return "not_welcome_ticket"

    welcome_message = trigger_message
    if welcome_message is None:
        try:
            welcome_message = await locate_welcome_message(thread)
        except Exception as exc:
            log.warning(
                "welcome ticket candidate deferred • source=%s • thread_id=%s • reason=welcome_lookup_failed • error=%s: %s",
                source,
                thread_id,
                type(exc).__name__,
                exc,
            )
            return "welcome_lookup_failed"
    if welcome_message is None:
        log.info(
            "welcome ticket candidate deferred • source=%s • thread_id=%s • reason=welcome_message_missing",
            source,
            thread_id,
        )
        return "welcome_message_missing"

    bot_user = getattr(bot, "user", None)
    bot_user_id = getattr(bot_user, "id", None)
    try:
        subject_user_id = await resolve_subject_user_id(
            thread, bot_user_id=bot_user_id
        )
        outcome = await post_open_questions_panel(
            bot,
            thread,
            actor=actor,
            flow="welcome",
            trigger_message=welcome_message,
            subject_user_id=subject_user_id,
        )
    except Exception as exc:
        log.exception(
            "welcome onboarding ensure failed • source=%s • thread_id=%s • error=%s: %s",
            source,
            thread_id,
            type(exc).__name__,
            exc,
        )
        return "failed"

    if outcome.result == "panel_created":
        result = "started"
    elif outcome.result == "skipped" and outcome.reason == "panel_exists":
        result = "already_present"
    else:
        result = f"{outcome.result}:{outcome.reason or '-'}"

    log.info(
        "welcome onboarding ensure complete • source=%s • thread_id=%s • result=%s • panel_message_id=%s",
        source,
        thread_id,
        result,
        getattr(outcome, "panel_message_id", None),
    )
    return result


async def _resolve_parent(bot: commands.Bot):
    parent_id = _configured_parent_id()
    if parent_id is None:
        return None
    channel = bot.get_channel(parent_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(parent_id)
        except Exception:
            log.exception(
                "welcome reconciliation parent lookup failed • channel_id=%s", parent_id
            )
            return None
    return channel


def _dedupe_threads(items: Iterable[object], parent_id: int) -> list[discord.Thread]:
    result: list[discord.Thread] = []
    seen: set[int] = set()
    for item in items:
        if not isinstance(item, discord.Thread):
            continue
        if getattr(item, "parent_id", None) != parent_id:
            continue
        tid = getattr(item, "id", None)
        if not isinstance(tid, int) or tid in seen:
            continue
        seen.add(tid)
        result.append(item)
    return result


async def reconcile_open_welcome_threads(bot: commands.Bot) -> dict[str, int]:
    """Repair open welcome tickets whose onboarding panel was never created."""

    summary = {"scanned": 0, "started": 0, "already_present": 0, "deferred": 0, "failed": 0}
    if not _feature_enabled():
        log.info("welcome reconciliation skipped • reason=feature_disabled")
        return summary

    parent = await _resolve_parent(bot)
    parent_id = _configured_parent_id()
    if parent is None or parent_id is None:
        log.warning("welcome reconciliation skipped • reason=parent_unavailable")
        return summary

    candidates: list[object] = list(getattr(parent, "threads", ()) or ())
    guild = getattr(parent, "guild", None)
    if guild is not None:
        candidates.extend(list(getattr(guild, "threads", ()) or ()))
        active_threads = getattr(guild, "active_threads", None)
        if callable(active_threads):
            try:
                candidates.extend(list(await active_threads()))
            except Exception:
                log.warning("welcome reconciliation active-thread fetch failed", exc_info=True)

    for thread in _dedupe_threads(candidates, parent_id):
        if not _is_target_thread(thread):
            continue
        summary["scanned"] += 1
        result = await ensure_onboarding_started(bot, thread, source="startup_reconcile")
        if result == "started":
            summary["started"] += 1
        elif result == "already_present":
            summary["already_present"] += 1
        elif result == "failed":
            summary["failed"] += 1
        else:
            summary["deferred"] += 1

    log.info(
        "welcome reconciliation complete • scanned=%s • started=%s • already_present=%s • deferred=%s • failed=%s",
        summary["scanned"],
        summary["started"],
        summary["already_present"],
        summary["deferred"],
        summary["failed"],
    )
    return summary


async def _delayed_reconcile(bot: commands.Bot, *, delay: float = 3.0) -> None:
    await asyncio.sleep(delay)
    await reconcile_open_welcome_threads(bot)


def _schedule_reconcile(bot: commands.Bot) -> None:
    existing = getattr(bot, _RECONCILE_TASK_ATTR, None)
    if existing is not None and not existing.done():
        return
    task = asyncio.create_task(_delayed_reconcile(bot), name="welcome-onboarding-self-heal")
    setattr(bot, _RECONCILE_TASK_ATTR, task)


class WelcomeOnboardingSelfHeal(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, thread: discord.Thread) -> asyncio.Lock:
        tid = int(thread.id)
        return self._locks.setdefault(tid, asyncio.Lock())

    async def _ensure_after_grace(
        self,
        thread: discord.Thread,
        *,
        source: str,
        actor: discord.abc.User | None = None,
        trigger_message: discord.Message | None = None,
        delay: float = 0.8,
    ) -> None:
        await asyncio.sleep(delay)
        async with self._lock(thread):
            # The Ticket Tool welcome post can arrive just after the thread event.
            for attempt in range(1, 5):
                result = await ensure_onboarding_started(
                    self.bot,
                    thread,
                    source=source,
                    actor=actor,
                    trigger_message=trigger_message,
                )
                if result != "welcome_message_missing":
                    return
                if attempt < 4:
                    await asyncio.sleep(1.0)

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        await _initialize_legacy_watcher(self.bot)
        _schedule_reconcile(self.bot)

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        if not _is_target_thread(thread):
            return
        log.info(
            "welcome ticket event received • event=thread_create • thread_id=%s • thread=%s",
            getattr(thread, "id", None),
            getattr(thread, "name", None),
        )
        asyncio.create_task(
            self._ensure_after_grace(thread, source="thread_create"),
            name=f"welcome-self-heal-thread:{getattr(thread, 'id', 0)}",
        )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        thread = message.channel if isinstance(message.channel, discord.Thread) else None
        if thread is None or not _is_target_thread(thread):
            return
        ticket_tool_id = get_ticket_tool_bot_id()
        author_id = getattr(message.author, "id", None)
        if ticket_tool_id is None or author_id != ticket_tool_id:
            return
        log.info(
            "welcome ticket event received • event=ticket_tool_message • thread_id=%s • message_id=%s",
            getattr(thread, "id", None),
            getattr(message, "id", None),
        )
        asyncio.create_task(
            self._ensure_after_grace(
                thread,
                source="ticket_tool_message",
                actor=message.author,
                trigger_message=message,
                delay=0.4,
            ),
            name=f"welcome-self-heal-message:{getattr(thread, 'id', 0)}",
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: RawReactionActionEvent) -> None:
        if str(payload.emoji) not in _SUPPORTED_WAKE_EMOJIS:
            return
        bot_user = getattr(self.bot, "user", None)
        if bot_user is not None and payload.user_id == getattr(bot_user, "id", None):
            return
        guild = self.bot.get_guild(payload.guild_id) if payload.guild_id else None
        if guild is None:
            return
        thread = guild.get_thread(payload.channel_id)
        if thread is None:
            channel = self.bot.get_channel(payload.channel_id)
            thread = channel if isinstance(channel, discord.Thread) else None
        if thread is None or not _is_target_thread(thread):
            return
        actor = payload.member or guild.get_member(payload.user_id)
        log.info(
            "welcome ticket event received • event=reaction • thread_id=%s • message_id=%s • user_id=%s • emoji=%s",
            getattr(thread, "id", None),
            payload.message_id,
            payload.user_id,
            payload.emoji,
        )
        asyncio.create_task(
            self._ensure_after_grace(
                thread,
                source="reaction",
                actor=actor,
                delay=0.8,
            ),
            name=f"welcome-self-heal-reaction:{getattr(thread, 'id', 0)}",
        )


async def setup(bot: commands.Bot) -> None:
    """Install the self-heal cog and explicitly close the first-ready race."""

    existing = bot.get_cog(_COG_NAME)
    if existing is None:
        await bot.add_cog(WelcomeOnboardingSelfHeal(bot))
    elif not isinstance(existing, WelcomeOnboardingSelfHeal):
        raise RuntimeError(f"cog name collision for {_COG_NAME}")

    await _initialize_legacy_watcher(bot)
    _schedule_reconcile(bot)
