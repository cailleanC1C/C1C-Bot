"""Fusion debug commands."""

from __future__ import annotations

import logging

from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from shared.sheets import fusion as fusion_sheets

log = logging.getLogger("c1c.community.fusion")


class FusionCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @tier("user")
    @help_metadata(
        function_group="milestones",
        section="community",
        access_tier="user",
        usage="!fusion debug",
    )
    @commands.group(
        name="fusion",
        invoke_without_command=True,
        help="Fusion reminder data commands.",
    )
    async def fusion(self, ctx: commands.Context) -> None:
        await ctx.reply("Use `!fusion debug`.", mention_author=False)

    @tier("user")
    @help_metadata(
        function_group="milestones",
        section="community",
        access_tier="user",
        usage="!fusion debug",
    )
    @fusion.command(name="debug", help="Debug active fusion + first events from sheets cache.")
    async def fusion_debug(self, ctx: commands.Context) -> None:
        try:
            active = await fusion_sheets.get_active_fusion()
        except Exception as exc:
            log.exception("fusion debug failed to load active fusion")
            await ctx.reply(f"Fusion config error: {exc}", mention_author=False)
            return

        if active is None:
            await ctx.reply("No published fusion found.", mention_author=False)
            return

        try:
            events = await fusion_sheets.get_fusion_events(active.fusion_id)
        except Exception as exc:
            log.exception("fusion debug failed to load events", extra={"fusion_id": active.fusion_id})
            await ctx.reply(f"Fusion event load failed: {exc}", mention_author=False)
            return

        lines = [
            f"fusion_id: {active.fusion_id}",
            f"fusion_name: {active.fusion_name}",
            f"champion: {active.champion}",
            f"start_at_utc: {active.start_at_utc.isoformat()}",
            f"end_at_utc: {active.end_at_utc.isoformat()}",
            f"events: {len(events)}",
            "",
            "First 3 events:",
        ]
        for idx, event in enumerate(events[:3], start=1):
            points_display = (
                str(event.points_needed) if event.points_needed is not None else "TBA"
            )
            lines.append(
                f"{idx}. {event.event_name} | {event.start_at_utc.isoformat()} | "
                f"reward={event.reward_amount:g} | points={points_display}"
            )

        await ctx.reply("\n".join(lines), mention_author=False)
