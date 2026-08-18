"""CoreOps ready event helpers."""

from __future__ import annotations

import logging

from discord.ext import commands

from shared.config import cfg, get_feature_toggles

from modules.community.fusion.opt_in_view import register_persistent_fusion_views
from modules.community.shard_tracker.views import register_persistent_shard_views
from modules.community.reset_reminders.scheduler import register_persistent_reset_views
from modules.community.live_arena.panel import register_live_arena
from modules.housekeeping.staff_thread_guard import install as install_staff_thread_guard
from modules.onboarding import watcher_promo, watcher_welcome, welcome_self_heal
from modules.onboarding.ui import panels


log = logging.getLogger("modules.coreops.ready")


async def on_ready(bot: commands.Bot) -> None:
    """Run startup wiring that must execute after the bot is ready."""
    try:
        # Install the staff-thread guard ahead of the app-level on_message router.
        # The installer is idempotent so reconnects cannot stack wrappers.
        try:
            install_staff_thread_guard(bot)
        except Exception:
            log.exception("CORE_READY FAILURE: install_staff_thread_guard")

        # Existing startup wiring …
        # Register onboarding persistent views *after* the bot is ready to avoid race conditions.
        try:
            panels.register_views(bot)
        except Exception:
            log.exception("CORE_READY FAILURE: panels.register_views")
            return

        try:
            register_persistent_fusion_views(bot)
        except Exception:
            log.exception("CORE_READY FAILURE: register_persistent_fusion_views")
            return
        try:
            register_persistent_shard_views(bot)
        except Exception:
            log.exception("CORE_READY FAILURE: register_persistent_shard_views")
            return

        # Live Arena is optional and must never interrupt unrelated ready wiring.
        try:
            await register_live_arena(bot)
        except Exception:
            log.exception("CORE_READY FAILURE: register_live_arena")

        try:
            await register_persistent_reset_views(bot)
        except Exception:
            hook_name = "register_persistent_reset_views"
            toggles = get_feature_toggles()
            reset_feature_enabled = any(
                bool(toggles.get(key, False))
                for key in (
                    "reset_reminders",
                    "reset_reminders_enabled",
                    "feature_reset_reminders",
                )
            )
            log.exception(
                "CORE_READY FAILURE: %s | env=%s | guild_count=%s | guild_ids=%s | reset_feature_enabled=%s",
                hook_name,
                str(cfg.get("ENV_NAME") or "unknown").strip() or "unknown",
                len(getattr(bot, "guilds", []) or []),
                [
                    getattr(guild, "id", None)
                    for guild in (getattr(bot, "guilds", []) or [])
                ],
                reset_feature_enabled,
            )
            return

        # Ensure both onboarding watchers are wired.
        try:
            await watcher_welcome.setup(bot)
        except Exception:
            log.exception("CORE_READY FAILURE: watcher_welcome.setup")
            return

        # WelcomeWatcher is added from inside this on_ready dispatch. Explicitly
        # initialize it and schedule reconciliation so first-start ticket handling
        # does not depend on the newly-added cog receiving the current on_ready event.
        try:
            await welcome_self_heal.setup(bot)
        except Exception:
            log.exception("CORE_READY FAILURE: welcome_self_heal.setup")
            return

        try:
            await watcher_promo.setup(bot)
        except Exception as exc:
            log.exception(
                "CORE_READY FAILURE: watcher_promo.setup (%s: %s)",
                type(exc).__name__,
                exc,
            )
            return

        # Guard against bots without a .logger attribute; fall back to module logger.
        try:
            logger = getattr(bot, "logger", None)
            if logger is None:
                logger = log
            logger.info("on_ready: onboarding views registered (post-ready)")
        except Exception:
            pass
    except Exception:
        log.exception("core_ready.on_ready failed")
        raise
