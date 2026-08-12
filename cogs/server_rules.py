"""Admin commands for the sheet-driven Server Rules and FAQ publisher."""

from __future__ import annotations

from typing import Any

import discord
from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only
from modules.common import feature_flags, runtime as runtime_helpers
from modules.common.embeds import get_embed_colour
from modules.common.logs import channel_label
from modules.ops import server_rules
import modules.ops.server_rules_interactive as server_rules_interactive

FEATURE_TOGGLE = "server_rules_faq"


def _usage_embed() -> discord.Embed:
    embed = discord.Embed()
    embed.title = "Server rules"
    embed.description = "Use `!serverrules publish` or `!serverrules refresh`."
    embed.colour = get_embed_colour("admin")
    return embed


def _disabled_embed() -> discord.Embed:
    embed = discord.Embed()
    embed.title = "Server rules disabled"
    embed.description = "Server rules FAQ publishing is disabled in FeatureToggles."
    embed.colour = get_embed_colour("admin")
    return embed


class ServerRulesCog(commands.Cog):
    def __init__(self, bot: commands.Bot, operations: Any = server_rules) -> None:
        self.bot = bot
        self.operations = operations

    @tier("admin")
    @help_metadata(
        function_group="operational",
        section="utilities",
        access_tier="admin",
    )
    @commands.group(name="serverrules", invoke_without_command=True)
    @admin_only()
    async def serverrules(self, ctx: commands.Context) -> None:
        await ctx.reply(embed=_usage_embed(), mention_author=False)

    async def _run(self, ctx: commands.Context, action: str) -> None:
        if not feature_flags.is_enabled(FEATURE_TOGGLE):
            await ctx.reply(embed=_disabled_embed(), mention_author=False)
            await runtime_helpers.send_log_message(
                f"📘 **Server rules** — cmd=serverrules {action} • status=disabled"
            )
            return
        if action == "publish":
            summary, target = await self.operations.publish(self.bot)
        else:
            summary, target = await self.operations.refresh(self.bot)
        await ctx.reply(
            embed=server_rules.result_embed(action, summary), mention_author=False
        )
        await runtime_helpers.send_log_message(
            "📘 **Server rules** — "
            f"cmd=serverrules {action} • admin={getattr(getattr(ctx, 'author', None), 'id', 'unknown')} "
            f"• destination={channel_label(getattr(target, 'guild', getattr(ctx, 'guild', None)), getattr(target, 'id', None))} "
            f"• created={summary.created} • refreshed={summary.refreshed} • removed={summary.removed} "
            f"• skipped={summary.skipped} • failed={summary.failed}"
        )

    @serverrules.command(name="publish")
    @admin_only()
    async def publish(self, ctx: commands.Context) -> None:
        await self._run(ctx, "publish")

    @serverrules.command(name="refresh")
    @admin_only()
    async def refresh(self, ctx: commands.Context) -> None:
        await self._run(ctx, "refresh")


async def setup(bot: commands.Bot) -> None:
    server_rules_interactive.register_persistent_view(bot)
    await bot.add_cog(ServerRulesCog(bot, operations=server_rules_interactive))
