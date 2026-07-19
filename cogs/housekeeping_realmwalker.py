from __future__ import annotations

import logging

import discord
from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only
from modules.common.embeds import get_embed_colour
from modules.housekeeping import realmwalker

log = logging.getLogger("c1c.housekeeping.realmwalker.cog")

MEMBER_LOAD_ERROR = (
    "The full member list could not be loaded, so the RealmWalker audit was aborted. "
    "No roles were changed."
)


class RealmWalkerAuditCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @tier("admin")
    @help_metadata(
        function_group="operational", section="housekeeping", access_tier="admin"
    )
    @commands.group(name="audit", invoke_without_command=True)
    @commands.guild_only()
    @admin_only()
    async def audit_group(self, ctx: commands.Context) -> None:
        embed = discord.Embed(
            title="🧹 Housekeeping Audit",
            description="Usage: `!audit realmwalker [fix]`",
            colour=get_embed_colour("admin"),
        )
        await ctx.send(embed=embed)

    @tier("admin")
    @help_metadata(
        function_group="operational", section="housekeeping", access_tier="admin"
    )
    @audit_group.command(name="realmwalker")
    @commands.guild_only()
    @admin_only()
    async def audit_realmwalker(self, ctx: commands.Context, action: str = "") -> None:
        if action and action.lower() != "fix":
            await ctx.send(
                embed=realmwalker.build_embeds(
                    realmwalker.RealmWalkerAuditResult(),
                    error="Usage: `!audit realmwalker [fix]`",
                )[0]
            )
            return
        config, error = await realmwalker.resolve_config()
        if config is None:
            log.warning("RealmWalker audit config invalid", extra={"reason": error})
            await ctx.send(
                embed=realmwalker.build_embeds(
                    realmwalker.RealmWalkerAuditResult(), error=error
                )[0]
            )
            return
        guild = ctx.guild
        assert guild is not None
        try:
            members = [member async for member in guild.fetch_members(limit=None)]
        except Exception:
            log.warning(
                "RealmWalker audit aborted; full member fetch failed", exc_info=True
            )
            await ctx.send(
                embed=realmwalker.build_embeds(
                    realmwalker.RealmWalkerAuditResult(), error=MEMBER_LOAD_ERROR
                )[0],
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return
        result = realmwalker.scan_members(members, config)
        if action.lower() == "fix":
            fixed = await realmwalker.fix_issues(
                result.issues, guild.get_role(config.access_role_id)
            )
            fixed.checked = result.checked
            result = fixed
        for embed in realmwalker.build_embeds(result, fixing=action.lower() == "fix"):
            await ctx.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(RealmWalkerAuditCog(bot))
