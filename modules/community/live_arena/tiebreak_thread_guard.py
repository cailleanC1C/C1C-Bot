"""Race-safe publication and cleanup for qualification tiebreak threads.

Captain's Table reconciliation can run from startup, panel refreshes, and result
follow-ups at nearly the same time. A persisted MATCHES.thread_id is authoritative,
but multiple processes can still observe it blank before any one of them writes.
This guard converges those races on one bot-created forum thread and repairs the
already-duplicated live state without touching user-authored threads.
"""

from __future__ import annotations

import asyncio
import logging

import discord

from modules.community.live_arena import captains_table_runtime_repair as runtime
from modules.community.live_arena import captains_table_control_center as control
from modules.community.live_arena import knockout
from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.tiebreak_thread_guard")
_installed = False
_locks: dict[tuple[str, str, str], asyncio.Lock] = {}


def _identity(sheet_id: str, tournament_id: str, match_id: str) -> tuple[str, str, str]:
    return (
        str(sheet_id or "").strip(),
        str(tournament_id or "").strip(),
        str(match_id or "").strip(),
    )


def _expected_name(match: dict[str, object]) -> str:
    return (
        f"Qualification Tiebreak • {_text(match.get('player_a_display_name'))} vs "
        f"{_text(match.get('player_b_display_name'))}"
    )[:100]


def _thread_id(thread) -> int:
    try:
        return int(thread.id)
    except (TypeError, ValueError, AttributeError):
        return 2**63 - 1


def _same_forum(thread, forum) -> bool:
    parent_id = _text(getattr(thread, "parent_id", ""))
    forum_id = _text(getattr(forum, "id", ""))
    if not parent_id or not forum_id:
        return True
    return parent_id == forum_id


def _is_owned_match_thread(thread, forum, *, bot_id: str, expected_name: str) -> bool:
    if _text(getattr(thread, "name", "")) != expected_name:
        return False
    if not _same_forum(thread, forum):
        return False
    owner_id = _text(getattr(thread, "owner_id", ""))
    # Never delete/adopt a thread unless Discord identifies the bot as its owner.
    return bool(bot_id and owner_id and owner_id == bot_id)


async def _candidate_threads(manager, forum, match: dict[str, object]) -> list[object]:
    """Return active bot-created threads matching this exact tiebreak identity.

    Discord exposes active forum threads through both ``forum.threads`` and the
    guild's active thread collection. The persisted thread is also resolved
    directly so reconciliation can adopt it even if one cache view lags behind.
    """

    expected_name = _expected_name(match)
    bot_id = _text(getattr(getattr(manager.bot, "user", None), "id", ""))
    found: dict[int, object] = {}

    def add(thread) -> None:
        if not _is_owned_match_thread(
            thread, forum, bot_id=bot_id, expected_name=expected_name
        ):
            return
        ident = _thread_id(thread)
        if ident != 2**63 - 1:
            found[ident] = thread

    for thread in list(getattr(forum, "threads", ()) or ()):
        add(thread)

    guild = getattr(forum, "guild", None)
    for thread in list(getattr(guild, "threads", ()) or ()):
        add(thread)

    persisted_id = _text(match.get("thread_id"))
    if persisted_id:
        try:
            persisted = await runtime._resolve_thread(manager.bot, persisted_id)
        except discord.NotFound:
            persisted = None
        except Exception as exc:
            # A persisted ID already exists. On permission/transient/cache errors,
            # fail closed rather than creating another thread and making the live
            # duplication problem worse. Only a confirmed Discord 404 permits a
            # replacement to be created.
            log.exception(
                "Live Arena persisted tiebreak thread resolution failed • match=%s • thread=%s • error=%s: %s",
                _text(match.get("match_id")),
                persisted_id,
                type(exc).__name__,
                exc,
            )
            raise
        if persisted is not None:
            add(persisted)

    return list(found.values())


