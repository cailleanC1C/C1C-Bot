"""Channel-shape compatibility for the Victory Ledger workspace.

The configured Victory Ledger destination can itself be a Discord Thread. In that
shape it is still the correct messageable destination for the mobile index and the
current-round post, but child threads cannot be created from it. The archive,
results, and Hall of Fame therefore need to be sibling threads/posts under the
Victory Ledger thread's real parent channel or forum.
"""

from __future__ import annotations

import logging

import discord

from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.victory_ledger_thread_parent")
_installed = False


def _is_thread_destination(channel) -> bool:
    return isinstance(channel, discord.Thread)


def _is_forum_container(channel) -> bool:
    return isinstance(channel, discord.ForumChannel)


def _forum_result(result):
    """Return ``(thread, starter_message)`` across discord.py forum result shapes."""
    thread = getattr(result, "thread", None)
    message = getattr(result, "message", None)
    if thread is not None:
        return thread, message
    if isinstance(result, tuple) and result:
        thread = result[0]
        message = result[1] if len(result) > 1 else None
        return thread, message
    return result, None


async def _ensure_thread_from_ledger_parent(
    workspace,
    original,
    *,
    bot,
    repository,
    parent,
    key: str,
    template,
    resources,
):
    """Create workspace destinations beside a Victory Ledger thread, not inside it."""
    if not _is_thread_destination(parent):
        return await original(
            bot=bot,
            repository=repository,
            parent=parent,
            key=key,
            template=template,
            resources=resources,
        )

    container = getattr(parent, "parent", None)
    if container is None:
        raise RuntimeError(
            "Victory Ledger is a Discord thread but its parent channel is unavailable"
        )

    resource = resources.get(
        (workspace._GLOBAL_RESOURCE_ID, workspace._THREAD_RESOURCE_TYPE, key)
    )
    thread_id = _text(resource.get("thread_id")) if resource else ""
    thread = None
    intro = None

    if thread_id:
        try:
            thread = await workspace._resolve_channel(bot, thread_id)
        except discord.NotFound:
            thread = None

    if thread is None:
        for candidate in getattr(container, "threads", ()):
            if _text(getattr(candidate, "name", "")) == template.title:
                thread = candidate
                break

    async def create(duration: int):
        if _is_forum_container(container):
            created = await container.create_thread(
                name=template.title[:100],
                embed=template.embed(),
                auto_archive_duration=duration,
                reason="C1C Live Arena Victory Ledger workspace",
            )
            return _forum_result(created)
        created = await container.create_thread(
            name=template.title[:100],
            type=discord.ChannelType.public_thread,
            auto_archive_duration=duration,
            reason="C1C Live Arena Victory Ledger workspace",
        )
        return created, None

    if thread is None:
        try:
            thread, intro = await create(10080)
        except discord.HTTPException:
            thread, intro = await create(1440)
        log.info(
            "Live Arena Victory Ledger workspace destination created • key=%s • ledger_thread=%s • parent=%s • parent_type=%s • thread=%s",
            key,
            getattr(parent, "id", ""),
            getattr(container, "id", ""),
            type(container).__name__,
            getattr(thread, "id", ""),
        )
    elif getattr(thread, "archived", False):
        await thread.edit(archived=False, reason="Live Arena workspace refresh")

    if intro is None and resource and _text(resource.get("message_id")):
        intro = await workspace._fetch_message(thread, _text(resource.get("message_id")))
    if intro is None:
        intro = await thread.send(embed=template.embed())
    elif resource is not None:
        await intro.edit(embed=template.embed())

    changed = (
        resource is None
        or _text(resource.get("channel_id")) != str(container.id)
        or _text(resource.get("thread_id")) != str(thread.id)
        or _text(resource.get("message_id")) != str(intro.id)
        or _text(resource.get("state")) != "active"
    )
    if changed:
        values = {
            "tournament_id": workspace._GLOBAL_RESOURCE_ID,
            "resource_type": workspace._THREAD_RESOURCE_TYPE,
            "resource_key": key,
            "channel_id": str(container.id),
            "message_id": str(intro.id),
            "thread_id": str(thread.id),
            "created_at_utc": (
                _text(resource.get("created_at_utc")) if resource else workspace._now()
            ),
            "updated_at_utc": workspace._now(),
            "state": "active",
            "notes": f"Victory Ledger workspace thread: {key}",
        }
        await repository.upsert_discord_resource(**values)
        resources[workspace._identity(values)] = values

    return thread


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import victory_ledger_workspace as workspace

    original = workspace._ensure_thread

    async def ensure_thread(**kwargs):
        try:
            return await _ensure_thread_from_ledger_parent(
                workspace,
                original,
                **kwargs,
            )
        except Exception as exc:
            parent = kwargs.get("parent")
            container = getattr(parent, "parent", None) if parent is not None else None
            log.error(
                "Live Arena Victory Ledger workspace destination failed • key=%s • ledger_type=%s • ledger_id=%s • parent_type=%s • parent_id=%s • error=%s: %s",
                kwargs.get("key", ""),
                type(parent).__name__ if parent is not None else "None",
                getattr(parent, "id", "") if parent is not None else "",
                type(container).__name__ if container is not None else "None",
                getattr(container, "id", "") if container is not None else "",
                type(exc).__name__,
                exc,
                exc_info=True,
            )
            raise

    workspace._ensure_thread = ensure_thread
