from __future__ import annotations

import discord
from discord.ext import commands
from c1c_coreops.helpers import help_metadata

from modules.onboarding.controllers.wizard import WizardController


class ResumeCog(commands.Cog):
    """Expose recruiter helpers for resuming onboarding sessions."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _resolve_controller(self) -> WizardController | None:
        candidates: list[object | None] = []
        candidates.append(getattr(self.bot, "onboarding_wizard", None))
        candidates.append(getattr(self.bot, "onboarding_controller", None))

        state = getattr(self.bot, "state", None)
        getter = getattr(state, "get", None)
        if callable(getter):
            candidates.append(getter("onboarding_wizard"))
            candidates.append(getter("onboarding_controller"))
        if isinstance(state, dict):
            candidates.append(state.get("onboarding_wizard"))
            candidates.append(state.get("onboarding_controller"))

        for candidate in candidates:
            if isinstance(candidate, WizardController):
                return candidate
        return None

    @help_metadata(
        function_group="recruitment",
        section="recruitment",
        access_tier="staff",
        usage="!onb resume @member",
    )
    @commands.command(
        name="onb",
        brief="Recruiter-only recovery for onboarding threads.",
        help=(
            "Recruiter-only recovery for onboarding threads. Run `!onb resume @member` "
            "inside the recruiter's ticket thread to restore their panel (requires Manage Threads)."
        ),
    )
    @commands.has_permissions(manage_threads=True)
    async def onb(
        self,
        ctx: commands.Context,
        action: str | None = None,
        member: discord.Member | None = None,
    ) -> None:
        if action != "resume":
            return

        channel = ctx.channel
        if not isinstance(channel, discord.Thread):
            await ctx.reply("Use this command inside an onboarding thread.", mention_author=False)
            return
        if member is None:
            await ctx.reply("Usage: `!onb resume @user`", mention_author=False)
            return

        controller = self._resolve_controller()
        if controller is None:
            await ctx.reply("Onboarding wizard is not available.", mention_author=False)
            return

        ok, recovered = await controller.recruiter_resume(ctx, member.id)
        if not ok:
            await ctx.reply("No onboarding session found for that user in this thread.", mention_author=False)
            return

        message = "Onboarding panel restored."
        if recovered:
            message = "Onboarding panel restored with a fresh message."
        await ctx.reply(message, mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ResumeCog(bot))
