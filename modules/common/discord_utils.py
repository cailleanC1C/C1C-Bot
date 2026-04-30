"""Shared Discord channel/thread resolution helpers."""

from __future__ import annotations

import discord


async def resolve_message_target(bot: discord.Client, target_id: int) -> discord.abc.Messageable:
    """Resolve a configured message target from cache/API and ensure it can receive messages."""

    target = bot.get_channel(target_id)
    if target is None:
        try:
            target = await bot.fetch_channel(target_id)
        except discord.NotFound as exc:
            raise ValueError(f"target {target_id} was not found") from exc
        except discord.Forbidden as exc:
            raise PermissionError(f"missing permissions for target {target_id}") from exc
        except discord.HTTPException as exc:
            raise RuntimeError(f"failed to fetch target {target_id}: {exc}") from exc

    if not isinstance(target, (discord.TextChannel, discord.Thread)):
        raise TypeError(
            f"target {target_id} is not a supported message target "
            f"({type(target).__name__})"
        )

    if isinstance(target, discord.Thread) and target.archived:
        try:
            await target.edit(archived=False)
        except discord.NotFound as exc:
            raise ValueError(f"thread {target_id} was not found") from exc
        except discord.Forbidden as exc:
            raise PermissionError(f"missing permissions to unarchive thread {target_id}") from exc
        except discord.HTTPException as exc:
            raise RuntimeError(f"failed to unarchive thread {target_id}: {exc}") from exc

    return target


__all__ = ["resolve_message_target"]
