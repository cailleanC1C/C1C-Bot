"""CoreOps ready event helpers."""

from __future__ import annotations

import logging

from discord.ext import commands

from modules.community.fusion.opt_in_view import register_persistent_fusion_views
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
