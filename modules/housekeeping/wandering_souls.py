from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import discord

from shared import config
from shared.theme import colors

ACTIVITY_FOOTER = "Last activity and message count are based on tracked bot-visible activity only."
UNKNOWN = "unknown"
MAX_EMBED_DESCRIPTION = 3900


@dataclass(frozen=True)
class InvestigationEntry:
    member: Any
    last_activity: dt.datetime | None = None
    message_count: int | None = None


@dataclass(frozen=True)
class InvestigationResult:
    total_wandering: int
    excluded: int
    entries: tuple[InvestigationEntry, ...]


def _role_ids(member: Any) -> set[int]:
    return {int(role.id) for role in getattr(member, "roles", ()) if getattr(role, "id", None) is not None}


def _resolve_required_role(guild: Any, env_name: str, role_id: int | None) -> tuple[Any | None, str | None]:
    if role_id is None:
        return None, f"Missing or invalid {env_name}. Set it to a numeric Discord role ID."
    role = guild.get_role(role_id) if guild is not None else None
    if role is None:
        return None, f"Configured {env_name}={role_id} was not found in this guild."
    return role, None


def resolve_investigation_roles(guild: Any) -> tuple[Any | None, Any | None, str | None]:
    wandering_role, error = _resolve_required_role(
        guild, "WANDERING_SOULS_ROLE_ID", config.get_wandering_souls_role_id()
    )
    if error:
        return None, None, error
    exclude_role, error = _resolve_required_role(
        guild,
        "WANDERING_SOULS_EXCLUDE_ROLE_ID",
        config.get_wandering_souls_exclude_role_id(),
    )
    if error:
        return None, None, error
    return wandering_role, exclude_role, None


def collect_wandering_souls(guild: Any, wandering_role_id: int, exclude_role_id: int) -> InvestigationResult:
    wandering: list[Any] = []
    entries: list[InvestigationEntry] = []
    excluded = 0
    for member in getattr(guild, "members", ()):
        ids = _role_ids(member)
        if wandering_role_id not in ids:
            continue
        wandering.append(member)
        if exclude_role_id in ids:
            excluded += 1
            continue
        entries.append(InvestigationEntry(member=member))
    entries.sort(key=lambda entry: (entry.last_activity is None, entry.last_activity or dt.datetime.max.replace(tzinfo=dt.timezone.utc), getattr(entry.member, "display_name", "")))
    return InvestigationResult(total_wandering=len(wandering), excluded=excluded, entries=tuple(entries))


def _format_date(value: Any) -> str:
    if value is None:
        return UNKNOWN
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return str(value)


def _entry_block(entry: InvestigationEntry) -> str:
    member = entry.member
    display_name = getattr(member, "display_name", None) or getattr(member, "name", None) or str(getattr(member, "id", "unknown"))
    joined = _format_date(getattr(member, "joined_at", None))
    activity = _format_date(entry.last_activity)
    messages = UNKNOWN if entry.message_count is None else str(entry.message_count)
    return f"Player: {display_name}\nJoined: {joined}\nLast activity: {activity}\nMessages: {messages}"


def build_investigation_embeds(result: InvestigationResult) -> list[discord.Embed]:
    summary = (
        f"Total members with Wandering Souls role: {result.total_wandering}\n"
        f"Excluded members count: {result.excluded}\n"
        f"Final listed members count: {len(result.entries)}"
    )
    embeds: list[discord.Embed] = []
    current = summary
    page_entries = 0
    for entry in result.entries:
        block = "\n\n" + _entry_block(entry)
        if page_entries and len(current) + len(block) > MAX_EMBED_DESCRIPTION:
            embed = discord.Embed(title="Wandering Souls Investigation", description=current, colour=colors.admin)
            embed.set_footer(text=ACTIVITY_FOOTER)
            embeds.append(embed)
            current = summary + block
            page_entries = 1
        else:
            current += block
            page_entries += 1
    embed = discord.Embed(title="Wandering Souls Investigation", description=current, colour=colors.admin)
    embed.set_footer(text=ACTIVITY_FOOTER)
    embeds.append(embed)
    for index, embed in enumerate(embeds, start=1):
        if len(embeds) > 1:
            embed.title = f"Wandering Souls Investigation ({index}/{len(embeds)})"
    return embeds


def build_diagnostics_embed() -> discord.Embed:
    return discord.Embed(
        title="Wandering Souls Diagnostics",
        description="Use `!wanderingsouls investigate` to list current Wandering Souls members.",
        colour=colors.admin,
    )


def build_error_embed(message: str) -> discord.Embed:
    return discord.Embed(
        title="Wandering Souls Investigation Error",
        description=message,
        colour=discord.Colour.red(),
    )
