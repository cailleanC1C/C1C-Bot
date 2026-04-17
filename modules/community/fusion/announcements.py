"""Fusion announcement resolution/publish helpers shared by commands and jobs."""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass

import discord
from discord.ext import commands

from modules.community.fusion.opt_in_view import build_fusion_opt_in_view
from modules.community.fusion.rendering import build_fusion_announcement_embed
from shared.sheets import fusion as fusion_sheets

log = logging.getLogger("c1c.community.fusion.announcements")


_VALID_PUBLISHED_STATUSES = {"active", "published"}


@dataclass(slots=True)
class AnnouncementResolution:
    message: discord.Message | None
    had_reference: bool
    is_stale: bool


async def resolve_announcement_channel(
    bot: commands.Bot,
    channel_id: int | None,
) -> discord.abc.Messageable | None:
    if channel_id is None:
        return None
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return None
    if not isinstance(channel, discord.abc.Messageable):
        return None
    return channel


async def resolve_stored_announcement(
    bot: commands.Bot,
    target: fusion_sheets.FusionRow,
) -> AnnouncementResolution:
    has_message_id = target.announcement_message_id is not None
    has_channel_id = target.announcement_channel_id is not None
    had_reference = has_message_id or has_channel_id
    if not has_message_id or not has_channel_id:
        return AnnouncementResolution(message=None, had_reference=had_reference, is_stale=had_reference)

    if target.status.casefold() not in _VALID_PUBLISHED_STATUSES:
        log.info(
            "fusion announcement metadata treated as stale because status is not publishable",
            extra={
                "fusion_id": target.fusion_id,
                "status": target.status,
                "announcement_channel_id": target.announcement_channel_id,
                "announcement_message_id": target.announcement_message_id,
            },
        )
        return AnnouncementResolution(message=None, had_reference=True, is_stale=True)

    channel = await resolve_announcement_channel(bot, target.announcement_channel_id)
    if channel is None:
        log.warning(
            "fusion stale announcement metadata detected (channel missing/unusable)",
            extra={
                "fusion_id": target.fusion_id,
                "status": target.status,
                "announcement_channel_id": target.announcement_channel_id,
                "announcement_message_id": target.announcement_message_id,
            },
        )
        return AnnouncementResolution(message=None, had_reference=True, is_stale=True)

    try:
        message = await channel.fetch_message(target.announcement_message_id)
        return AnnouncementResolution(message=message, had_reference=True, is_stale=False)
    except Exception:
        log.warning(
            "fusion stale announcement metadata detected (message missing/unresolvable)",
            extra={
                "fusion_id": target.fusion_id,
                "status": target.status,
                "announcement_channel_id": target.announcement_channel_id,
                "announcement_message_id": target.announcement_message_id,
            },
            exc_info=True,
        )
        return AnnouncementResolution(message=None, had_reference=True, is_stale=True)


async def publish_fusion_announcement(
    bot: commands.Bot,
    target: fusion_sheets.FusionRow,
) -> discord.Message | None:
    channel = await resolve_announcement_channel(bot, target.announcement_channel_id)
    if channel is None:
        return None

    events = await fusion_sheets.get_fusion_events(target.fusion_id)
    announcement_embed = build_fusion_announcement_embed(target, events)
    announcement_view = build_fusion_opt_in_view(target)
    try:
        announcement_message = await channel.send(embed=announcement_embed, view=announcement_view)
    except discord.HTTPException:
        image_url = str(getattr(getattr(announcement_embed, "image", None), "url", "") or "").strip()
        if not image_url:
            raise
        log.warning(
            "fusion announcement send failed with champion image; retrying without image",
            extra={"fusion_id": target.fusion_id, "champion_image_url": image_url},
            exc_info=True,
        )
        fallback_embed = announcement_embed.copy()
        fallback_embed.set_image(url=None)
        announcement_message = await channel.send(embed=fallback_embed, view=announcement_view)

    set_status_published = target.status.casefold() != "published"
    await fusion_sheets.update_fusion_publication(
        target.fusion_id,
        announcement_message_id=announcement_message.id,
        announcement_channel_id=channel.id,
        published_at=dt.datetime.now(dt.timezone.utc),
        set_published_status=set_status_published,
    )
    log.info(
        "fusion fresh announcement metadata written back",
        extra={
            "fusion_id": target.fusion_id,
            "announcement_channel_id": channel.id,
            "announcement_message_id": announcement_message.id,
            "set_published_status": set_status_published,
        },
    )
    return announcement_message


async def ensure_fusion_announcement(
    bot: commands.Bot,
    target: fusion_sheets.FusionRow,
) -> discord.Message | None:
    channel = await resolve_announcement_channel(bot, target.announcement_channel_id)
    if channel is None:
        return None

    resolution = await resolve_stored_announcement(bot, target)
    if resolution.message is not None:
        return resolution.message

    return await publish_fusion_announcement(bot, target)


__all__ = [
    "ensure_fusion_announcement",
    "resolve_stored_announcement",
    "publish_fusion_announcement",
    "resolve_announcement_channel",
]
