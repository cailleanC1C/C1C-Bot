from __future__ import annotations

import logging

from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only
from modules.housekeeping import wandering_souls as ws

log = logging.getLogger("c1c.housekeeping.wandering_souls.cog")

MEMBER_LOAD_ERROR = "The investigation could not safely run because the full member list could not be loaded."


class WanderingSoulsCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @tier("admin")
    @help_metadata(function_group="operational", section="utilities", access_tier="admin")
    @commands.group(
        name="wanderingsouls",
        invoke_without_command=True,
        help="Admin diagnostics for Wandering Souls members.",
        brief="Investigate Wandering Souls members.",
    )
    @commands.guild_only()
    @admin_only()
    async def wandering_souls_group(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=ws.build_diagnostics_embed())

    @tier("admin")
    @help_metadata(function_group="operational", section="utilities", access_tier="admin")
    @wandering_souls_group.command(name="investigate")
    @commands.guild_only()
    @admin_only()
    async def investigate(self, ctx: commands.Context) -> None:
        guild = ctx.guild
        if guild is None:
            await ctx.send(embed=ws.build_error_embed(MEMBER_LOAD_ERROR))
            return
        if not getattr(guild, "chunked", False):
            try:
                await guild.chunk(cache=True)
            except Exception:
                log.exception("wandering souls investigation failed to chunk guild members")
                await ctx.send(embed=ws.build_error_embed(MEMBER_LOAD_ERROR))
                return
        if getattr(guild, "members", None) is None:
            await ctx.send(embed=ws.build_error_embed(MEMBER_LOAD_ERROR))
            return

        wandering_role, exclude_role, error = ws.resolve_investigation_roles(guild)
        if error:
            await ctx.send(embed=ws.build_error_embed(error))
            return
        result = ws.collect_wandering_souls(guild, int(wandering_role.id), int(exclude_role.id))
        for embed in ws.build_investigation_embeds(result):
            await ctx.send(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(WanderingSoulsCog(bot))
