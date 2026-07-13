from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import discord

from shared import config
from shared.theme import colors

ACTIVITY_FOOTER = "Message stats are based on bot-visible channel history scanned for this command."
UNKNOWN = "unknown"
MAX_EMBED_DESCRIPTION = 3900
DEFAULT_SCAN_DAYS = 90
MIN_SCAN_DAYS = 1
MAX_SCAN_DAYS = 180


@dataclass(frozen=True)
class InvestigationEntry:
    member: Any
    last_message_at: dt.datetime | None = None
    scanned_message_count: int = 0
    last_message_channel: Any | None = None


@dataclass(frozen=True)
class InvestigationResult:
    total_wandering: int
    excluded: int
    entries: tuple[InvestigationEntry, ...]
    scan_days: int = DEFAULT_SCAN_DAYS
    scan_warning_count: int = 0


def parse_scan_days(value: str | None) -> tuple[int | None, str | None]:
    if value is None:
        return DEFAULT_SCAN_DAYS, None
    try:
        days = int(value)
    except (TypeError, ValueError):
        return None, "Accepted syntax: `!wanderingsouls investigate` or `!wanderingsouls investigate <days>` where `<days>` is a whole number from 1 to 180."
    return max(MIN_SCAN_DAYS, min(MAX_SCAN_DAYS, days)), None


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


def collect_wandering_souls(guild: Any, wandering_role_id: int, exclude_role_id: int, *, scan_days: int = DEFAULT_SCAN_DAYS) -> InvestigationResult:
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
    entries = _sort_entries(entries)
    return InvestigationResult(total_wandering=len(wandering), excluded=excluded, entries=tuple(entries), scan_days=scan_days)


def _sort_entries(entries: list[InvestigationEntry]) -> list[InvestigationEntry]:
    return sorted(
        entries,
        key=lambda entry: (
            entry.last_message_at is None,
            entry.last_message_at or dt.datetime.max.replace(tzinfo=dt.timezone.utc),
            getattr(entry.member, "joined_at", None) or dt.datetime.max.replace(tzinfo=dt.timezone.utc),
            (getattr(entry.member, "display_name", None) or "").casefold(),
        ),
    )


def _scan_channels(guild: Any) -> list[Any]:
    channels = list(getattr(guild, "text_channels", ()) or ())
    channels.extend(getattr(guild, "threads", ()) or ())
    return channels


async def scan_recent_messages(guild: Any, result: InvestigationResult, *, now: dt.datetime | None = None) -> InvestigationResult:
    if now is None:
        now = dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=dt.timezone.utc)
    cutoff = now - dt.timedelta(days=result.scan_days)
    candidate_ids = {int(getattr(entry.member, "id")) for entry in result.entries}
    stats: dict[int, dict[str, Any]] = {member_id: {"count": 0, "last_at": None, "channel": None} for member_id in candidate_ids}
    warnings = 0
    for channel in _scan_channels(guild):
        history = getattr(channel, "history", None)
        if history is None:
            continue
        try:
            async for message in history(limit=None, after=cutoff, oldest_first=False):
                created_at = getattr(message, "created_at", None)
                if isinstance(created_at, dt.datetime):
                    if created_at.tzinfo is None:
                        created_at = created_at.replace(tzinfo=dt.timezone.utc)
                    if created_at < cutoff:
                        break
                author_id = getattr(getattr(message, "author", None), "id", None)
                if author_id not in candidate_ids:
                    continue
                item = stats[int(author_id)]
                item["count"] += 1
                if created_at is not None and (item["last_at"] is None or created_at > item["last_at"]):
                    item["last_at"] = created_at
                    item["channel"] = channel
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            warnings += 1
            continue
    entries = [
        InvestigationEntry(
            member=entry.member,
            last_message_at=stats[int(getattr(entry.member, "id"))]["last_at"],
            scanned_message_count=stats[int(getattr(entry.member, "id"))]["count"],
            last_message_channel=stats[int(getattr(entry.member, "id"))]["channel"],
        )
        for entry in result.entries
    ]
    return InvestigationResult(result.total_wandering, result.excluded, tuple(_sort_entries(entries)), result.scan_days, warnings)


def _format_date(value: Any, *, none_text: str = UNKNOWN) -> str:
    if value is None:
        return none_text
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt.timezone.utc)
        return value.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return str(value)


def _channel_reference(channel: Any | None) -> str:
    if channel is None:
        return UNKNOWN
    return getattr(channel, "mention", None) or getattr(channel, "name", None) or UNKNOWN


def _entry_block(entry: InvestigationEntry) -> str:
    member = entry.member
    member_id = getattr(member, "id", "unknown")
    display_name = getattr(member, "display_name", None) or getattr(member, "name", None) or str(member_id)
    joined = _format_date(getattr(member, "joined_at", None))
    last_message = _format_date(entry.last_message_at, none_text="none found in scan window")
    return (
        f"Profile: <@{member_id}>\n"
        f"Name: {display_name}\n"
        f"ID: {member_id}\n"
        f"Joined: {joined}\n"
        f"Last message: {last_message}\n"
        f"Messages in scan window: {entry.scanned_message_count}\n"
        f"Last seen channel: {_channel_reference(entry.last_message_channel)}"
    )


def build_investigation_embeds(result: InvestigationResult) -> list[discord.Embed]:
    summary = (
        f"Total members with Wandering Souls role: {result.total_wandering}\n"
        f"Excluded members count: {result.excluded}\n"
        f"Final listed members count: {len(result.entries)}\n"
        f"Message scan window: last {result.scan_days} days"
    )
    if result.scan_warning_count:
        summary += f"\nScan warning count: {result.scan_warning_count} channel(s) could not be read"
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
