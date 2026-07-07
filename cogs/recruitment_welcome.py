"""Recruitment welcome command registered under the cogs namespace."""

from __future__ import annotations

from typing import Optional

from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only, is_admin_member, is_staff_member
from modules.recruitment.welcome import WelcomeCommandService


def staff_only():
    """Allow CoreOps staff/admin roles or Discord Administrator fallback."""

    async def predicate(ctx: commands.Context) -> bool:
        author = getattr(ctx, "author", None)
        if is_staff_member(author) or is_admin_member(author):
            return True
        if getattr(ctx, "_coreops_suppress_denials", False):
            raise commands.CheckFailure("Staff only.")
        try:
            await ctx.reply("Staff only.")
        except Exception:
            pass
        raise commands.CheckFailure("Staff only.")

    return commands.check(predicate)


class WelcomeBridge(commands.Cog):
    """Recruitment welcome command using cached templates and legacy parity."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._service = WelcomeCommandService(bot)

    @tier("staff")
    @help_metadata(function_group="recruitment", section="recruitment", access_tier="staff")
    @commands.command(
        name="welcome",
        usage="<clan> [@member] [note]",
        help="Posts a templated welcome message for a new recruit using the cached templates.",
        brief="Posts a templated welcome message for a new recruit.",
    )
    @staff_only()
    async def welcome(
        self,
        ctx: commands.Context,
        clan: Optional[str] = None,
        *,
        note: Optional[str] = None,
    ) -> None:
        """Post a templated welcome message for the provided clan tag."""

        await self._service.post_welcome(ctx, clan, tail=note)

    @tier("admin")
    @help_metadata(function_group="operational", section="welcome_templates", access_tier="admin", usage="!welcome-refresh")
    @commands.command(
        name="welcome-refresh",
        help="Admin cache refresh for WelcomeTemplates. Reloads the sheet-backed welcome template cache used by !welcome and replies with visible refresh status; does not post a welcome message or change sheet schema.",
        brief="Refresh sheet-backed WelcomeTemplates cache.",
    )
    @admin_only()
    async def welcome_refresh(self, ctx: commands.Context) -> None:
        """Reload the WelcomeTemplates cache bucket."""

        await self._service.refresh_templates(ctx)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(WelcomeBridge(bot))
