"""Automated server map generator and scheduler wiring."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Sequence, Set, Tuple, TYPE_CHECKING

import discord

from shared.config import get_server_map_channel_id, get_server_map_refresh_days
from shared.logfmt import channel_label
from modules.common import feature_flags, runtime as runtime_helpers

from . import server_map_state

if TYPE_CHECKING:  # pragma: no cover - typing helpers only
    from modules.common.runtime import Runtime

__all__ = [
    "build_map_messages",
    "refresh_server_map",
    "schedule_server_map_job",
    "should_refresh",
]

log = logging.getLogger("c1c.server_map")

DEFAULT_MESSAGE_THRESHOLD = 1800
BULLET = "🔹"
MAP_HEADER = "# 🧭 Server Map"
MAP_INTRO_LINES = [
    MAP_HEADER,
    "Tired of wandering the digital wilderness?",
    "Here’s your trusty compass—every channel, category, and secret nook laid out in one sleek guide.",
    "Ready to explore? Let’s go light the path together. ✨",
    "",
    "Channels with a 🔒 icon do need special roles to unlock access.",
    "",
]


@dataclass(slots=True)
class ServerMapResult:
    status: str
    message_count: int = 0
    total_chars: int = 0
    reason: str | None = None
    last_run: str | None = None


@dataclass(slots=True)
class ServerMapStats:
    categories: int
    channels: int
    uncategorized: int


def _channel_sort_key(channel: object) -> Tuple[int, int]:
    position = getattr(channel, "position", 0)
    identifier = getattr(channel, "id", 0)
    try:
        identifier = int(identifier)
    except (TypeError, ValueError):
        identifier = 0
    try:
        pos_value = int(position)
    except (TypeError, ValueError):
        pos_value = 0
    return pos_value, identifier


def _category_label(category: object) -> str:
    name = str(getattr(category, "name", "")).strip()
    if name:
        return name
    identifier = getattr(category, "id", None)
    return f"Category {identifier}" if identifier is not None else "Category"


def _channel_name(channel: object) -> str:
    name = str(getattr(channel, "name", "")).strip()
    if name:
        return name
    identifier = getattr(channel, "id", None)
    return f"channel-{identifier}" if identifier is not None else "channel"


def _normalize_id(value: object) -> int | None:
    try:
        identifier = int(value)
    except (TypeError, ValueError):
        return None
    return identifier if identifier >= 0 else None


def _channel_mention(channel: object) -> str:
    identifier = _normalize_id(getattr(channel, "id", None))
    if identifier is not None:
        return f"<#{identifier}>"
    return _channel_name(channel)


def _channel_line(channel: object) -> str:
    return f"{BULLET} {_channel_mention(channel)}"


def _split_blocks(blocks: Sequence[str], threshold: int) -> List[str]:
    messages: List[str] = []
    current = ""
    for block in blocks:
        block_text = block.strip()
        if not block_text:
            continue
        if not current:
            candidate = block_text
        else:
            candidate = f"{current}\n\n{block_text}"
        if len(candidate) > threshold and current:
            messages.append(current)
            current = block_text
        else:
            current = candidate
    if current:
        messages.append(current)
    return messages


def _snowflake_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _config_value_text(value: object | None) -> str | None:
    text = str(value or "").strip()
    return text or None


def _parse_id_blacklist(raw: str | None) -> Set[str]:
    ids: Set[str] = set()
    if not raw:
        return ids
    for chunk in str(raw).split(","):
        entry = chunk.strip()
        if entry:
            ids.add(entry)
    return ids


def _normalize_blacklist_values(values: Iterable[object] | None) -> Set[str]:
    normalized: Set[str] = set()
    if not values:
        return normalized
    for value in values:
        text = _snowflake_text(value)
        if text:
            normalized.add(text)
    return normalized


def _split_channels_by_category(
    guild: object,
) -> tuple[List[object], Dict[object, List[object]]]:
    channels = getattr(guild, "channels", []) or []
    orphan: List[object] = []
    grouped: Dict[object, List[object]] = {}
    for channel in channels:
        channel_type = getattr(channel, "type", None)
        if channel_type == discord.ChannelType.category or isinstance(
            channel, discord.CategoryChannel
        ):
            continue
        category_id = getattr(channel, "category_id", None)
        if category_id is None:
            orphan.append(channel)
        else:
            bucket = grouped.setdefault(category_id, [])
            bucket.append(channel)
    orphan.sort(key=_channel_sort_key)
    for bucket in grouped.values():
        bucket.sort(key=_channel_sort_key)
    return orphan, grouped


def build_map_messages(
    guild: object,
    *,
    threshold: int = DEFAULT_MESSAGE_THRESHOLD,
    category_blacklist: Iterable[object] | None = None,
    channel_blacklist: Iterable[object] | None = None,
) -> tuple[List[str], ServerMapStats]:
    """Return formatted server map message bodies for ``guild``."""

    category_blacklist = _normalize_blacklist_values(category_blacklist)
    channel_blacklist = _normalize_blacklist_values(channel_blacklist)
    categories = sorted(getattr(guild, "categories", []) or [], key=_channel_sort_key)
    blocks: List[str] = []
    rendered_category_count = 0
    rendered_channel_count = 0
    rendered_uncategorized_count = 0

    orphan_channels, channels_by_category = _split_channels_by_category(guild)
    intro_lines = list(MAP_INTRO_LINES)
    for channel in orphan_channels:
        channel_id = _snowflake_text(getattr(channel, "id", None))
        if channel_id and channel_id in channel_blacklist:
            continue
        intro_lines.append(_channel_line(channel))
        rendered_channel_count += 1
        rendered_uncategorized_count += 1
    blocks.append("\n".join(intro_lines).rstrip())

    for category in categories:
        category_id_text = _snowflake_text(getattr(category, "id", None))
        if category_id_text and category_id_text in category_blacklist:
            continue
        channels = channels_by_category.get(getattr(category, "id", None), [])
        rendered_channels: list[str] = []
        for channel in channels:
            channel_id = _snowflake_text(getattr(channel, "id", None))
            if channel_id and channel_id in channel_blacklist:
                continue
            rendered_channels.append(_channel_line(channel))
        if not rendered_channels:
            continue
        header = _category_label(category)
        lines = [f"## {header}", ""]
        lines.extend(rendered_channels)
        rendered_category_count += 1
        rendered_channel_count += len(rendered_channels)
        blocks.append("\n".join(lines).rstrip())

    stats = ServerMapStats(
        categories=rendered_category_count,
        channels=rendered_channel_count,
        uncategorized=rendered_uncategorized_count,
    )
    return _split_blocks(blocks, threshold), stats


def _extract_message_slots(state: Mapping[str, str]) -> list[tuple[int, int]]:
    slots: list[tuple[int, int]] = []
    for key, value in state.items():
        if not key.startswith("SERVER_MAP_MESSAGE_ID_"):
            continue
        try:
            slot = int(key.rsplit("_", 1)[-1])
            message_id = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        slots.append((slot, message_id))
    slots.sort(key=lambda item: item[0])
    return slots


def _parse_timestamp(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    text = value.strip()
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


def should_refresh(
    last_run: dt.datetime | None,
    refresh_days: int,
    *,
    now: dt.datetime | None = None,
) -> bool:
    if last_run is None:
        return True
    now = now or dt.datetime.now(dt.timezone.utc)
    return (now - last_run) >= dt.timedelta(days=max(1, refresh_days))


def _fallback_messages(guild: discord.Guild | None) -> List[str]:
    name = getattr(guild, "name", "Server")
    return [f"{name}\n{BULLET} No visible categories."]


def _format_skip(reason: str, last_run: str | None = None) -> str:
    suffix = f" • last_run={last_run}" if last_run else ""
    return f"📘 Server map — skipped • reason={reason}{suffix}"


def _format_error(reason: str) -> str:
    return f"❌ Server map — error • reason={reason}"


def _now_iso(now: dt.datetime | None = None) -> str:
    timestamp = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    return timestamp.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean_state_entries(
    updated_messages: Sequence[discord.Message],
    stored_slots: Sequence[tuple[int, int]],
    timestamp: str,
) -> Dict[str, str]:
    entries: Dict[str, str] = {}
    for index, message in enumerate(updated_messages, start=1):
        entries[f"SERVER_MAP_MESSAGE_ID_{index}"] = str(message.id)
    max_slot = max((slot for slot, _ in stored_slots), default=0)
    for slot in range(len(updated_messages) + 1, max_slot + 1):
        entries[f"SERVER_MAP_MESSAGE_ID_{slot}"] = ""
    entries["SERVER_MAP_LAST_RUN_AT"] = timestamp
    return entries


def _humanized_channel_list(guild: discord.Guild, ids: Sequence[int]) -> str:
    labels = [channel_label(guild, cid) for cid in ids if cid]
    return ", ".join(labels) if labels else "none"


async def refresh_server_map(
    bot: discord.Client,
    *,
    force: bool = False,
    actor: str = "scheduler",
    requested_channel: str | None = "config",
) -> ServerMapResult:
    await bot.wait_until_ready()

    if not feature_flags.is_enabled("SERVER_MAP"):
        message = _format_skip("feature_disabled")
        await runtime_helpers.send_log_message(message)
        return ServerMapResult(status="disabled", reason="feature_disabled")

    channel_id = get_server_map_channel_id()
    channel, channel_error = await runtime_helpers.resolve_configured_text_channel(
        bot,
        channel_id=channel_id,
        logger=log,
        context="server map",
        invalid_reason="invalid_channel_type",
    )
    if channel_error:
        message = _format_error(channel_error)
        await runtime_helpers.send_log_message(message)
        return ServerMapResult(status="error", reason=channel_error)

    guild = channel.guild
    guild_name = getattr(guild, "name", "unknown guild")
    cmd_label = "cron" if actor == "scheduler" else "servermap"
    requested_label = requested_channel or "config"
    target_label = channel_label(guild, channel_id)


    try:
        state = await server_map_state.fetch_state()
    except Exception:
        log.exception("failed to read Config worksheet for server map state")
        message = _format_error("config_fetch_failed")
        await runtime_helpers.send_log_message(message)
        return ServerMapResult(status="error", reason="config_fetch_failed")

    last_run_raw = state.get("SERVER_MAP_LAST_RUN_AT")
    last_run = _parse_timestamp(last_run_raw)
    refresh_days = get_server_map_refresh_days()
    now = dt.datetime.now(dt.timezone.utc)

    if not force and not should_refresh(last_run, refresh_days, now=now):
        log.debug("server map skipped: interval not elapsed", extra={"last_run": last_run_raw, "actor": actor})
        return ServerMapResult(
            status="skipped",
            reason="interval_not_elapsed",
            last_run=last_run_raw,
        )

    raw_category_blacklist = _config_value_text(
        os.getenv("SERVER_MAP_CATEGORY_BLACKLIST", "")
    )
    raw_channel_blacklist = _config_value_text(
        os.getenv("SERVER_MAP_CHANNEL_BLACKLIST", "")
    )

    category_blacklist = _parse_id_blacklist(raw_category_blacklist)
    channel_blacklist = _parse_id_blacklist(raw_channel_blacklist)

    category_blacklist_label = _humanized_channel_list(guild, category_blacklist)
    channel_blacklist_label = _humanized_channel_list(guild, channel_blacklist)

    cat_raw_display = (raw_category_blacklist or "").replace("\"", "'")
    chan_raw_display = (raw_channel_blacklist or "").replace("\"", "'")
    config_debug = (
        "📘 Server map — config • "
        f"guild={guild_name} "
        f"• cat_blacklist={category_blacklist_label} "
        f"• chan_blacklist={channel_blacklist_label} "
        f"• cat_blacklist_raw={cat_raw_display!r} "
        f"• chan_blacklist_raw={chan_raw_display!r} "
        f"• cat_ids={len(category_blacklist)} "
        f"• chan_ids={len(channel_blacklist)}"
    )
    log.info(
        "Server map — config • guild=%s • cat_blacklist_raw=%r • chan_blacklist_raw=%r "
        "• cat_ids=%d • chan_ids=%d",
        guild_name,
        raw_category_blacklist,
        raw_channel_blacklist,
        len(category_blacklist),
        len(channel_blacklist),
    )
    log.debug(config_debug)

    bodies, stats = build_map_messages(
        guild,
        threshold=DEFAULT_MESSAGE_THRESHOLD,
        category_blacklist=category_blacklist,
        channel_blacklist=channel_blacklist,
    )
    if not bodies:
        bodies = _fallback_messages(guild)
        stats = ServerMapStats(categories=0, channels=0, uncategorized=0)

    stored_slots = _extract_message_slots(state)
    fetched_messages: List[discord.Message] = []
    for _, message_id in stored_slots:
        try:
            message = await channel.fetch_message(message_id)
        except discord.NotFound:
            continue
        except discord.HTTPException:
            log.debug("failed to fetch stored server map message", exc_info=True)
            continue
        fetched_messages.append(message)

    updated_messages: List[discord.Message] = []
    for index, body in enumerate(bodies):
        existing = fetched_messages[index] if index < len(fetched_messages) else None
        if existing is None:
            try:
                new_message = await channel.send(body)
            except discord.HTTPException:
                log.exception("failed to send server map block", extra={"channel": channel_id})
                message = _format_error("message_send_failed")
                await runtime_helpers.send_log_message(message)
                return ServerMapResult(status="error", reason="message_send_failed")
            updated_messages.append(new_message)
            continue
        try:
            if (existing.content or "").strip() != body.strip():
                await existing.edit(content=body)
        except discord.HTTPException:
            log.exception("failed to edit server map message", extra={"message_id": existing.id})
            message = _format_error("message_edit_failed")
            await runtime_helpers.send_log_message(message)
            return ServerMapResult(status="error", reason="message_edit_failed")
        updated_messages.append(existing)

    cleaned = 0
    for extra in fetched_messages[len(updated_messages):]:
        try:
            await extra.delete()
            cleaned += 1
        except discord.HTTPException:
            log.debug("failed to delete old server map message", exc_info=True)

    if updated_messages:
        primary = updated_messages[0]
        try:
            if not primary.pinned:
                await primary.pin()
        except discord.HTTPException:
            log.debug("failed to pin primary server map message", exc_info=True)

    total_chars = sum(len(body) for body in bodies)
    now_iso = _now_iso(now)
    entries = _clean_state_entries(updated_messages, stored_slots, now_iso)
    try:
        await server_map_state.update_state(entries)
    except Exception:
        log.exception("failed to persist server map state")
        message = _format_error("state_update_failed")
        await runtime_helpers.send_log_message(message)
        return ServerMapResult(status="error", reason="state_update_failed")

    await runtime_helpers.send_log_message(
        "✅ Server map updated — "
        f"cmd={cmd_label} • guild={guild_name} • target={target_label} "
        f"• categories={stats.categories} • channels={stats.channels} • uncategorized={stats.uncategorized} "
        f"• messages={len(updated_messages)}"
        + (f" • cleaned={cleaned}" if cleaned else "")
    )

    return ServerMapResult(
        status="ok", message_count=len(updated_messages), total_chars=total_chars
    )


def schedule_server_map_job(runtime: "Runtime") -> None:
    job = runtime.scheduler.every(hours=24, jitter="small", tag="server_map", name="server_map_refresh")

    async def runner() -> None:
        try:
            await refresh_server_map(runtime.bot, actor="scheduler")
        except asyncio.CancelledError:  # pragma: no cover - scheduler lifecycle
            raise
        except Exception:  # pragma: no cover - defensive scheduler guard
            log.exception("scheduled server map refresh failed")

    job.do(runner)
