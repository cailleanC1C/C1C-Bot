from __future__ import annotations

import discord
from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only
from modules.community.progress_guides.service import (
    ProgressGuideFAQPersistentView,
    PublishSummary,
    publish_or_refresh,
)


def _summary_embed(action: str, summary: PublishSummary) -> discord.Embed:
    embed = discord.Embed(
        title=f"Progress guides {action}", color=discord.Color.blurple()
    )
    embed.add_field(name="Created", value=str(summary.created), inline=True)
    embed.add_field(name="Refreshed", value=str(summary.refreshed), inline=True)
    embed.add_field(name="Skipped", value=str(len(summary.skipped)), inline=True)
    embed.add_field(name="Failures", value=str(len(summary.failures)), inline=True)
    if summary.skipped:
        embed.add_field(
            name="Skipped details",
            value="\n".join(summary.skipped)[:1024],
            inline=False,
        )
    if summary.failures:
        embed.add_field(
            name="Failure details",
            value="\n".join(summary.failures)[:1024],
            inline=False,
        )
    return embed


class ProgressGuidesCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        bot.add_view(ProgressGuideFAQPersistentView())

    @tier("admin")
    @help_metadata(
        function_group="community", section="progress guides", access_tier="admin"
    )
    @commands.group(name="progressguides", invoke_without_command=True)
    @admin_only()
    async def progressguides(self, ctx: commands.Context) -> None:
        embed = discord.Embed(
            title="Progress guides",
            description="Use `!progressguides publish` or `!progressguides refresh`.",
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed)

    @progressguides.command(name="publish")
    @admin_only()
    async def publish(self, ctx: commands.Context) -> None:
        summary = await publish_or_refresh(self.bot, refresh=False)
        await ctx.send(embed=_summary_embed("publish", summary))

    @progressguides.command(name="refresh")
    @admin_only()
    async def refresh(self, ctx: commands.Context) -> None:
        summary = await publish_or_refresh(self.bot, refresh=True)
        await ctx.send(embed=_summary_embed("refresh", summary))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ProgressGuidesCog(bot))
