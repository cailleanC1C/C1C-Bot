from __future__ import annotations

import asyncio
import logging

from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only
from modules.housekeeping.mirralith_overview import run_mirralith_overview_job

log = logging.getLogger("c1c.housekeeping.mirralith.cog")

MIRRALITH_MANUAL_COOLDOWN_SECONDS = 300


class MirralithOverviewCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._last_manual_run: float | None = None

    @tier("admin")
    @help_metadata(function_group="operational", section="utilities", access_tier="admin")
    @commands.group(
        name="mirralith",
        invoke_without_command=True,
        help="Post or refresh the Mirralith overview.",
        brief="Post or refresh the Mirralith overview.",
    )
    @admin_only()
    async def mirralith_group(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send('Use "!mirralith refresh" to regenerate the Mirralith overview.')

    @tier("admin")
    @help_metadata(function_group="operational", section="utilities", access_tier="admin")
    @mirralith_group.command(name="refresh")
    @admin_only()
    async def mirralith_refresh(self, ctx: commands.Context) -> None:
        now = asyncio.get_event_loop().time()
        if self._last_manual_run is not None:
            elapsed = now - self._last_manual_run
            if elapsed < MIRRALITH_MANUAL_COOLDOWN_SECONDS:
                remaining = int(MIRRALITH_MANUAL_COOLDOWN_SECONDS - elapsed)
                await ctx.send(
                    f"Mirralith was updated recently. Please wait ~{remaining} seconds before running it again."
                )
                return

        self._last_manual_run = now
        await ctx.send("Starting Mirralith overview update…")

        try:
            await run_mirralith_overview_job(self.bot, trigger="manual")
        except Exception:
            log.exception("Mirralith manual refresh failed")
            await ctx.send("Mirralith update failed. Please check the bot logs for details.")
            return

        await ctx.send("Mirralith overview updated and posted to the Mirralith channel.")


async def setup(bot: commands.Bot):
    await bot.add_cog(MirralithOverviewCog(bot))
