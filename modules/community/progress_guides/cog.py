from __future__ import annotations

import discord
from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only
from shared.sheets import milestones_config
from shared.sheets.core import is_rate_limited_error
from modules.community.progress_guides.service import (
    ProgressGuideFAQPersistentView,
    ProgressGuideHowToUsePersistentView,
    ProgressGuideMissionPersistentView,
    ProgressGuidePlanAheadPersistentView,
    ProgressGuideMyProgressPersistentView,
    PublishSummary,
    publish_or_refresh,
)


def _quota_unavailable_embed() -> discord.Embed:
    return discord.Embed(
        title="Progress guides refresh unavailable",
        description=(
            "Google Sheets read quota was temporarily exceeded. "
            "Please wait a minute and run the command again."
        ),
        color=discord.Color.red(),
    )


def _is_quota_or_config_failure(exc: BaseException) -> bool:
    if isinstance(exc, milestones_config.MilestonesConfigLoadFailed):
        return True
    return is_rate_limited_error(exc)


async def _send_publish_result(
    ctx: commands.Context, bot: commands.Bot, *, action: str, refresh: bool
) -> None:
    try:
        summary = await publish_or_refresh(bot, refresh=refresh)
    except Exception as exc:
        if _is_quota_or_config_failure(exc):
            await ctx.send(embed=_quota_unavailable_embed())
            return
        raise
    await ctx.send(embed=_summary_embed(action, summary))


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
        bot.add_view(ProgressGuideMissionPersistentView())
        bot.add_view(ProgressGuideMyProgressPersistentView())
        bot.add_view(ProgressGuidePlanAheadPersistentView())
        bot.add_view(ProgressGuideHowToUsePersistentView())

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
        await _send_publish_result(ctx, self.bot, action="publish", refresh=False)

    @progressguides.command(name="refresh")
    @admin_only()
    async def refresh(self, ctx: commands.Context) -> None:
        await _send_publish_result(ctx, self.bot, action="refresh", refresh=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ProgressGuidesCog(bot))
