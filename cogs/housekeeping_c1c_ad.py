from __future__ import annotations

import logging

from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import ops_only
from modules.housekeeping.c1c_ad import run_c1c_ad_job

log = logging.getLogger("c1c.housekeeping.c1c_ad.cog")


class C1CAdCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @tier("staff")
    @help_metadata(
        function_group="operational", section="utilities", access_tier="staff"
    )
    @commands.command(
        name="c1cad",
        help="Post or refresh the C1C recruitment ad.",
        brief="Post or refresh the C1C recruitment ad.",
    )
    @ops_only()
    async def c1cad(self, ctx: commands.Context) -> None:
        try:
            result = await run_c1c_ad_job(self.bot, trigger="manual", force=True)
        except Exception:
            log.exception("C1C ad manual refresh failed")
            await ctx.send(
                "C1C ad update failed. Please check the bot logs for details."
            )
            return
        if result.status == "success":
            await ctx.send("C1C recruitment ad refreshed.")
        elif result.message == "feature toggle off":
            await ctx.send("C1C recruitment ad is disabled by feature toggle.")
        else:
            await ctx.send(f"C1C recruitment ad skipped: {result.message}.")


async def setup(bot: commands.Bot):
    await bot.add_cog(C1CAdCog(bot))
