"""Remove orphan organizer-preview copies when a stage becomes official.

The authoritative retirement path owns the registered preview message and Sheet
resource state. This module only performs defensive duplicate cleanup around that
boundary. It deliberately derives the duplicate signature from the already-registered
Discord preview instead of rebuilding a fresh Sheet snapshot: retirement must stay
cheap, quota-safe, and incapable of turning a harmless orphan scan into a tournament
ERROR.
"""

from __future__ import annotations

import logging

import discord

from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.preview_retirement_cleanup")
_installed = False


async def _resolve_channel(manager, channel_id: str):
    if not channel_id:
        return None
    channel = manager.bot.get_channel(int(channel_id))
    if channel is None:
        channel = await manager.bot.fetch_channel(int(channel_id))
    return channel


async def _delete_exact_preview_copies(
    manager,
    embed: discord.Embed,
    *,
    channel=None,
    canonical=None,
) -> int:
    """Delete exact bot-authored duplicates while leaving the canonical to retirement."""
    from modules.community.live_arena import preview_message_guard

    if channel is None:
        return 0
    matches = await preview_message_guard._matching_bot_messages(
        channel,
        manager.bot,
        embed,
        current=canonical,
    )
    canonical_id = _text(getattr(canonical, "id", ""))
    deleted = 0
    for message in matches:
        message_id = _text(getattr(message, "id", ""))
        if canonical_id and message_id == canonical_id:
            continue
        try:
            await message.delete()
            deleted += 1
        except discord.NotFound:
            pass
        except Exception as exc:
            log.warning(
                "Live Arena retired preview duplicate cleanup skipped message • "
                "message=%s • error=%s: %s",
                message_id,
                type(exc).__name__,
                exc,
            )
    if deleted:
        log.warning(
            "Live Arena orphan organizer preview copies removed during retirement • count=%s",
            deleted,
        )
    return deleted


async def _registered_preview(manager, service, resource_type: str, resource_key: str):
    """Return the registered canonical preview without reconstructing round state."""
    config = getattr(getattr(service, "repository", None), "config", None) or {}
    tournament_id = _text(config.get("ACTIVE_TOURNAMENT_ID"))
    if not tournament_id:
        return None, None, None

    resource = await service.registration_repository.discord_resource(
        tournament_id,
        resource_type,
        resource_key,
    )
    if not resource or _text(resource.get("state")).lower() != "active":
        return resource, None, None

    channel_id = _text(resource.get("channel_id"))
    message_id = _text(resource.get("message_id"))
    if not channel_id or not message_id:
        return resource, None, None

    channel = await _resolve_channel(manager, channel_id)
    if channel is None:
        return resource, None, None
    try:
        message = await channel.fetch_message(int(message_id))
    except discord.NotFound:
        return resource, channel, None

    bot_id = _text(getattr(getattr(manager.bot, "user", None), "id", ""))
    author_id = _text(getattr(getattr(message, "author", None), "id", ""))
    if bot_id and author_id and author_id != bot_id:
        return resource, channel, None

    embeds = list(getattr(message, "embeds", ()) or ())
    if len(embeds) != 1:
        return resource, channel, None
    return resource, channel, message


async def _cleanup_registered_preview(
    manager,
    service,
    *,
    resource_type: str,
    resource_key: str,
    label: str,
) -> int:
    """Best-effort orphan cleanup that never owns the authoritative retirement."""
    try:
        _, channel, canonical = await _registered_preview(
            manager,
            service,
            resource_type,
            resource_key,
        )
        if channel is None or canonical is None:
            return 0
        return await _delete_exact_preview_copies(
            manager,
            canonical.embeds[0],
            channel=channel,
            canonical=canonical,
        )
    except Exception as exc:
        # This is intentionally defensive. The normal retirement still runs next
        # and remains the only authoritative mutation of the preview resource.
        log.warning(
            "Live Arena retired-preview duplicate scan skipped • %s • error=%s: %s",
            label,
            type(exc).__name__,
            exc,
        )
        return 0


async def _cleanup_swiss_preview(manager, service, number: int) -> int:
    return await _cleanup_registered_preview(
        manager,
        service,
        resource_type="swiss_preview",
        resource_key=f"q{number}",
        label=f"round={number}",
    )


async def _cleanup_knockout_preview(manager, service, stage: str) -> int:
    return await _cleanup_registered_preview(
        manager,
        service,
        resource_type="knockout_preview",
        resource_key=stage,
        label=f"stage={stage}",
    )


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
