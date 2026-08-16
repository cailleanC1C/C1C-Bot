"""Race-safe Discord publication for organizer-only Live Arena previews.

Startup reconciliation, post-close progression, and Captain's Table rendering can
all converge on the same preview at nearly the same time. The Sheet resource row
is authoritative across restarts, but a surrounding read scope can temporarily
return a pre-write resource snapshot. This guard serializes in-process publication,
keeps a tiny write-through canonical-message cache, and converges duplicate Discord
messages deterministically if two publishers ever overlap.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

import discord

from modules.community.live_arena.messages import load_pr5_config
from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.preview_message_guard")
_installed = False
_locks: dict[tuple[str, str, str, str], asyncio.Lock] = {}
_canonical_message_ids: dict[tuple[str, str, str, str], tuple[str, str]] = {}


def _identity(sheet_id: str, tournament_id: str, resource_type: str, resource_key: str):
    return (
        str(sheet_id or "").strip(),
        str(tournament_id or "").strip(),
        str(resource_type or "").strip(),
        str(resource_key or "").strip(),
    )


def _embed_signature(embed: discord.Embed) -> str:
    """Stable signature used only to identify exact duplicate preview messages."""
    payload = embed.to_dict()
    # Preview embeds intentionally carry no volatile timestamp. If a later UX layer
    # adds one, it must not prevent duplicate convergence.
    payload.pop("timestamp", None)
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


async def _resolve_channel(manager, channel_id: str):
    channel = manager.bot.get_channel(int(channel_id))
    if channel is None:
        channel = await manager.bot.fetch_channel(int(channel_id))
    return channel


async def _fetch_message(channel, message_id: str):
    if not message_id:
        return None
    try:
        return await channel.fetch_message(int(message_id))
    except discord.NotFound:
        return None


async def _matching_bot_messages(channel, bot, embed: discord.Embed, current=None):
    """Return exact bot-authored duplicate previews visible in recent history."""
    signature = _embed_signature(embed)
    found: dict[int, object] = {}

    if current is not None:
        try:
            found[int(current.id)] = current
        except (TypeError, ValueError, AttributeError):
            pass

    history = getattr(channel, "history", None)
    bot_id = _text(getattr(getattr(bot, "user", None), "id", ""))
    if not callable(history) or not bot_id:
        return list(found.values())

    try:
        async for message in history(limit=75):
            author_id = _text(getattr(getattr(message, "author", None), "id", ""))
            if author_id != bot_id:
                continue
            embeds = list(getattr(message, "embeds", ()) or ())
            if len(embeds) != 1 or _embed_signature(embeds[0]) != signature:
                continue
            try:
                found[int(message.id)] = message
            except (TypeError, ValueError, AttributeError):
                continue
    except Exception:
        # Duplicate cleanup is defensive. Failure to inspect history must never make
        # the canonical preview itself unavailable.
        log.exception("Live Arena preview duplicate history scan failed")

    return list(found.values())


async def _converge_duplicates(channel, bot, embed: discord.Embed, current):
    """Keep the oldest exact duplicate and delete later orphan copies."""
    matches = await _matching_bot_messages(channel, bot, embed, current=current)
    if not matches:
        return current

    def message_id(message):
        try:
            return int(message.id)
        except (TypeError, ValueError, AttributeError):
            return 2**63 - 1

    canonical = min(matches, key=message_id)
    for duplicate in matches:
        if message_id(duplicate) == message_id(canonical):
            continue
        try:
            await duplicate.delete()
            log.warning(
                "Live Arena duplicate organizer preview removed • kept=%s • removed=%s",
                message_id(canonical),
                message_id(duplicate),
            )
        except discord.NotFound:
            pass
        except Exception:
            log.exception(
                "Live Arena duplicate organizer preview cleanup failed • kept=%s • duplicate=%s",
                message_id(canonical),
                message_id(duplicate),
            )
    return canonical


async def _sync_preview_resource(
    manager,
    service,
    snapshot,
    *,
    resource_type: str,
    resource_key: str,
    embed: discord.Embed,
    notes: str,
):
    """Create/edit exactly one organizer preview and persist its canonical ID."""
    tournament_id = _text(snapshot.round_row.get("tournament_id"))
    identity = _identity(manager.sheet_id, tournament_id, resource_type, resource_key)
    lock = _locks.setdefault(identity, asyncio.Lock())

    async with lock:
        config, _ = await load_pr5_config(manager.sheet_id)
        channel_id = _text(config.get("ORGANIZER_CHANNEL_ID"))
        if not channel_id:
            raise RuntimeError("CONFIG: missing ORGANIZER_CHANNEL_ID for tournament preview")
        channel = await _resolve_channel(manager, channel_id)

        resource = await service.registration_repository.discord_resource(
            tournament_id, resource_type, resource_key
        )

        message = None
        cached = _canonical_message_ids.get(identity)
        if cached and cached[0] == channel_id:
            message = await _fetch_message(channel, cached[1])
            if message is None:
                _canonical_message_ids.pop(identity, None)

        if message is None and resource and _text(resource.get("state")) == "active":
            message = await _fetch_message(channel, _text(resource.get("message_id")))

        # Adoption before creation repairs an orphan from an interrupted write and
        # also lets a restarted process converge an already-duplicated live preview.
        if message is None:
            existing = await _matching_bot_messages(channel, manager.bot, embed)
            if existing:
                message = min(existing, key=lambda item: int(item.id))

        created = False
        if message is None:
            message = await channel.send(embed=embed)
            created = True
        else:
            await message.edit(embed=embed)

        canonical = await _converge_duplicates(channel, manager.bot, embed, message)
        if canonical is not message:
            # The deterministic older copy wins. Ensure it carries current content.
            await canonical.edit(embed=embed)

        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        try:
            await service.registration_repository.upsert_discord_resource(
                tournament_id=tournament_id,
                resource_type=resource_type,
                resource_key=resource_key,
                channel_id=channel_id,
                message_id=str(canonical.id),
                created_at_utc=(
                    _text(resource.get("created_at_utc")) if resource else now
                ),
                updated_at_utc=now,
                state="active",
                notes=notes,
            )
        except Exception:
            # Do not knowingly leave a brand-new untracked preview behind when its
            # resource row cannot be persisted. Existing/adopted messages are left
            # intact because they may already be the registered canonical resource.
            if created and canonical is message:
                try:
                    await message.delete()
                except Exception:
                    log.exception("Live Arena untracked organizer preview cleanup failed")
            _canonical_message_ids.pop(identity, None)
            raise

        _canonical_message_ids[identity] = (channel_id, str(canonical.id))
        return canonical


def install() -> None:
    """Install before later Swiss UI decorators so their behavior is preserved."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import knockout_runtime, swiss_runtime

    async def sync_swiss_preview(manager, service, snapshot):
        number = int(_text(snapshot.round_row.get("round_number")))
        return await _sync_preview_resource(
            manager,
            service,
            snapshot,
            resource_type="swiss_preview",
            resource_key=f"q{number}",
            embed=swiss_runtime.preview_embed(snapshot, official=False),
            notes="Organizer-only Swiss preview; not official until approved/published",
        )

    async def sync_knockout_preview(manager, service, snapshot):
        stage = _text(snapshot.round_row.get("round_stage")).lower()
        return await _sync_preview_resource(
            manager,
            service,
            snapshot,
            resource_type="knockout_preview",
            resource_key=stage,
            embed=knockout_runtime._preview_embed(snapshot),
            notes="Organizer-only knockout preview; no player-facing resources until approval",
        )

    swiss_runtime._sync_preview_message = sync_swiss_preview
    knockout_runtime._sync_preview_message = sync_knockout_preview
