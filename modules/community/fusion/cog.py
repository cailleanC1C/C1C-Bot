"""Fusion debug and publish commands."""

from __future__ import annotations

import logging

import discord
from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only
from modules.community.fusion import logs as fusion_logs
from modules.community.fusion.announcements import (
    ensure_fusion_announcement,
    publish_fusion_announcement,
    resolve_announcement_channel,
    resolve_stored_announcement,
)
from modules.community.fusion.rendering import build_fusion_announcement_embed
from shared.sheets import fusion as fusion_sheets

log = logging.getLogger("c1c.community.fusion")


class FusionCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def _ensure_fusion_announcement(
        self,
        target: fusion_sheets.FusionRow,
    ) -> discord.Message | None:
        return await ensure_fusion_announcement(self.bot, target)

    async def _tracker_entrypoint(
        self,
        ctx: commands.Context,
        *,
        tracker_kind: str,
        tracker_label: str,
    ) -> None:
        try:
            target = await fusion_sheets.get_publishable_fusion(tracker_kind=tracker_kind)
        except Exception:
            log.exception("%s command failed to load rows", tracker_label)
            await ctx.reply(f"Couldn’t check the {tracker_label} right now. Try again in a moment.", mention_author=False)
            return

        if target is None:
            await ctx.reply(f"No {tracker_label} running. Enjoy the peace while it lasts.", mention_author=False)
            return

        try:
            announcement_message = await self._ensure_fusion_announcement(target)
        except Exception:
            log.exception("%s command failed to resolve announcement", tracker_label, extra={"fusion_id": target.fusion_id})
            announcement_message = None

        if announcement_message is not None:
            await ctx.reply(
                f"🔗 {tracker_label.title()}’s up. Don’t get lost:\n{announcement_message.jump_url}",
                mention_author=False,
            )
            return

        try:
            events = await fusion_sheets.get_fusion_events(target.fusion_id)
            emergency_embed = build_fusion_announcement_embed(target, events)
            await ctx.reply(embed=emergency_embed, mention_author=False)
            return
        except Exception:
            log.exception("%s command emergency embed fallback failed", tracker_label, extra={"fusion_id": target.fusion_id})
            await ctx.reply(f"Couldn’t check the {tracker_label} right now. Try again in a moment.", mention_author=False)
            return

    async def _publish_tracker(
        self,
        ctx: commands.Context,
        *,
        tracker_kind: str,
        tracker_label: str,
        prefer_draft: bool,
    ) -> None:
        try:
            target = await fusion_sheets.get_publishable_fusion(
                include_draft=True,
                tracker_kind=tracker_kind,
                prefer_draft=prefer_draft,
            )
        except Exception as exc:
            log.exception("%s publish failed to load rows", tracker_label)
            await fusion_logs.send_ops_alert(
                component="command_publish",
                summary=f"load_{tracker_label}_failed",
                dedupe_key=f"fusion:command:publish:load_{tracker_label}",
                error=exc,
            )
            await ctx.reply(f"Could not load {tracker_label} data right now.", mention_author=False)
            return

        if target is None:
            await ctx.reply(
                f"No {tracker_label} rows exist in the configured fusion sheet tab.",
                mention_author=False,
            )
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
                f"{tracker_label.title()} row is missing required fields: " + ", ".join(missing_fields),
                mention_author=False,
            )
            return

        channel = await resolve_announcement_channel(self.bot, target.announcement_channel_id)
        if channel is None:
            await ctx.reply("Configured announcement channel is not messageable.", mention_author=False)
            return

        resolution = await resolve_stored_announcement(self.bot, target)
        if resolution.message is not None:
            await ctx.reply(
                f"This {tracker_label} already has an announcement post. Clear the message id or use a future republish flow.",
                mention_author=False,
            )
            return
        if resolution.had_reference and resolution.is_stale:
            log.info(
                "%s publish allowed despite stale announcement metadata",
                tracker_label,
                extra={
                    "fusion_id": target.fusion_id,
                    "status": target.status,
                    "announcement_channel_id": target.announcement_channel_id,
                    "announcement_message_id": target.announcement_message_id,
                },
            )

        try:
            event_count = len(await fusion_sheets.get_fusion_events(target.fusion_id))
            announcement_message = await publish_fusion_announcement(self.bot, target)
            if announcement_message is None:
                await ctx.reply("Configured announcement channel is not messageable.", mention_author=False)
                return
        except Exception as exc:
            log.exception(
                "%s publish failed during announce send",
                tracker_label,
                extra={
                    "fusion_id": target.fusion_id,
                    "tracker_kind": tracker_kind,
                    "event_count": event_count if "event_count" in locals() else None,
                    "target_channel_id": getattr(target, "announcement_channel_id", None),
                    "announcement_channel_id": getattr(target, "announcement_channel_id", None),
                    "announcement_message_id": getattr(target, "announcement_message_id", None),
                },
                exc_info=True,
            )
            await fusion_logs.send_ops_alert(
                component="command_publish",
                summary="announce_send_failed",
                dedupe_key=f"fusion:command:publish:send:{tracker_label}:{target.fusion_id}",
                error=exc,
                fields={"fusion_id": target.fusion_id, "tracker_kind": tracker_kind},
            )
            await ctx.reply("Failed to publish announcement right now.", mention_author=False)
            return

        destination = channel.mention if isinstance(channel, discord.abc.GuildChannel) else "configured channel"
        await ctx.reply(
            f"{tracker_label.title()} announcement published to {destination} for **{target.fusion_name}**.",
            mention_author=False,
        )

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
        await self._tracker_entrypoint(ctx, tracker_kind="fusion", tracker_label="fusion")

    @tier("user")
    @help_metadata(
        function_group="milestones",
        section="community",
        access_tier="user",
        usage="!titan",
    )
    @commands.group(
        name="titan",
        invoke_without_command=True,
        help="Titan reminder data commands.",
    )
    async def titan(self, ctx: commands.Context) -> None:
        await self._tracker_entrypoint(ctx, tracker_kind="titan", tracker_label="titan")

    @tier("admin")
    @help_metadata(
        function_group="milestones",
        section="community",
        access_tier="admin",
        usage="!fusion debug",
    )
    @fusion.command(name="debug", help="Debug active fusion + first events from sheets cache.")
    @commands.guild_only()
    @admin_only()
    async def fusion_debug(self, ctx: commands.Context) -> None:
        try:
            active = await fusion_sheets.get_publishable_fusion(include_draft=True)
        except Exception as exc:
            log.exception("fusion debug failed to load fusion")
            await fusion_logs.send_ops_alert(
                component="command_debug",
                summary="load_fusion_failed",
                dedupe_key="fusion:command:debug:load_fusion",
                error=exc,
            )
            await ctx.reply("Fusion debug is temporarily unavailable.", mention_author=False)
            return

        if active is None:
            await ctx.reply("No fusion rows found.", mention_author=False)
            return

        try:
            events = await fusion_sheets.get_fusion_events(active.fusion_id)
        except Exception as exc:
            log.exception("fusion debug failed to load events", extra={"fusion_id": active.fusion_id})
            await fusion_logs.send_ops_alert(
                component="command_debug",
                summary="load_events_failed",
                dedupe_key=f"fusion:command:debug:events:{active.fusion_id}",
                error=exc,
                fields={"fusion_id": active.fusion_id},
            )
            await ctx.reply("Fusion events are temporarily unavailable.", mention_author=False)
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
        await self._publish_tracker(
            ctx,
            tracker_kind="fusion",
            tracker_label="fusion",
            prefer_draft=True,
        )

    @tier("admin")
    @help_metadata(
        function_group="milestones",
        section="community",
        access_tier="admin",
        usage="!titan publish",
    )
    @titan.command(name="publish", help="Publish titan announcement to the configured channel.")
    @commands.guild_only()
    @admin_only()
    async def titan_publish(self, ctx: commands.Context) -> None:
        await self._publish_tracker(
            ctx,
            tracker_kind="titan",
            tracker_label="titan",
            prefer_draft=True,
        )
