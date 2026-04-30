"""CoreOps ready event helpers."""

from __future__ import annotations

import logging

from discord.ext import commands

from shared.config import cfg, get_feature_toggles

from modules.community.fusion.opt_in_view import register_persistent_fusion_views
from modules.community.shard_tracker.views import register_persistent_shard_views
from modules.community.reset_reminders.scheduler import register_persistent_reset_views
from modules.onboarding import watcher_promo, watcher_welcome
from modules.onboarding.ui import panels


log = logging.getLogger("modules.coreops.ready")


async def on_ready(bot: commands.Bot) -> None:
    """Run startup wiring that must execute after the bot is ready."""
    try:
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
                [getattr(guild, "id", None) for guild in (getattr(bot, "guilds", []) or [])],
                reset_feature_enabled,
            )
            return

        # Ensure both onboarding watchers are wired
        try:
            await watcher_welcome.setup(bot)
        except Exception:
            log.exception("CORE_READY FAILURE: watcher_welcome.setup")
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
