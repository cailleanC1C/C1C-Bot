"""Cluster role map builder backed by the WhoWeAre worksheet."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Dict, Iterable, List, Mapping, Sequence

import discord

from shared.config import get_recruitment_sheet_id
from shared.sheets import recruitment
from shared.sheets.async_core import afetch_records

log = logging.getLogger("c1c.cluster_role_map")

CATEGORY_EMOJIS: Dict[str, str] = {
    "clusterleadership": "🔥",
    "clustersupport": "🛡️",
    "recruitment": "🌱",
    "communitysupport": "📘",
    "specialsupporters": "💎",
}

DEFAULT_DESCRIPTION = "no description set"
MARKER_LINE = ":white_small_square::white_small_square::white_small_square:"
INVISIBLE_MARKER = "\u2063\u200b\u2060\u2063\u200b\u2060\u2063\u200b\u2060\u2063\u200b\u2060"
ROLE_MAP_MARKER = INVISIBLE_MARKER
INDEX_HEADER_LINES = [
    "# WHO WE ARE — C1C Role Map",
    "Roles first. Humans optional. Snark mandatory.",
]
INDEX_BUILDING_NOTICE = "_(Building links…)_"
INDEX_EMPTY_NOTICE = "_(No categories are currently available — check the WhoWeAre sheet.)_"


@dataclass(slots=True)
class RoleMapRow:
    """Structured view of a WhoWeAre worksheet row."""

    category: str
    role_id: int
    sheet_role_name: str
    role_description: str
    role_usage: str


@dataclass(slots=True)
class RoleMapRender:
    """Rendered output plus summary counts for logging."""

    categories: List["RoleMapCategoryRender"]
    category_count: int
    role_count: int
    unassigned_roles: int


@dataclass(slots=True)
class RoleEntryRender:
    """Display payload for a single Discord role entry."""

    role_id: int
    display_name: str
    description: str
    members: List[str]
    usage: str


@dataclass(slots=True)
class RoleMapCategoryRender:
    """Heading, emoji, and role rows for a category."""

    name: str
    emoji: str
    roles: List[RoleEntryRender]


@dataclass(slots=True)
class IndexLink:
    """Jump-link metadata for the index message."""

    name: str
    emoji: str
    url: str


class RoleMapLoadError(RuntimeError):
    """Raised when the WhoWeAre worksheet cannot be read."""


def _normalize_text(value: object) -> str:
    return str(value or "").strip()


def _normalize_usage(value: object) -> str:
    return " ".join(str(value or "").split())


def _cell(row: Mapping[str, object], *names: str) -> str:
    wanted = {name.strip().lower() for name in names if name}
    for column, value in row.items():
        column_name = str(column or "").strip().lower()
        if column_name in wanted:
            return _normalize_text(value)
    return ""


def parse_role_map_records(rows: Sequence[Mapping[str, object]]) -> List[RoleMapRow]:
    entries: List[RoleMapRow] = []
    for row in rows:
        category = _cell(row, "category")
        if not category:
            continue
        role_id_text = _cell(row, "role_id", "role id")
        if not role_id_text:
            continue
        try:
            role_id = int(role_id_text)
        except (TypeError, ValueError):
            continue
        sheet_role_name = _cell(row, "role_name", "role name")
        role_description = _cell(row, "role_description", "role description")
        role_usage = _cell(row, "usage")
        entries.append(
            RoleMapRow(
                category=category,
                role_id=role_id,
                sheet_role_name=sheet_role_name,
                role_description=role_description,
                role_usage=role_usage,
            )
        )
    return entries


async def fetch_role_map_rows(tab_name: str | None = None) -> List[RoleMapRow]:
    """Return parsed WhoWeAre rows from the configured worksheet."""

    sheet_id = _normalize_text(get_recruitment_sheet_id())
    if not sheet_id:
        raise RoleMapLoadError("Recruitment sheet ID is missing")

    rolemap_tab = _normalize_text(tab_name) or recruitment.get_role_map_tab_name()
    if not rolemap_tab:
        raise RoleMapLoadError("Role map tab name missing")

    try:
        records = await afetch_records(sheet_id, rolemap_tab)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - network/runtime failure
        log.warning("cluster_role_map: failed to read worksheet", exc_info=True)
        raise RoleMapLoadError(f"Failed to load worksheet '{rolemap_tab}': {exc}") from exc

    return parse_role_map_records(records)


def _category_order(entries: Iterable[RoleMapRow]) -> tuple[List[str], Dict[str, List[RoleMapRow]]]:
    order: List[str] = []
    grouped: Dict[str, List[RoleMapRow]] = {}
    for entry in entries:
        if entry.category not in grouped:
            grouped[entry.category] = []
            order.append(entry.category)
        grouped[entry.category].append(entry)
    return order, grouped


def _category_emoji(name: str) -> str:
    normalized = name.strip().lower()
    return CATEGORY_EMOJIS.get(normalized, "•")


def build_role_map_render(guild: discord.Guild | object, entries: Sequence[RoleMapRow]) -> RoleMapRender:
    """Compose the Discord message for the supplied WhoWeAre rows."""

    order, grouped = _category_order(entries)
    role_count = 0
    unassigned_roles = 0
    categories: List[RoleMapCategoryRender] = []

    get_role = getattr(guild, "get_role", None)

    for category in order:
        emoji = _category_emoji(category)
        role_rows: List[RoleEntryRender] = []
        for row in grouped.get(category, []):
            role_count += 1
            role = get_role(row.role_id) if callable(get_role) else None
            display_name = _normalize_text(row.sheet_role_name)
            if role is not None:
                members = list(getattr(role, "members", []) or [])
            else:
                members = []
            if not display_name:
                display_name = _normalize_text(getattr(role, "name", "")) if role is not None else ""
            if not display_name:
                display_name = f"role {row.role_id}"
            description = row.role_description or DEFAULT_DESCRIPTION
            usage = row.role_usage
            mentions: List[str] = []
            if members:
                mentions = [
                    str(getattr(member, "mention", getattr(member, "name", "")))
                    for member in members
                    if str(getattr(member, "mention", getattr(member, "name", "")))
                ]
            if not mentions:
                unassigned_roles += 1
            role_rows.append(
                RoleEntryRender(
                    role_id=row.role_id,
                    display_name=display_name,
                    description=description,
                    members=mentions,
                    usage=usage,
                )
            )
        if role_rows:
            categories.append(
                RoleMapCategoryRender(name=category, emoji=emoji, roles=role_rows)
            )

    return RoleMapRender(
        categories=categories,
        category_count=len(categories),
        role_count=role_count,
        unassigned_roles=unassigned_roles,
    )


def _mark_message(lines: Sequence[str]) -> str:
    body = "\n".join(lines).rstrip()
    if not body:
        return INVISIBLE_MARKER
    if body.endswith(INVISIBLE_MARKER):
        return body
    return f"{body}\n{INVISIBLE_MARKER}"


def build_index_placeholder() -> str:
    lines = list(INDEX_HEADER_LINES)
    lines.append("")
    lines.append(INDEX_BUILDING_NOTICE)
    return _mark_message(lines)


def build_index_message(links: Sequence[IndexLink], *, empty_reason: str | None = None) -> str:
    lines = list(INDEX_HEADER_LINES)
    lines.append("")
    if links:
        lines.append("Jump to:")
        for entry in links:
            lines.append(f"{entry.emoji} [{entry.name}]({entry.url})")
    else:
        lines.append(empty_reason or INDEX_EMPTY_NOTICE)
    lines.append("")
    lines.append(
        "↳ Tag roles instead of individuals so no one carries things alone! "
        "[Whispers to Leadership](https://discord.com/channels/689502814149672965/1345478723444539473) "
        "is there when things don’t fit a channel."
    )
    return _mark_message(lines)


def build_category_message(category: RoleMapCategoryRender) -> str:
    lines = [f"**{category.emoji} {category.name}**", ""]
    for role in category.roles:
        usage = _normalize_usage(role.usage)
        lines.append(f"**{role.display_name}**")
        description = role.description or DEFAULT_DESCRIPTION
        lines.append(f"{description}")
        if role.members:
            lines.append(
                f":small_blue_diamond: {', '.join(role.members)}"
            )
        else:
            lines.append(":small_blue_diamond: (currently unassigned)")
        if usage:
            lines.append(f"↳ Use <@&{role.role_id}> for {usage}")
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    return _mark_message(lines)


def build_category_embed(category: RoleMapCategoryRender) -> discord.Embed:
    embed = discord.Embed(title=f"{category.emoji} {category.name}")
    lines: List[str] = []
    for role in category.roles:
        usage = _normalize_usage(role.usage)
        lines.append(f"**{role.display_name}**")
        description = role.description or DEFAULT_DESCRIPTION
        lines.append(description)
        if role.members:
            lines.append(f":small_blue_diamond: {', '.join(role.members)}")
        else:
            lines.append(":small_blue_diamond: (currently unassigned)")
        if usage:
            lines.append(f"↳ Use <@&{role.role_id}> for {usage}")
        lines.append("")
    if lines and lines[-1] == "":
        lines.pop()
    embed.description = "\n".join(lines)
    return embed


def build_jump_url(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{int(guild_id)}/{int(channel_id)}/{int(message_id)}"


async def cleanup_previous_role_map_messages(
    channel: discord.abc.Messageable | object,
    *,
    bot_id: int | None,
    limit: int = 400,
) -> int:
    """Remove prior !whoweare posts (marked via INVISIBLE_MARKER)."""

    if bot_id is None:
        return 0

    history = getattr(channel, "history", None)
    if history is None:
        return 0

    to_delete: List[discord.Message] = []
    try:
        async for message in history(limit=limit):
            author = getattr(message, "author", None)
            if getattr(author, "id", None) != bot_id:
                continue
            content = getattr(message, "content", None) or ""
            if INVISIBLE_MARKER not in content:
                continue
            to_delete.append(message)
    except Exception:  # pragma: no cover - Discord history edge cases
        log.debug("cluster_role_map: failed to inspect channel history", exc_info=True)
        return 0

    if not to_delete:
        return 0

    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=14)
    recent: List[discord.Message] = []
    older: List[discord.Message] = []
    for message in to_delete:
        created_at = getattr(message, "created_at", None)
        if isinstance(created_at, dt.datetime) and created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=dt.timezone.utc)
        if isinstance(created_at, dt.datetime) and created_at >= cutoff:
            recent.append(message)
        else:
            older.append(message)

    deleted = 0
    delete_messages = getattr(channel, "delete_messages", None)
    if recent and callable(delete_messages):
        try:
            await delete_messages(recent)
            deleted += len(recent)
            recent = []
        except Exception:  # pragma: no cover - Discord bulk delete limitations
            log.debug("cluster_role_map: bulk delete failed", exc_info=True)

    for message in recent + older:
        try:
            await message.delete()
            deleted += 1
        except Exception:  # pragma: no cover - message already deleted / perms
            log.debug("cluster_role_map: failed to delete prior map message", exc_info=True)

    return deleted


__all__ = [
    "CATEGORY_EMOJIS",
    "MARKER_LINE",
    "ROLE_MAP_MARKER",
    "RoleMapRow",
    "RoleMapRender",
    "RoleEntryRender",
    "RoleMapCategoryRender",
    "IndexLink",
    "RoleMapLoadError",
    "build_role_map_render",
    "build_index_placeholder",
    "build_index_message",
    "build_category_message",
    "build_category_embed",
    "build_jump_url",
    "cleanup_previous_role_map_messages",
    "fetch_role_map_rows",
    "parse_role_map_records",
]
