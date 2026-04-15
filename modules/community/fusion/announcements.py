"""Fusion announcement resolution/publish helpers shared by commands and jobs."""

from __future__ import annotations

import datetime as dt

import discord
from discord.ext import commands

from modules.community.fusion.opt_in_view import build_fusion_opt_in_view
from modules.community.fusion.rendering import build_fusion_announcement_embed
from shared.sheets import fusion as fusion_sheets


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
    announcement_message = await channel.send(embed=announcement_embed, view=announcement_view)

    set_status_published = target.status.casefold() == "draft"
    await fusion_sheets.update_fusion_publication(
        target.fusion_id,
        announcement_message_id=announcement_message.id,
        announcement_channel_id=channel.id,
        published_at=dt.datetime.now(dt.timezone.utc),
        set_published_status=set_status_published,
    )
    return announcement_message


async def ensure_fusion_announcement(
    bot: commands.Bot,
    target: fusion_sheets.FusionRow,
) -> discord.Message | None:
    channel = await resolve_announcement_channel(bot, target.announcement_channel_id)
    if channel is None:
        return None

    if target.announcement_message_id is not None:
        try:
            return await channel.fetch_message(target.announcement_message_id)
        except Exception:
            pass

    return await publish_fusion_announcement(bot, target)


__all__ = [
    "ensure_fusion_announcement",
    "publish_fusion_announcement",
    "resolve_announcement_channel",
]