async def _cleanup_duplicates(match: dict[str, object], canonical, candidates) -> int:
    removed = 0
    canonical_id = _thread_id(canonical)
    for duplicate in candidates:
        duplicate_id = _thread_id(duplicate)
        if duplicate_id == canonical_id:
            continue
        try:
            await duplicate.delete(
                reason="Live Arena duplicate qualification tiebreak cleanup"
            )
            removed += 1
            log.warning(
                "Live Arena duplicate qualification tiebreak removed • match=%s • kept=%s • removed=%s",
                _text(match.get("match_id")),
                canonical_id,
                duplicate_id,
            )
        except discord.NotFound:
            pass
        except Exception as exc:
            log.exception(
                "Live Arena duplicate qualification tiebreak cleanup failed • match=%s • kept=%s • duplicate=%s • error=%s: %s",
                _text(match.get("match_id")),
                canonical_id,
                duplicate_id,
                type(exc).__name__,
                exc,
            )
    return removed


async def _create_thread(manager, forum, match: dict[str, object], templates):
    embed = templates["qualification_tiebreak_thread"].embed(
        player_a_mention=f"<@{_text(match['player_a_discord_user_id'])}>",
        player_b_mention=f"<@{_text(match['player_b_discord_user_id'])}>",
    )
    created = await forum.create_thread(
        name=_expected_name(match),
        content=(
            f"<@{_text(match['player_a_discord_user_id'])}> "
            f"<@{_text(match['player_b_discord_user_id'])}>"
        ),
        embed=embed,
        allowed_mentions=discord.AllowedMentions(
            users=True, roles=False, everyone=False
        ),
    )
    thread = getattr(created, "thread", None)
    if thread is None and isinstance(created, tuple):
        thread = created[0]
    return thread if thread is not None else created


async def _sync_one(manager, service, state: control.ControlState, match, forum, templates):
    identity = _identity(
        service.sheet_id, state.tournament_id, _text(match.get("match_id"))
    )
    lock = _locks.setdefault(identity, asyncio.Lock())

    async with lock:
        candidates = await _candidate_threads(manager, forum, match)
        created = None

        if not candidates:
            created = await _create_thread(manager, forum, match, templates)
            # Include the object returned by create_thread immediately; then scan
            # again so an overlapping deployment process can be adopted too.
            candidates = [created]
            discovered = await _candidate_threads(manager, forum, match)
            by_id = {_thread_id(item): item for item in candidates + discovered}
            candidates = [
                item for ident, item in by_id.items() if ident != 2**63 - 1
            ]

        canonical = min(candidates, key=_thread_id)
        canonical_id = str(_thread_id(canonical))
        previous_id = _text(match.get("thread_id"))

        try:
            if previous_id != canonical_id:
                await runtime._persist_thread_id_without_reread(
                    service, state, match, canonical_id
                )
        except Exception:
            # Only remove a brand-new thread if it is still the untracked canonical
            # object. Existing/adopted threads may already be valid resources.
            if created is canonical:
                try:
                    await created.delete(
                        reason="Live Arena tiebreak canonical ID persistence failed"
                    )
                except Exception:
                    log.exception(
                        "Live Arena untracked tiebreak cleanup failed • match=%s • thread=%s",
                        _text(match.get("match_id")),
                        canonical_id,
                    )
            raise

        removed = await _cleanup_duplicates(match, canonical, candidates)
        await runtime._ensure_result_controls(manager, canonical_id)
        if removed:
            log.info(
                "Live Arena qualification tiebreak converged • match=%s • canonical=%s • duplicates_removed=%s",
                _text(match.get("match_id")),
                canonical_id,
                removed,
            )


async def _publish_tiebreak_threads(
    manager,
    service: knockout.KnockoutService,
    state: control.ControlState,
    templates,
) -> None:
    if not state.tiebreak_required or state.unsupported_tie:
        return

    from modules.community.live_arena import qualification_panel

    config = service.repository.config
    forum = await qualification_panel._resolve_channel(
        manager.bot, int(config["MATCH_FORUM_CHANNEL_ID"])
    )
    for match in state.tiebreak_matches:
        await _sync_one(manager, service, state, match, forum, templates)


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    # #1138's ensure-flow resolves this module global at call time, so replace the
    # publication boundary itself instead of stacking another manager.sync wrapper.
    runtime._publish_tiebreak_threads = _publish_tiebreak_threads
    control._publish_tiebreak_threads = _publish_tiebreak_threads
