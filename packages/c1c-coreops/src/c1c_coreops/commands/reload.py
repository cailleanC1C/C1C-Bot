"""CoreOps reload command helpers kept import-safe for C-03 guardrail."""

from __future__ import annotations

import inspect
import logging

from discord.ext import commands

from c1c_coreops.helpers import help_metadata
from modules.common import runtime
from modules.onboarding.schema import get_cached_welcome_questions
from shared import config as cfg
from shared.sheets import onboarding_questions

logger = logging.getLogger("c1c.coreops.commands.reload")

bot: commands.Bot | None = None


def _set_bot(value: commands.Bot) -> None:
    global bot
    bot = value


def _get_bot() -> commands.Bot | None:
    return bot


async def _maybe_await(result: object) -> None:
    if inspect.isawaitable(result):
        await result  # type: ignore[arg-type]


async def _invoke_bot_method(name: str) -> None:
    bot = _get_bot()
    if bot is None:
        logger.debug("skip %s: bot not registered", name)
        return
    method = getattr(bot, name, None)
    if method is None:
        logger.debug("skip %s: missing attribute", name)
        return
    try:
        await _maybe_await(method())
    except Exception:
        logger.exception("%s raised during reload", name)
        raise


async def _handle_reboot() -> None:
    """Perform the reboot side effects for ``!reload --reboot``."""

    await runtime.recreate_http_app()
    await _invoke_bot_method("reload_extensions")
    await _invoke_bot_method("start_reconnect_cycle")
    logger.info("Runtime rebooted via !reload --reboot")


class Reload(commands.Cog):
    """Reload command wiring."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        _set_bot(bot)

    @help_metadata(function_group="operational", access_tier="admin")
    @commands.command(
        name="reload",
        help="Reloads runtime configs and command modules, with optional soft reboot support.",
        brief="Reloads runtime configs and command modules.",
    )
    async def reload_command(self, ctx: commands.Context, *flags: str) -> None:
        if flags and flags[0].lower() == "onboarding":
            await self._reload_onboarding(ctx)
            return
        reboot = any(flag == "--reboot" for flag in flags)
        # Re-parse env + re-apply config invariants at runtime.
        cfg.reload_config()
        message = "✅ Configuration reloaded."
        if reboot:
            try:
                await _handle_reboot()
            except Exception as exc:  # pragma: no cover - surfaced to command invoker
                await ctx.send(f"⚠️ Reload failed: {exc}")
                raise
            else:
                message = "✅ Reloaded configuration and rebooted runtime."
        await ctx.send(message)

    async def _reload_onboarding(self, ctx: commands.Context) -> None:
        try:
            await get_cached_welcome_questions(force=True)
        except Exception as exc:
            logger.exception("onboarding cache reload failed")
            await ctx.send(f"⚠️ Onboarding reload failed: {exc}")
            return
        welcome_hash = onboarding_questions.schema_hash("welcome")
        try:
            promo_hash = onboarding_questions.schema_hash("promo")
        except Exception:
            promo_hash = ""
        logger.info(
            "onboarding reload • welcome=%s • promo=%s",
            welcome_hash,
            promo_hash or "n/a",
        )
        parts = [f"Welcome `{welcome_hash}`"]
        if promo_hash:
            parts.append(f"Promo `{promo_hash}`")
        await ctx.send(f"✅ Onboarding questions reloaded. {' • '.join(parts)}")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Reload(bot))
