from __future__ import annotations

import logging

from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only
from modules.housekeeping.achievements import (
    AchievementsConfigError,
    publish_achievements,
    refresh_achievements,
)

log = logging.getLogger("c1c.housekeeping.achievements.cog")


class AchievementsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @tier("admin")
    @help_metadata(
        function_group="operational", section="utilities", access_tier="admin"
    )
    @commands.group(
        name="achievements",
        invoke_without_command=True,
        help="Publish or refresh the configured achievements images.",
        brief="Publish or refresh achievements images.",
    )
    @admin_only()
    async def achievements_group(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send('Use "!achievements publish" or "!achievements refresh".')

    @tier("admin")
    @help_metadata(
        function_group="operational", section="utilities", access_tier="admin"
    )
    @achievements_group.command(name="publish")
    @admin_only()
    async def achievements_publish(self, ctx: commands.Context) -> None:
        try:
            result = await publish_achievements(self.bot)
        except AchievementsConfigError as exc:
            await ctx.send(f"Achievements publish failed: {exc}")
            return
        except Exception:
            log.exception("Achievements publish failed")
            await ctx.send(
                "Achievements publish failed. Please check the bot logs for details."
            )
            return
        await ctx.send(result.message)

    @tier("admin")
    @help_metadata(
        function_group="operational", section="utilities", access_tier="admin"
    )
    @achievements_group.command(name="refresh")
    @admin_only()
    async def achievements_refresh(self, ctx: commands.Context) -> None:
        try:
            result = await refresh_achievements(self.bot)
        except AchievementsConfigError as exc:
            await ctx.send(f"Achievements refresh failed: {exc}")
            return
        except Exception:
            log.exception("Achievements refresh failed")
            await ctx.send(
                "Achievements refresh failed. Please check the bot logs for details."
            )
            return
        await ctx.send(result.message)


async def setup(bot: commands.Bot):
    await bot.add_cog(AchievementsCog(bot))
