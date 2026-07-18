"""Automated Guides & Help forum tag index."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Sequence, Set, TYPE_CHECKING

import discord

from modules.common import feature_flags, runtime as runtime_helpers
from modules.ops import server_map_state
from shared.logfmt import channel_label
from shared.sheets import recruitment

if TYPE_CHECKING:  # pragma: no cover
    from modules.common.runtime import Runtime

log = logging.getLogger("c1c.housekeeping.guides_help_index")

FEATURE_KEY = "GUIDES_HELP_INDEX_ENABLED"
DEFAULT_MESSAGE_THRESHOLD = 1800
BULLET = "🔹"
HEADER = "# 🛠️ Guides & Help Index"

CONFIG_SOURCE_CATEGORY_ID = "GUIDES_HELP_INDEX_SOURCE_CATEGORY_ID"
CONFIG_TARGET_CHANNEL_ID = "GUIDES_HELP_INDEX_TARGET_CHANNEL_ID"
CONFIG_REFRESH_DAYS = "GUIDES_HELP_INDEX_REFRESH_DAYS"
CONFIG_FORUM_BLACKLIST = "GUIDES_HELP_INDEX_FORUM_CHANNEL_BLACKLIST"
CONFIG_TAG_BLACKLIST = "GUIDES_HELP_INDEX_TAG_BLACKLIST"
CONFIG_POST_BLACKLIST = "GUIDES_HELP_INDEX_POST_BLACKLIST"
STATE_MESSAGE_PREFIX = "GUIDES_HELP_INDEX_MESSAGE_ID_"
STATE_LAST_RUN_AT = "GUIDES_HELP_INDEX_LAST_RUN_AT"


@dataclass(slots=True)
class GuidesHelpIndexResult:
    status: str
    message_count: int = 0
    indexed_posts: int = 0
    tag_groups: int = 0
    reason: str | None = None
    last_run: str | None = None
    stale_deleted: int = 0


@dataclass(frozen=True, slots=True)
class IndexedThread:
    thread: object
    forum: object


def _text(value: object | None) -> str | None:
    cleaned = str(value or "").strip()
    return cleaned or None


def _parse_int(value: object | None) -> int | None:
    text = _text(value)
    if not text:
        return None
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _parse_csv(raw: object | None) -> Set[str]:
    values: Set[str] = set()
    if raw is None:
        return values
    for chunk in str(raw).split(","):
        item = chunk.strip()
        if item:
            values.add(item)
    return values


def _parse_timestamp(value: str | None) -> dt.datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _now_iso(now: dt.datetime | None = None) -> str:
    timestamp = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    return timestamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def should_refresh(
    last_run: dt.datetime | None, refresh_days: int, *, now: dt.datetime | None = None
) -> bool:
    if last_run is None:
        return True
    return ((now or dt.datetime.now(dt.timezone.utc)) - last_run) >= dt.timedelta(
        days=max(1, refresh_days)
    )


def _sort_key(channel: object) -> tuple[int, int]:
    return (
        _parse_int(getattr(channel, "position", 0)) or 0,
        _parse_int(getattr(channel, "id", 0)) or 0,
    )


def discover_forum_channels(
    category: object, *, blacklist: Iterable[str] | None = None
) -> list[object]:
    blacklisted = {str(item).strip() for item in (blacklist or []) if str(item).strip()}
    forums: list[object] = []
    for channel in getattr(category, "channels", []) or []:
        channel_id = str(getattr(channel, "id", "")).strip()
        if channel_id and channel_id in blacklisted:
            continue
        channel_type = getattr(channel, "type", None)
        if (
            isinstance(channel, discord.ForumChannel)
            or channel_type == discord.ChannelType.forum
            or hasattr(channel, "available_tags")
        ):
            forums.append(channel)
    return sorted(forums, key=_sort_key)


def _tag_group_sort_key(
    item: tuple[str, tuple[int | None, list[IndexedThread]]],
) -> tuple[int, int, str]:
    tag_name, (available_tag_index, _) = item
    if available_tag_index is None:
        return (1, 0, tag_name.casefold())
    return (0, available_tag_index, tag_name.casefold())


def _thread_sort_key(item: IndexedThread) -> str:
    return (_text(getattr(item.thread, "name", "")) or "").casefold()


def _thread_mention(thread: object) -> str:
    mention = _text(getattr(thread, "mention", None))
    if mention:
        return mention
    jump_url = _text(getattr(thread, "jump_url", None))
    name = _text(getattr(thread, "name", None)) or f"thread-{getattr(thread, 'id', '')}"
    return f"[{name}]({jump_url})" if jump_url else name


async def _collect_forum_threads(forum: object) -> tuple[list[object], bool]:
    threads: dict[int, object] = {}
    for thread in getattr(forum, "threads", []) or []:
        tid = _parse_int(getattr(thread, "id", None))
        if tid is not None:
            threads[tid] = thread
    active = getattr(forum, "active_threads", None)
    if callable(active):
        for thread in await active():
            tid = _parse_int(getattr(thread, "id", None))
            if tid is not None:
                threads[tid] = thread
    try:
        async for thread in forum.archived_threads(limit=None):
            tid = _parse_int(getattr(thread, "id", None))
            if tid is not None:
                threads[tid] = thread
    except AttributeError:
        pass
    return list(threads.values()), True


def build_index_messages(
    forums: Sequence[object],
    threads_by_forum: Mapping[int, Sequence[object]],
    *,
    tag_blacklist: Iterable[str] | None = None,
    post_blacklist: Iterable[str] | None = None,
    threshold: int = DEFAULT_MESSAGE_THRESHOLD,
) -> tuple[list[str], int, int]:
    tag_black = {
        str(item).strip() for item in (tag_blacklist or []) if str(item).strip()
    }
    post_black = {
        str(item).strip() for item in (post_blacklist or []) if str(item).strip()
    }
    grouped: Dict[str, tuple[int | None, list[IndexedThread]]] = {}
    indexed_ids: set[str] = set()
    tag_order: Dict[str, int] = {}
    for forum in forums:
        for index, tag in enumerate(getattr(forum, "available_tags", []) or []):
            name = str(getattr(tag, "name", "")).strip()
            if name:
                tag_order.setdefault(name, index)
        forum_id = _parse_int(getattr(forum, "id", None))
        for thread in threads_by_forum.get(forum_id or 0, []):
            thread_id = str(getattr(thread, "id", "")).strip()
            if thread_id and thread_id in post_black:
                continue
            tags = list(getattr(thread, "applied_tags", []) or [])
            if not tags:
                continue
            rendered_this_thread = False
            for tag in tags:
                tag_id = str(getattr(tag, "id", "")).strip()
                tag_name = str(getattr(tag, "name", "")).strip()
                if not tag_name or tag_id in tag_black or tag_name in tag_black:
                    continue
                grouped.setdefault(tag_name, (tag_order.get(tag_name), []))[1].append(
                    IndexedThread(thread=thread, forum=forum)
                )
                rendered_this_thread = True
            if rendered_this_thread and thread_id:
                indexed_ids.add(thread_id)

    blocks = [HEADER]
    for tag_name, (_, items) in sorted(grouped.items(), key=_tag_group_sort_key):
        lines = [f"## {tag_name}", ""]
        seen: set[str] = set()
        for item in sorted(items, key=_thread_sort_key):
            tid = str(getattr(item.thread, "id", "")).strip()
            if tid and tid in seen:
                continue
            seen.add(tid)
            lines.append(f"{BULLET} {_thread_mention(item.thread)}")
        if len(lines) > 2:
            blocks.append("\n".join(lines).rstrip())

    messages: list[str] = []
    current = ""
    for block in blocks:
        candidate = block if not current else f"{current}\n\n{block}"
        if current and len(candidate) > threshold:
            messages.append(current)
            current = block
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages, len(indexed_ids), len(grouped)


async def _config(key: str, default: str | None = None) -> str | None:
    return await recruitment.get_config_value_async(key, default)


def _resolve_category_from_guild(
    guild: object, category_id: int | None
) -> tuple[object | None, str | None]:
    if category_id is None:
        return None, "missing_source_category"

    categories = list(getattr(guild, "categories", []) or [])
    for category in categories:
        if _parse_int(getattr(category, "id", None)) == category_id:
            return category, None

    resolved = None
    get_channel = getattr(guild, "get_channel", None)
    if callable(get_channel):
        resolved = get_channel(category_id)

    if resolved is not None:
        log.warning(
            "guides help index source category resolved outside guild.categories: "
            "source_id=%s resolved_class=%s resolved_name=%s resolved_type=%s",
            category_id,
            resolved.__class__.__name__,
            getattr(resolved, "name", None),
            getattr(resolved, "type", None),
        )
        return None, "invalid_category_type"

    return None, "missing_source_category"


def _extract_message_slots(state: Mapping[str, str]) -> list[tuple[int, int]]:
    slots = []
    for key, value in state.items():
        if key.startswith(STATE_MESSAGE_PREFIX):
            slot = _parse_int(key.rsplit("_", 1)[-1])
            mid = _parse_int(value)
            if slot and mid:
                slots.append((slot, mid))
    return sorted(slots)


async def refresh_guides_help_index(
    bot: discord.Client, *, force: bool = False, actor: str = "scheduler"
) -> GuidesHelpIndexResult:
    await bot.wait_until_ready()
    if not feature_flags.is_enabled(FEATURE_KEY):
        log.info("guides help index skipped: disabled")
        await runtime_helpers.send_log_message(
            "📘 Guides help index — skipped • reason=feature_disabled"
        )
        return GuidesHelpIndexResult(status="disabled", reason="feature_disabled")

    source_id = _parse_int(await _config(CONFIG_SOURCE_CATEGORY_ID))
    target_id = _parse_int(await _config(CONFIG_TARGET_CHANNEL_ID))
    if source_id is None:
        return GuidesHelpIndexResult(status="error", reason="missing_source_category")
    if target_id is None:
        return GuidesHelpIndexResult(status="error", reason="missing_target_channel")

    target, target_error = await runtime_helpers.resolve_configured_text_channel(
        bot,
        channel_id=target_id,
        logger=log,
        context="guides help index",
        invalid_reason="invalid_target_channel_type",
    )
    if target_error:
        reason = (
            "missing_target_channel"
            if target_error in {"missing_channel", "not_found"}
            else target_error
        )
        await runtime_helpers.send_log_message(
            f"❌ Guides help index — error • reason={reason}"
        )
        return GuidesHelpIndexResult(status="error", reason=reason)

    category, category_error = _resolve_category_from_guild(
        getattr(target, "guild", None), source_id
    )
    if category_error:
        await runtime_helpers.send_log_message(
            f"❌ Guides help index — error • reason={category_error}"
        )
        return GuidesHelpIndexResult(status="error", reason=category_error)

    try:
        state = await server_map_state.fetch_state()
    except Exception:
        log.exception("failed to read Config worksheet for guides help index state")
        return GuidesHelpIndexResult(status="error", reason="config_fetch_failed")
    refresh_days = _parse_int(await _config(CONFIG_REFRESH_DAYS, "1")) or 1
    last_run_raw = state.get(STATE_LAST_RUN_AT)
    now = dt.datetime.now(dt.timezone.utc)
    if not force and not should_refresh(
        _parse_timestamp(last_run_raw), refresh_days, now=now
    ):
        log.info("guides help index skipped: interval not elapsed")
        return GuidesHelpIndexResult(
            status="skipped", reason="interval_not_elapsed", last_run=last_run_raw
        )

    forum_black = _parse_csv(await _config(CONFIG_FORUM_BLACKLIST, ""))
    tag_black = _parse_csv(await _config(CONFIG_TAG_BLACKLIST, ""))
    post_black = _parse_csv(await _config(CONFIG_POST_BLACKLIST, ""))
    forums = discover_forum_channels(category, blacklist=forum_black)
    if not forums:
        return GuidesHelpIndexResult(
            status="error", reason="no_readable_forum_channels"
        )

    threads_by_forum: dict[int, list[object]] = {}
    readable = 0
    for forum in forums:
        fid = _parse_int(getattr(forum, "id", None))
        if fid is None:
            continue
        try:
            threads, _ = await _collect_forum_threads(forum)
        except (discord.Forbidden, discord.HTTPException) as exc:
            log.warning(
                "guides help index forum fetch failed: forum=%s error=%s",
                channel_label(getattr(forum, "guild", None), fid),
                exc,
            )
            continue
        readable += 1
        threads_by_forum[fid] = threads
    if readable == 0:
        return GuidesHelpIndexResult(
            status="error", reason="discord_fetch_permission_issue"
        )

    bodies, indexed_posts, tag_groups = build_index_messages(
        forums, threads_by_forum, tag_blacklist=tag_black, post_blacklist=post_black
    )
    stored_slots = _extract_message_slots(state)
    fetched = []
    for _, message_id in stored_slots:
        try:
            fetched.append(await target.fetch_message(message_id))
        except (discord.NotFound, discord.HTTPException):
            continue
    updated = []
    for index, body in enumerate(bodies):
        existing = fetched[index] if index < len(fetched) else None
        if existing is None:
            try:
                updated.append(await target.send(body))
            except discord.HTTPException:
                log.exception(
                    "failed to send guides help index block",
                    extra={"channel": target_id},
                )
                await runtime_helpers.send_log_message(
                    "❌ Guides help index — error • reason=message_send_failed"
                )
                return GuidesHelpIndexResult(
                    status="error", reason="message_send_failed"
                )
        else:
            try:
                if (getattr(existing, "content", "") or "").strip() != body.strip():
                    await existing.edit(content=body)
            except discord.HTTPException:
                log.exception(
                    "failed to edit guides help index message",
                    extra={"message_id": getattr(existing, "id", None)},
                )
                await runtime_helpers.send_log_message(
                    "❌ Guides help index — error • reason=message_edit_failed"
                )
                return GuidesHelpIndexResult(
                    status="error", reason="message_edit_failed"
                )
            updated.append(existing)
    stale_deleted = 0
    for extra in fetched[len(updated) :]:
        try:
            await extra.delete()
            stale_deleted += 1
        except discord.HTTPException:
            log.debug("failed to delete stale guides help index message", exc_info=True)
    if updated:
        try:
            if not getattr(updated[0], "pinned", False):
                await updated[0].pin()
        except Exception:
            log.debug("failed to pin guides help index primary message", exc_info=True)
    entries = {
        f"{STATE_MESSAGE_PREFIX}{idx}": str(msg.id)
        for idx, msg in enumerate(updated, start=1)
    }
    for slot in range(
        len(updated) + 1, max((slot for slot, _ in stored_slots), default=0) + 1
    ):
        entries[f"{STATE_MESSAGE_PREFIX}{slot}"] = ""
    entries[STATE_LAST_RUN_AT] = _now_iso(now)
    try:
        await server_map_state.update_state(entries)
    except Exception:
        log.exception("failed to persist guides help index state")
        await runtime_helpers.send_log_message(
            "❌ Guides help index — error • reason=state_update_failed"
        )
        return GuidesHelpIndexResult(status="error", reason="state_update_failed")
    await runtime_helpers.send_log_message(
        "✅ Guides help index updated — "
        f"cmd={'cron' if actor == 'scheduler' else 'guideshelpindex'} • forums={readable} • tags={tag_groups} "
        f"• posts={indexed_posts} • messages={len(updated)} • cleaned={stale_deleted}"
    )
    return GuidesHelpIndexResult(
        status="ok",
        message_count=len(updated),
        indexed_posts=indexed_posts,
        tag_groups=tag_groups,
        stale_deleted=stale_deleted,
    )


def schedule_guides_help_index_job(runtime: "Runtime") -> None:
    job = runtime.scheduler.every(
        hours=24,
        jitter="small",
        tag="guides_help_index",
        name="guides_help_index_refresh",
    )

    async def runner() -> None:
        try:
            await refresh_guides_help_index(runtime.bot, actor="scheduler")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduled guides help index refresh failed")

    job.do(runner)
