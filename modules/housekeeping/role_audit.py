from __future__ import annotations

"""Scheduled audit for roles and visitor ticket hygiene."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

import discord
from discord.ext import commands

from modules.common.embeds import get_embed_colour
from modules.common.tickets import TicketThread, fetch_ticket_threads
from shared.config import (
    get_log_channel_id,
    get_logging_channel_id,
    get_admin_audit_dest_id,
    get_allowed_guild_ids,
    get_clan_role_ids,
    get_promo_channel_id,
    get_raid_role_id,
    get_visitor_role_id,
    get_wandering_souls_role_id,
    get_welcome_channel_id,
)

log = logging.getLogger("c1c.housekeeping.role_audit")

ROLE_AUDIT_REASON = "Housekeeping role audit"


def resolve_audit_destination() -> tuple[int | None, str]:
    """Return audit destination ID with config-source label."""
    for key, value in (
        ("ADMIN_AUDIT_DEST_ID", get_admin_audit_dest_id()),
        ("LOG_CHANNEL_ID", get_log_channel_id()),
        ("LOGGING_CHANNEL_ID", get_logging_channel_id()),
    ):
        if value:
            return value, key
    return None, "unconfigured"


@dataclass(slots=True)
class AuditResult:
    checked: int = 0
    auto_fixed_strays: list[discord.Member] | None = None
    auto_fixed_wanderers: list[discord.Member] | None = None
    wanderers_with_clans: list[tuple[discord.Member, list[discord.Role]]] | None = None
    visitors_no_ticket: list[discord.Member] | None = None
    visitors_closed_only: list[tuple[discord.Member, list[TicketThread]]] | None = None
    visitors_extra_roles: list[tuple[discord.Member, list[discord.Role], list[TicketThread]]] | None = None
    proposed_role_mutations: list[tuple[discord.Member, list[discord.Role], list[discord.Role]]] | None = None


def _member_roles(member: discord.Member) -> set[int]:
    return {getattr(role, "id", 0) for role in getattr(member, "roles", [])}


def _classify_roles(
    member_roles: set[int], *, raid_role_id: int, wanderer_role_id: int, clan_role_ids: set[int]
) -> str:
    has_raid = raid_role_id in member_roles
    has_wanderer = wanderer_role_id in member_roles
    has_clan = bool(member_roles & clan_role_ids)

    if has_raid and not has_clan and not has_wanderer:
        return "stray"
    if has_raid and has_wanderer and not has_clan:
        return "drop_raid"
    if has_wanderer and has_clan:
        return "wander_with_clan"
    return "ok"


def _extra_roles(member: discord.Member, visitor_role_id: int) -> list[discord.Role]:
    visitor_and_everyone = {visitor_role_id, getattr(getattr(member, "guild", None), "id", -1)}
    extras: list[discord.Role] = []
    for role in getattr(member, "roles", []):
        if getattr(role, "id", 0) in visitor_and_everyone:
            continue
        extras.append(role)
    return extras


def _format_member(member: discord.Member) -> str:
    mention = getattr(member, "mention", None)
    if mention:
        return mention
    name = getattr(member, "display_name", None) or getattr(member, "name", None)
    if name:
        return f"{name} ({getattr(member, 'id', 'unknown')})"
    return str(getattr(member, "id", "unknown"))


def _format_roles(roles: Iterable[discord.Role]) -> str:
    labels = []
    for role in roles:
        name = getattr(role, "name", "")
        labels.append(f"`{name}`" if name else f"`{getattr(role, 'id', 'role')}`")
    return ", ".join(labels) if labels else "`-`"


def _format_ticket_links(tickets: Sequence[TicketThread]) -> str:
    return ", ".join(f"[{ticket.name}]({ticket.url})" for ticket in tickets) or "-"


async def _apply_role_changes(
    member: discord.Member,
    *,
    actor: str = "manual",
    dry_run: bool = True,
    remove: Sequence[discord.Role] = (),
    add: Sequence[discord.Role] = (),
) -> bool:
    actor_normalized = (actor or "").strip().lower()
    scheduled_actors = {"scheduled", "background", "cron", "startup", "ready"}
    if dry_run or actor_normalized in scheduled_actors:
        return True
    before_roles = [getattr(role, "name", str(getattr(role, "id", "unknown"))) for role in getattr(member, "roles", [])]
    try:
        if remove:
            await member.remove_roles(*remove, reason=ROLE_AUDIT_REASON)
        if add:
            await member.add_roles(*add, reason=ROLE_AUDIT_REASON)
    except discord.Forbidden:
        log.warning(
            "role audit skipped member — missing permissions",
            extra={"member_id": getattr(member, "id", None)},
        )
        return False
    except discord.HTTPException as exc:
        log.warning(
            "role audit member update failed",
            exc_info=True,
            extra={"member_id": getattr(member, "id", None), "error": str(exc)},
        )
        return False
    after_roles = [getattr(role, "name", str(getattr(role, "id", "unknown"))) for role in getattr(member, "roles", [])]
    log.info(
        "role audit mutation applied",
        extra={
            "actor": actor,
            "member_id": getattr(member, "id", None),
            "member_name": getattr(member, "display_name", None) or getattr(member, "name", None),
            "before_roles": before_roles,
            "after_roles": after_roles,
            "remove_roles": [getattr(r, "name", str(getattr(r, "id", "unknown"))) for r in remove],
            "add_roles": [getattr(r, "name", str(getattr(r, "id", "unknown"))) for r in add],
            "reason": ROLE_AUDIT_REASON,
            "success": True,
        },
    )
    return True


async def _audit_guild(
    bot: commands.Bot,
    guild: discord.Guild,
    *,
    raid_role_id: int,
    wanderer_role_id: int,
    visitor_role_id: int,
    clan_role_ids: set[int],
    raid_role_name: str,
    wanderer_role_name: str,
    actor: str = "manual",
    dry_run: bool = True,
) -> AuditResult | None:
    raid_role = guild.get_role(raid_role_id)
    wanderer_role = guild.get_role(wanderer_role_id)
    visitor_role = guild.get_role(visitor_role_id)
    if not all((raid_role, wanderer_role, visitor_role)):
        log.warning(
            "role audit skipped guild — missing roles",
            extra={
                "guild_id": getattr(guild, "id", None),
                "raid": bool(raid_role),
                "wanderer": bool(wanderer_role),
                "visitor": bool(visitor_role),
            },
        )
        return None

    try:
        members = [member async for member in guild.fetch_members(limit=None)]
    except Exception:
        members = list(getattr(guild, "members", []))

    tickets = await fetch_ticket_threads(
        bot,
        include_archived=True,
        with_members=True,
        guild_id=getattr(guild, "id", None),
    )

    ticket_map: dict[int, list[TicketThread]] = {}
    for ticket in tickets:
        for member_id in ticket.member_ids:
            ticket_map.setdefault(int(member_id), []).append(ticket)

    clan_lookup = {role.id: role for role in getattr(guild, "roles", []) if role.id in clan_role_ids}

    result = AuditResult(
        checked=len(members),
        auto_fixed_strays=[],
        auto_fixed_wanderers=[],
        wanderers_with_clans=[],
        visitors_no_ticket=[],
        visitors_closed_only=[],
        visitors_extra_roles=[],
        proposed_role_mutations=[],
    )

    for member in members:
        member_roles = _member_roles(member)

        classification = _classify_roles(
            member_roles,
            raid_role_id=raid_role_id,
            wanderer_role_id=wanderer_role_id,
            clan_role_ids=clan_role_ids,
        )
        if classification == "stray":
            result.proposed_role_mutations.append((member, [raid_role], [wanderer_role]))
            changed = await _apply_role_changes(
                member,
                actor=actor,
                dry_run=dry_run,
                remove=(raid_role,),
                add=(wanderer_role,),
            )
            if changed:
                result.auto_fixed_strays.append(member)
            continue

        if classification == "drop_raid":
            result.proposed_role_mutations.append((member, [raid_role], []))
            changed = await _apply_role_changes(
                member,
                actor=actor,
                dry_run=dry_run,
                remove=(raid_role,),
            )
            if changed:
                result.auto_fixed_wanderers.append(member)
            continue

        if classification == "wander_with_clan":
            clan_roles = [clan_lookup[role_id] for role_id in member_roles & clan_role_ids if role_id in clan_lookup]
            result.wanderers_with_clans.append((member, clan_roles))

        if visitor_role_id not in member_roles:
            continue

        member_tickets = ticket_map.get(getattr(member, "id", 0), [])
        open_tickets = [ticket for ticket in member_tickets if ticket.is_open]

        extras = _extra_roles(member, visitor_role_id)
        if extras:
            result.visitors_extra_roles.append((member, extras, member_tickets))

        if not member_tickets:
            result.visitors_no_ticket.append(member)
            continue

        if not open_tickets:
            result.visitors_closed_only.append((member, member_tickets))

    return result


def _render_section(title: str, lines: Sequence[str]) -> list[str]:
    content = lines or ["• None"]
    return [f"**{title}**", *content]


def _render_report(
    *,
    summary: AuditResult,
    raid_role_name: str,
    wanderer_role_name: str,
    dry_run: bool = True,
) -> discord.Embed:
    date_text = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    parts: list[str] = []

    stray_action = "Would remove" if dry_run else "Removed"
    stray_add_action = "would add" if dry_run else "added"
    wanderer_action = "Would remove" if dry_run else "Removed"
    stray_lines = [
        f"• {_format_member(member)} – {stray_action} `{raid_role_name}`, {stray_add_action} `{wanderer_role_name}` (no clan tags)"
        for member in (summary.auto_fixed_strays or [])
    ]
    wanderer_lines = [
        f"• {_format_member(member)} – {wanderer_action} `{raid_role_name}`, kept `{wanderer_role_name}` (no clan tags)"
        for member in (summary.auto_fixed_wanderers or [])
    ]
    parts.extend(_render_section("1) Auto-fixed stray members", stray_lines + wanderer_lines))
    parts.append("")

    manual_lines = [
        f"• {_format_member(member)} – Has `{wanderer_role_name}` and clan tags: {_format_roles(clan_roles)}"
        for member, clan_roles in (summary.wanderers_with_clans or [])
    ]
    parts.extend(
        _render_section(
            "2) Manual review – Wandering Souls with clan tags",
            manual_lines,
        )
    )
    parts.append("")

    visitor_no_ticket = [f"• {_format_member(member)} – no ticket found" for member in (summary.visitors_no_ticket or [])]
    parts.extend(_render_section("3) Visitors without any ticket", visitor_no_ticket))
    parts.append("")

    visitor_closed_only = [
        f"• {_format_member(member)} – Tickets: {_format_ticket_links(tickets)}"
        for member, tickets in (summary.visitors_closed_only or [])
    ]
    parts.extend(_render_section("4) Visitors with only closed tickets", visitor_closed_only))
    parts.append("")

    visitor_extra_roles = [
        f"• {_format_member(member)} – Roles: {_format_roles(roles)} – Tickets: {_format_ticket_links(tickets)}"
        for member, roles, tickets in (summary.visitors_extra_roles or [])
    ]
    parts.extend(_render_section("5) Visitors with extra roles", visitor_extra_roles))

    embed = discord.Embed(
        title="🧹 Role & Visitor Audit",
        description="\n".join(parts).strip(),
        colour=get_embed_colour("admin"),
    )
    embed.set_footer(text=f"Date: {date_text} • Checked: {summary.checked} members")

    return embed


async def run_role_and_visitor_audit(
    bot: commands.Bot,
    *,
    actor: str = "manual",
    dry_run: bool = True,
    max_mutations: int = 10,
    allow_over_cap: bool = False,
) -> tuple[bool, str]:
    raid_role_id = get_raid_role_id()
    wanderer_role_id = get_wandering_souls_role_id()
    visitor_role_id = get_visitor_role_id()
    clan_role_ids = get_clan_role_ids()
    dest_id, dest_source = resolve_audit_destination()

    if not all((raid_role_id, wanderer_role_id, visitor_role_id, clan_role_ids, dest_id)):
        return False, "config-missing"

    if not (get_welcome_channel_id() or get_promo_channel_id()):
        return False, "ticket-channels-missing"

    allowed = get_allowed_guild_ids()
    target_guilds = [guild for guild in bot.guilds if not allowed or guild.id in allowed]
    if not target_guilds:
        return False, "no-guilds"

    raid_role_name = "Raid"
    wanderer_role_name = "Wandering Souls"
    aggregated = AuditResult(
        checked=0,
        auto_fixed_strays=[],
        auto_fixed_wanderers=[],
        wanderers_with_clans=[],
        visitors_no_ticket=[],
        visitors_closed_only=[],
        visitors_extra_roles=[],
        proposed_role_mutations=[],
    )

    for guild in target_guilds:
        raid_role = guild.get_role(raid_role_id)
        wanderer_role = guild.get_role(wanderer_role_id)
        if raid_role and getattr(raid_role, "name", None):
            raid_role_name = raid_role.name
        if wanderer_role and getattr(wanderer_role, "name", None):
            wanderer_role_name = wanderer_role.name

        result = await _audit_guild(
            bot,
            guild,
            raid_role_id=raid_role_id,
            wanderer_role_id=wanderer_role_id,
            visitor_role_id=visitor_role_id,
            clan_role_ids=clan_role_ids,
            raid_role_name=raid_role_name,
            wanderer_role_name=wanderer_role_name,
            actor=actor,
            dry_run=dry_run,
        )
        if result is None:
            continue

        aggregated.checked += result.checked
        aggregated.auto_fixed_strays.extend(result.auto_fixed_strays or [])
        aggregated.auto_fixed_wanderers.extend(result.auto_fixed_wanderers or [])
        aggregated.wanderers_with_clans.extend(result.wanderers_with_clans or [])
        aggregated.visitors_no_ticket.extend(result.visitors_no_ticket or [])
        aggregated.visitors_closed_only.extend(result.visitors_closed_only or [])
        aggregated.visitors_extra_roles.extend(result.visitors_extra_roles or [])
        aggregated.proposed_role_mutations.extend(result.proposed_role_mutations or [])

    if aggregated.checked == 0:
        return False, "no-members"

    proposed_adds = len(aggregated.auto_fixed_strays or [])
    proposed_removes = len(aggregated.auto_fixed_strays or []) + len(aggregated.auto_fixed_wanderers or [])
    actor_normalized = (actor or "").strip().lower()
    scheduled_actors = {"scheduled", "background", "cron", "startup", "ready"}
    if dry_run or actor_normalized in scheduled_actors:
        log.info(
            "role audit mutations skipped",
            extra={
                "reason": "scheduled_report_only",
                "actor": actor,
                "guilds": ",".join(
                    f"{getattr(guild, 'id', 'unknown')}:{getattr(guild, 'name', 'unknown')}"
                    for guild in target_guilds
                ),
                "member_count_scanned": aggregated.checked,
                "proposed_add_count": proposed_adds,
                "proposed_remove_count": proposed_removes,
            },
        )
    elif len(aggregated.proposed_role_mutations or []) > max_mutations and not allow_over_cap:
        log.warning(
            "role audit apply aborted due to mutation cap",
            extra={
                "reason": "mutation_cap_exceeded",
                "actor": actor,
                "max_mutations": max_mutations,
                "proposed_mutations": len(aggregated.proposed_role_mutations or []),
            },
        )
        return False, "mutation-cap-exceeded"

    await bot.wait_until_ready()
    channel = bot.get_channel(dest_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(dest_id)
        except Exception as exc:  # pragma: no cover - defensive guard
            log.warning("role audit destination lookup failed", exc_info=True)
            return False, f"dest:{type(exc).__name__}"

    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return False, "dest-invalid"
    log.info(
        "role audit destination resolved",
        extra={
            "destination_source": "env",
            "destination_key": dest_source,
            "destination_id": dest_id,
            "destination_label": getattr(channel, "name", str(dest_id)),
            "destination_kind": type(channel).__name__,
        },
    )

    embed = _render_report(
        summary=aggregated,
        raid_role_name=raid_role_name,
        wanderer_role_name=wanderer_role_name,
        dry_run=dry_run or actor_normalized in scheduled_actors,
    )

    try:
        await channel.send(embed=embed)
    except Exception as exc:
        log.warning("failed to send role audit report", exc_info=True)
        return False, f"send:{type(exc).__name__}"

    return True, "-"


async def preview_role_audit_mutations(
    bot: commands.Bot, *, actor: str = "manual"
) -> tuple[bool, str, AuditResult | None]:
    ok, error = await run_role_and_visitor_audit(bot, actor=actor, dry_run=True)
    if not ok:
        return False, error, None
    # recompute detail snapshot without side effects
    raid_role_id = get_raid_role_id()
    wanderer_role_id = get_wandering_souls_role_id()
    visitor_role_id = get_visitor_role_id()
    clan_role_ids = get_clan_role_ids()
    allowed = get_allowed_guild_ids()
    target_guilds = [guild for guild in bot.guilds if not allowed or guild.id in allowed]
    combined = AuditResult(
        checked=0,
        auto_fixed_strays=[],
        auto_fixed_wanderers=[],
        wanderers_with_clans=[],
        visitors_no_ticket=[],
        visitors_closed_only=[],
        visitors_extra_roles=[],
        proposed_role_mutations=[],
    )
    for guild in target_guilds:
        result = await _audit_guild(
            bot,
            guild,
            raid_role_id=raid_role_id,
            wanderer_role_id=wanderer_role_id,
            visitor_role_id=visitor_role_id,
            clan_role_ids=clan_role_ids,
            raid_role_name="Raid",
            wanderer_role_name="Wandering Souls",
            actor=actor,
            dry_run=True,
        )
        if result is None:
            continue
        combined.checked += result.checked
        combined.proposed_role_mutations.extend(result.proposed_role_mutations or [])
    return True, "-", combined


__all__ = ["run_role_and_visitor_audit", "preview_role_audit_mutations", "AuditResult"]
