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

    async def _resolve_announcement_channel(
        self, channel_id: int | None
    ) -> discord.abc.Messageable | None:
        if channel_id is None:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                return None
        if not isinstance(channel, discord.abc.Messageable):
            return None
        return channel

    async def _post_fusion_announcement(
        self,
        target: fusion_sheets.FusionRow,
        channel: discord.abc.Messageable,
    ) -> discord.Message:
        events = await fusion_sheets.get_fusion_events(target.fusion_id)
        announcement_embed = build_fusion_announcement_embed(target, events)
        return await channel.send(embed=announcement_embed)

    async def _persist_fusion_publication(
        self,
        target: fusion_sheets.FusionRow,
        channel_id: int,
        message_id: int,
    ) -> None:
        set_status_published = target.status.casefold() == "draft"
        await fusion_sheets.update_fusion_publication(
            target.fusion_id,
            announcement_message_id=message_id,
            announcement_channel_id=channel_id,
            published_at=dt.datetime.now(dt.timezone.utc),
            set_published_status=set_status_published,
        )

    async def _publish_fusion_announcement(
        self,
        target: fusion_sheets.FusionRow,
    ) -> discord.Message | None:
        channel = await self._resolve_announcement_channel(target.announcement_channel_id)
        if channel is None:
            return None

        announcement_message = await self._post_fusion_announcement(target, channel)
        await self._persist_fusion_publication(target, channel.id, announcement_message.id)
        return announcement_message

    async def _ensure_fusion_announcement(
        self,
        target: fusion_sheets.FusionRow,
    ) -> discord.Message | None:
        channel = await self._resolve_announcement_channel(target.announcement_channel_id)
        if channel is None:
            return None

        if target.announcement_message_id is not None:
            try:
                return await channel.fetch_message(target.announcement_message_id)
            except Exception:
                pass

        return await self._publish_fusion_announcement(target)

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
        try:
            target = await fusion_sheets.get_publishable_fusion()
        except Exception:
            log.exception("fusion command failed to load fusion rows")
            await ctx.reply("Couldn’t check the fusion right now. Try again in a moment.", mention_author=False)
            return

        if target is None:
            await ctx.reply("No fusion running. Enjoy the peace while it lasts.", mention_author=False)
            return

        try:
            announcement_message = await self._ensure_fusion_announcement(target)
        except Exception:
            log.exception("fusion command failed to resolve announcement", extra={"fusion_id": target.fusion_id})
            announcement_message = None

        if announcement_message is not None:
            await ctx.reply(
                f"🔗 Fusion’s up. Don’t get lost:\n{announcement_message.jump_url}",
                mention_author=False,
            )
            return

        try:
            events = await fusion_sheets.get_fusion_events(target.fusion_id)
            emergency_embed = build_fusion_announcement_embed(target, events)
            await ctx.reply(embed=emergency_embed, mention_author=False)
            return
        except Exception:
            log.exception("fusion command emergency embed fallback failed", extra={"fusion_id": target.fusion_id})
            await ctx.reply("Couldn’t check the fusion right now. Try again in a moment.", mention_author=False)
            return

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

        channel = await self._resolve_announcement_channel(target.announcement_channel_id)
        if channel is None:
            await ctx.reply("Configured announcement channel is not messageable.", mention_author=False)
            return

        if target.announcement_message_id is not None:
            try:
                await channel.fetch_message(target.announcement_message_id)
                await ctx.reply(
                    "This fusion already has an announcement post. Clear the message id or use a future republish flow.",
                    mention_author=False,
                )
                return
            except Exception:
                pass

        try:
            announcement_message = await self._publish_fusion_announcement(target)
            if announcement_message is None:
                await ctx.reply("Configured announcement channel is not messageable.", mention_author=False)
                return
        except Exception as exc:
            log.exception("fusion publish failed during announce send", extra={"fusion_id": target.fusion_id})
            await ctx.reply(f"Failed to publish announcement: {exc}", mention_author=False)
            return

        destination = channel.mention if isinstance(channel, discord.abc.GuildChannel) else "configured channel"
        await ctx.reply(
            f"Fusion announcement published to {destination} for **{target.fusion_name}**.",
            mention_author=False,
        )
