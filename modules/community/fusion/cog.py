"""Fusion debug and publish commands."""

from __future__ import annotations

import datetime as dt
import logging

import discord
from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only
from modules.community.fusion.rendering import build_fusion_announcement_embed
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
        await ctx.reply("Use `!fusion debug` or `!fusion publish`.", mention_author=False)

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
            active = await fusion_sheets.get_publishable_fusion()
        except Exception as exc:
            log.exception("fusion debug failed to load fusion")
            await ctx.reply(f"Fusion config error: {exc}", mention_author=False)
            return

        if active is None:
            await ctx.reply("No fusion rows found.", mention_author=False)
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

    @tier("admin")
    @help_metadata(
        function_group="milestones",
        section="community",
        access_tier="admin",
        usage="!fusion publish",
    )
    @fusion.command(name="publish", help="Publish fusion announcement to the configured channel.")
    @commands.guild_only()
    @admin_only()
    async def fusion_publish(self, ctx: commands.Context) -> None:
        try:
            target = await fusion_sheets.get_publishable_fusion()
        except Exception as exc:
            log.exception("fusion publish failed to load fusion rows")
            await ctx.reply(f"Could not load fusion data: {exc}", mention_author=False)
            return

        if target is None:
            await ctx.reply("No fusion rows exist in the configured fusion sheet tab.", mention_author=False)
            return

        missing_fields: list[str] = []
        if target.announcement_channel_id is None:
            missing_fields.append("announcement_channel_id")
        if not target.fusion_name:
            missing_fields.append("fusion_name")
        if not target.champion:
            missing_fields.append("champion")
        if target.start_at_utc is None:
            missing_fields.append("start_at_utc")
        if target.end_at_utc is None:
            missing_fields.append("end_at_utc")

        if missing_fields:
            await ctx.reply(
                "Fusion row is missing required fields: " + ", ".join(missing_fields),
                mention_author=False,
            )
            return

        if target.announcement_message_id is not None:
            await ctx.reply(
                "This fusion already has an announcement post. Clear the message id or use a future republish flow.",
                mention_author=False,
            )
            return

        channel = self.bot.get_channel(target.announcement_channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(target.announcement_channel_id)
            except Exception as exc:
                log.exception(
                    "fusion publish failed to resolve channel",
                    extra={"channel_id": target.announcement_channel_id, "fusion_id": target.fusion_id},
                )
                await ctx.reply(
                    f"Configured announcement_channel_id ({target.announcement_channel_id}) could not be resolved: {exc}",
                    mention_author=False,
                )
                return

        if not isinstance(channel, discord.abc.Messageable):
            await ctx.reply("Configured announcement channel is not messageable.", mention_author=False)
            return

        try:
            events = await fusion_sheets.get_fusion_events(target.fusion_id)
            embed = build_fusion_announcement_embed(target, events)
            announcement_message = await channel.send(embed=embed)
        except Exception as exc:
            log.exception("fusion publish failed during announce send", extra={"fusion_id": target.fusion_id})
            await ctx.reply(f"Failed to publish announcement: {exc}", mention_author=False)
            return

        set_status_published = target.status.casefold() == "draft"
        try:
            await fusion_sheets.update_fusion_publication(
                target.fusion_id,
                announcement_message_id=announcement_message.id,
                published_at=dt.datetime.now(dt.timezone.utc),
                set_published_status=set_status_published,
            )
        except Exception as exc:
            log.exception("fusion publish metadata write-back failed", extra={"fusion_id": target.fusion_id})
            await ctx.reply(
                f"Announcement posted but sheet write-back failed: {exc}. Please update publication columns manually.",
                mention_author=False,
            )
            return

        destination = channel.mention if isinstance(channel, discord.abc.GuildChannel) else "configured channel"
        await ctx.reply(
            f"Fusion announcement published to {destination} for **{target.fusion_name}**.",
            mention_author=False,
        )
