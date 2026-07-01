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
    error: Exception | None = None


class FusionAnnouncementPermissionError(RuntimeError):
    """Raised when the bot cannot fetch or edit a stored announcement."""


class FusionAnnouncementMissingError(RuntimeError):
    """Raised when a stored announcement message id no longer resolves."""


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
        return AnnouncementResolution(
            message=None, had_reference=had_reference, is_stale=had_reference
        )

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
        return AnnouncementResolution(
            message=message, had_reference=True, is_stale=False
        )
    except discord.NotFound as exc:
        log.warning(
            "fusion stored announcement message is missing",
            extra={
                "fusion_id": target.fusion_id,
                "status": target.status,
                "announcement_channel_id": target.announcement_channel_id,
                "announcement_message_id": target.announcement_message_id,
            },
            exc_info=True,
        )
        return AnnouncementResolution(
            message=None, had_reference=True, is_stale=True, error=exc
        )
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.error(
            "fusion stored announcement message could not be fetched",
            extra={
                "fusion_id": target.fusion_id,
                "status": target.status,
                "announcement_channel_id": target.announcement_channel_id,
                "announcement_message_id": target.announcement_message_id,
            },
            exc_info=True,
        )
        return AnnouncementResolution(
            message=None, had_reference=True, is_stale=False, error=exc
        )
    except Exception as exc:
        log.warning(
            "fusion stale announcement metadata detected (message unresolvable)",
            extra={
                "fusion_id": target.fusion_id,
                "status": target.status,
                "announcement_channel_id": target.announcement_channel_id,
                "announcement_message_id": target.announcement_message_id,
            },
            exc_info=True,
        )
        return AnnouncementResolution(
            message=None, had_reference=True, is_stale=True, error=exc
        )


async def edit_fusion_announcement(
    message: discord.Message,
    target: fusion_sheets.FusionRow,
) -> discord.Message:
    events = await fusion_sheets.get_fusion_events(target.fusion_id)
    announcement_embed = build_fusion_announcement_embed(target, events)
    announcement_view = build_fusion_opt_in_view(target)
    try:
        return await message.edit(embed=announcement_embed, view=announcement_view)
    except discord.NotFound as exc:
        log.warning(
            "fusion stored announcement message disappeared before edit",
            extra={
                "fusion_id": target.fusion_id,
                "announcement_channel_id": target.announcement_channel_id,
                "announcement_message_id": target.announcement_message_id,
            },
            exc_info=True,
        )
        raise FusionAnnouncementMissingError(
            "Stored fusion announcement message is missing"
        ) from exc
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.error(
            "fusion stored announcement message could not be edited",
            extra={
                "fusion_id": target.fusion_id,
                "announcement_channel_id": target.announcement_channel_id,
                "announcement_message_id": target.announcement_message_id,
            },
            exc_info=True,
        )
        raise FusionAnnouncementPermissionError(
            "Bot cannot edit stored fusion announcement message"
        ) from exc


async def refresh_fusion_announcement(
    bot: commands.Bot,
    target: fusion_sheets.FusionRow,
) -> discord.Message:
    if target.announcement_message_id is None:
        raise FusionAnnouncementMissingError(
            "Fusion row has no announcement_message_id"
        )
    resolution = await resolve_stored_announcement(bot, target)
    if resolution.message is None:
        if isinstance(
            resolution.error, (discord.Forbidden, discord.HTTPException)
        ) and not isinstance(resolution.error, discord.NotFound):
            raise FusionAnnouncementPermissionError(
                "Bot cannot fetch stored fusion announcement message"
            ) from resolution.error
        raise FusionAnnouncementMissingError(
            "Stored fusion announcement message is missing"
        ) from resolution.error
    return await edit_fusion_announcement(resolution.message, target)


async def publish_fusion_announcement(
    bot: commands.Bot,
    target: fusion_sheets.FusionRow,
) -> discord.Message | None:
    channel = await resolve_announcement_channel(bot, target.announcement_channel_id)
    if channel is None:
        return None

    resolution = await resolve_stored_announcement(bot, target)
    if resolution.message is not None:
        edited = await edit_fusion_announcement(resolution.message, target)
        log.info(
            "fusion existing announcement edited during publish",
            extra={
                "fusion_id": target.fusion_id,
                "announcement_channel_id": target.announcement_channel_id,
                "announcement_message_id": target.announcement_message_id,
            },
        )
        return edited
    if (
        resolution.had_reference
        and isinstance(resolution.error, (discord.Forbidden, discord.HTTPException))
        and not isinstance(resolution.error, discord.NotFound)
    ):
        raise FusionAnnouncementPermissionError(
            "Bot cannot fetch stored fusion announcement message"
        ) from resolution.error
    if resolution.had_reference and resolution.is_stale:
        log.warning(
            "fusion stored announcement missing during publish; creating clearly logged replacement",
            extra={
                "fusion_id": target.fusion_id,
                "announcement_channel_id": target.announcement_channel_id,
                "announcement_message_id": target.announcement_message_id,
            },
        )

    events = await fusion_sheets.get_fusion_events(target.fusion_id)
    announcement_embed = build_fusion_announcement_embed(target, events)
    announcement_view = build_fusion_opt_in_view(target)
    try:
        announcement_message = await channel.send(
            embed=announcement_embed, view=announcement_view
        )
    except discord.HTTPException:
        image_url = str(
            getattr(getattr(announcement_embed, "image", None), "url", "") or ""
        ).strip()
        if not image_url:
            raise
        log.warning(
            "fusion announcement send failed with champion image; retrying without image",
            extra={"fusion_id": target.fusion_id, "champion_image_url": image_url},
            exc_info=True,
        )
        fallback_embed = announcement_embed.copy()
        fallback_embed.set_image(url=None)
        announcement_message = await channel.send(
            embed=fallback_embed, view=announcement_view
        )

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

    status = str(target.status or "").strip().casefold()
    was_published = target.published_at is not None
    if status not in _VALID_PUBLISHED_STATUSES or not was_published:
        return None

    return await publish_fusion_announcement(bot, target)


__all__ = [
    "ensure_fusion_announcement",
    "resolve_stored_announcement",
    "publish_fusion_announcement",
    "edit_fusion_announcement",
    "refresh_fusion_announcement",
    "FusionAnnouncementMissingError",
    "FusionAnnouncementPermissionError",
    "resolve_announcement_channel",
]
