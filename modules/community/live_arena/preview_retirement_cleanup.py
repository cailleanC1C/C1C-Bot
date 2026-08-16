"""Remove orphan organizer-preview copies when a stage becomes official.

The preview publication guard converges duplicates while a preview is being synced.
If old Sheet corruption blocks that sync until after the round is already public or
closed, retirement is the next safe repair boundary. Delete every exact bot-authored
copy of the preview, then let the normal retirement persist the canonical resource as
retired. This is generic to Swiss and knockout stages.
"""

from __future__ import annotations

import logging

import discord

from modules.community.live_arena.messages import load_pr5_config
from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.preview_retirement_cleanup")
_installed = False


async def _organizer_channel(manager):
    config, _ = await load_pr5_config(manager.sheet_id)
    channel_id = _text(config.get("ORGANIZER_CHANNEL_ID"))
    if not channel_id:
        return None
    channel = manager.bot.get_channel(int(channel_id))
    if channel is None:
        channel = await manager.bot.fetch_channel(int(channel_id))
    return channel


async def _delete_exact_preview_copies(manager, embed: discord.Embed) -> int:
    from modules.community.live_arena import preview_message_guard

    channel = await _organizer_channel(manager)
    if channel is None:
        return 0
    matches = await preview_message_guard._matching_bot_messages(
        channel, manager.bot, embed
    )
    deleted = 0
    for message in matches:
        try:
            await message.delete()
            deleted += 1
        except discord.NotFound:
            pass
        except Exception:
            log.exception(
                "Live Arena retired preview duplicate cleanup failed • message=%s",
                _text(getattr(message, "id", "")),
            )
    if deleted > 1:
        log.warning(
            "Live Arena orphan organizer preview copies removed during retirement • count=%s",
            deleted,
        )
    return deleted


async def _cleanup_swiss_preview(manager, service, number: int) -> int:
    try:
        snapshot = await service.snapshot(number)
        from modules.community.live_arena import swiss_runtime

        return await _delete_exact_preview_copies(
            manager,
            swiss_runtime.preview_embed(snapshot, official=False),
        )
    except Exception:
        # Retirement itself remains authoritative and must still run if the defensive
        # history cleanup cannot reconstruct or inspect the preview.
        log.exception(
            "Live Arena Swiss retired-preview cleanup failed • round=%s", number
        )
        return 0


async def _cleanup_knockout_preview(manager, service, stage: str) -> int:
    try:
        snapshot = await service.snapshot(stage)
        from modules.community.live_arena import knockout_runtime

        return await _delete_exact_preview_copies(
            manager,
            knockout_runtime._preview_embed(snapshot),
        )
    except Exception:
        log.exception(
            "Live Arena knockout retired-preview cleanup failed • stage=%s", stage
        )
        return 0


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import knockout_runtime, swiss_runtime

    original_swiss_retire = swiss_runtime._retire_preview_message
    original_knockout_retire = knockout_runtime._retire_preview_message

    async def retire_swiss_preview(manager, service, number: int):
        await _cleanup_swiss_preview(manager, service, number)
        return await original_swiss_retire(manager, service, number)

    async def retire_knockout_preview(manager, service, stage: str):
        await _cleanup_knockout_preview(manager, service, stage)
        return await original_knockout_retire(manager, service, stage)

    swiss_runtime._retire_preview_message = retire_swiss_preview
    knockout_runtime._retire_preview_message = retire_knockout_preview
