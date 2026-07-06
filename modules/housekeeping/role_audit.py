from __future__ import annotations

"""Scheduled audit for roles and visitor ticket hygiene."""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Sequence

import discord
from discord.ext import commands

from modules.community.fusion import role_cleanup as fusion_role_cleanup
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
    members_only_everyone: list[discord.Member] | None = None
    fusion_role_cleanup: list[fusion_role_cleanup.FusionRoleCleanupSummary] | None = None
    proposed_role_mutations: list[tuple[discord.Member, list[discord.Role], list[discord.Role]]] | None = None
    action_roles_removed: list[str] | None = None
    action_roles_added: list[str] | None = None
    action_users_kicked: list[str] | None = None
    action_failed_or_skipped: list[str] | None = None


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


def _is_only_everyone_member(member: discord.Member) -> bool:
    """Return True for non-bot members whose only role is the guild @everyone role."""
    if getattr(member, "bot", False):
        return False
    guild_id = getattr(getattr(member, "guild", None), "id", None)
    role_ids = _member_roles(member)
    if guild_id is not None:
        role_ids.discard(int(guild_id))
    return not role_ids


def _format_member(member: discord.Member) -> str:
    mention = getattr(member, "mention", None)
    if mention:
        return mention
    name = getattr(member, "display_name", None) or getattr(member, "name", None)
    if name:
        return f"{name} ({getattr(member, 'id', 'unknown')})"
    return str(getattr(member, "id", "unknown"))


def _format_member_with_username(member: discord.Member) -> str:
    name = getattr(member, "display_name", None) or getattr(member, "name", None)
    if not name:
        name = str(getattr(member, "id", "unknown"))
    return f"{_format_member(member)} – {name}"


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
) -> tuple[bool, str | None]:
    actor_normalized = (actor or "").strip().lower()
    scheduled_actors = {"scheduled", "background", "cron", "startup", "ready"}
    if dry_run or actor_normalized in scheduled_actors:
        return True, None
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
        return False, "missing permission"
    except discord.HTTPException as exc:
        log.warning(
            "role audit member update failed",
            exc_info=True,
            extra={"member_id": getattr(member, "id", None), "error": str(exc)},
        )
        return False, str(exc)
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
    return True, None


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
        members_only_everyone=[],
        proposed_role_mutations=[],
        action_roles_removed=[],
        action_roles_added=[],
        action_users_kicked=[],
        action_failed_or_skipped=[],
    )

    for member in members:
        member_roles = _member_roles(member)
        if _is_only_everyone_member(member):
            result.members_only_everyone.append(member)

        classification = _classify_roles(
            member_roles,
            raid_role_id=raid_role_id,
            wanderer_role_id=wanderer_role_id,
            clan_role_ids=clan_role_ids,
        )
        if classification == "stray":
            result.proposed_role_mutations.append((member, [raid_role], [wanderer_role]))
            changed, error = await _apply_role_changes(
                member,
                actor=actor,
                dry_run=dry_run,
                remove=(raid_role,),
                add=(wanderer_role,),
            )
            if changed:
                result.auto_fixed_strays.append(member)
                if not dry_run:
                    result.action_roles_removed.append(
                        f"• {_format_member(member)} – removed `{raid_role_name}`"
                    )
                    result.action_roles_added.append(
                        f"• {_format_member(member)} – added `{wanderer_role_name}`"
                    )
            elif error:
                result.action_failed_or_skipped.append(
                    f"• {_format_member(member)} – could not update roles: {error}"
                )
            continue

        if classification == "drop_raid":
            result.proposed_role_mutations.append((member, [raid_role], []))
            changed, error = await _apply_role_changes(
                member,
                actor=actor,
                dry_run=dry_run,
                remove=(raid_role,),
            )
            if changed:
                result.auto_fixed_wanderers.append(member)
                if not dry_run:
                    result.action_roles_removed.append(
                        f"• {_format_member(member)} – removed `{raid_role_name}`"
                    )
            elif error:
                result.action_failed_or_skipped.append(
                    f"• {_format_member(member)} – could not remove `{raid_role_name}`: {error}"
                )
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
    return [f"**{title}**", *lines]


def _format_joined_date(member: discord.abc.User) -> str:
    joined_at = getattr(member, "joined_at", None)
    if not joined_at:
        return "unknown"
    return joined_at.strftime("%Y-%m-%d")


def _build_fusion_cleanup_lines(
    summaries: Sequence[fusion_role_cleanup.FusionRoleCleanupSummary],
) -> list[str]:
    lines: list[str] = []
    for item in summaries:
        role_label = item.role_name or "unknown"
        status = "already processed/skipped" if item.already_processed else item.status
        lines.append(
            "• "
            f"{item.fusion_name or item.fusion_id} (`{item.fusion_id}`) – "
            f"role `{role_label}` ({item.role_id or 'missing'}); "
            f"found={item.members_found}, removed={item.removed_count}, "
            f"failed={item.failed_count}, skipped={item.skipped_count}; "
            f"dedupe=`{item.dedupe_key}` {status}"
        )
        for reason in item.failure_reasons:
            lines.append(f"  ◦ failure: {reason}")
    return lines


def _build_report_sections(
    *,
    summary: AuditResult,
    raid_role_name: str,
    wanderer_role_name: str,
    dry_run: bool = True,
) -> tuple[list[tuple[str, list[str]]], list[tuple[str, list[str]]]]:
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
    detected_sections: list[tuple[str, list[str]]] = []
    action_sections: list[tuple[str, list[str]]] = []
    if stray_lines or wanderer_lines:
        detected_sections.append(("1) Stray members", stray_lines + wanderer_lines))

    manual_lines = [
        f"• {_format_member(member)} – Has `{wanderer_role_name}` and clan tags: {_format_roles(clan_roles)}"
        for member, clan_roles in (summary.wanderers_with_clans or [])
    ]
    if manual_lines:
        detected_sections.append(("2) Manual review – Wandering Souls with clan tags", manual_lines))

    visitor_no_ticket = [
        f"• {_format_member(member)} – joined {_format_joined_date(member)} – no ticket found"
        for member in (summary.visitors_no_ticket or [])
    ]
    if visitor_no_ticket:
        detected_sections.append(("3) Visitors without any ticket", visitor_no_ticket))

    visitor_closed_only = [
        f"• {_format_member(member)} – Tickets: {_format_ticket_links(tickets)}"
        for member, tickets in (summary.visitors_closed_only or [])
    ]
    if visitor_closed_only:
        detected_sections.append(("4) Visitors with only closed tickets", visitor_closed_only))

    visitor_extra_roles = [
        f"• {_format_member(member)} – Roles: {_format_roles(roles)} – Tickets: {_format_ticket_links(tickets)}"
        for member, roles, tickets in (summary.visitors_extra_roles or [])
    ]
    if visitor_extra_roles:
        detected_sections.append(("5) Visitors with extra roles", visitor_extra_roles))

    only_everyone_lines = [
        f"• {_format_member_with_username(member)}"
        for member in (summary.members_only_everyone or [])
    ]
    if only_everyone_lines:
        detected_sections.append(("6) Members with only @everyone", only_everyone_lines))

    fusion_lines = _build_fusion_cleanup_lines(summary.fusion_role_cleanup or [])
    if fusion_lines:
        detected_sections.append(("7) Fusion role cleanup", fusion_lines))

    if summary.action_roles_removed:
        action_sections.append(("6) Roles removed", summary.action_roles_removed))
    if summary.action_roles_added:
        action_sections.append(("7) Roles added", summary.action_roles_added))
    if summary.action_users_kicked:
        action_sections.append(("8) Users kicked", summary.action_users_kicked))
    if summary.action_failed_or_skipped:
        action_sections.append(("9) Failed / skipped actions", summary.action_failed_or_skipped))

    return detected_sections, action_sections


_SAFE_DESCRIPTION_LIMIT = 3800


def _section_blocks(sections: Sequence[tuple[str, list[str]]], *, heading: str) -> list[list[str]]:
    if not sections:
        return []
    blocks: list[list[str]] = [["━━━━━━━━━━━━", heading, "━━━━━━━━━━━━", ""]]
    for idx, (title, lines) in enumerate(sections):
        section_lines = _render_section(title, lines)
        if idx < len(sections) - 1:
            section_lines.append("")
        blocks.append(section_lines)
    return blocks


def _split_report_parts(blocks: Sequence[Sequence[str]]) -> list[str]:
    parts: list[str] = []
    current: list[str] = []

    def _joined_len(lines: Sequence[str]) -> int:
        return len("\n".join(lines).strip())

    for block in blocks:
        block_lines = list(block)
        if current and _joined_len([*current, *block_lines]) > _SAFE_DESCRIPTION_LIMIT:
            parts.append("\n".join(current).strip())
            current = []
        if _joined_len(block_lines) <= _SAFE_DESCRIPTION_LIMIT:
            current.extend(block_lines)
            continue

        header = block_lines[:1]
        entries = block_lines[1:]
        if not current:
            current.extend(header)
        for line in entries:
            if current and _joined_len([*current, line]) > _SAFE_DESCRIPTION_LIMIT:
                parts.append("\n".join(current).strip())
                current = header.copy()
            current.append(line)
    if current or not parts:
        parts.append("\n".join(current).strip())
    return parts


def _render_report_embeds(
    *,
    summary: AuditResult,
    raid_role_name: str,
    wanderer_role_name: str,
    dry_run: bool = True,
) -> list[discord.Embed]:
    date_text = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    detected_sections, action_sections = _build_report_sections(
        summary=summary,
        raid_role_name=raid_role_name,
        wanderer_role_name=wanderer_role_name,
        dry_run=dry_run,
    )
    blocks: list[list[str]] = []
    if detected_sections:
        blocks.extend(_section_blocks(detected_sections, heading="DETECTED ISSUES"))

    if action_sections:
        if blocks:
            blocks.append([""])
        blocks.extend(_section_blocks(action_sections, heading="ACTIONS PERFORMED"))

    parts = _split_report_parts(blocks)
    embeds: list[discord.Embed] = []
    for idx, description in enumerate(parts, start=1):
        total = len(parts)
        title = "🧹 Role & Visitor Audit"
        if total > 1:
            title = f"{title} ({idx}/{total})"
        embed = discord.Embed(
            title=title,
            description=description,
            colour=get_embed_colour("admin"),
        )
        footer = f"Date: {date_text} • Checked: {summary.checked} members"
        if total > 1:
            footer = f"{footer} • Part {idx}/{total}"
        embed.set_footer(text=footer)
        embeds.append(embed)
    return embeds


def _render_report(
    *,
    summary: AuditResult,
    raid_role_name: str,
    wanderer_role_name: str,
    dry_run: bool = True,
) -> discord.Embed:
    return _render_report_embeds(
        summary=summary,
        raid_role_name=raid_role_name,
        wanderer_role_name=wanderer_role_name,
        dry_run=dry_run,
    )[0]


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
    actor_normalized = (actor or "").strip().lower()
    scheduled_actors = {"scheduled", "background", "cron", "startup", "ready"}
    try:
        fusion_cleanup_summaries = await fusion_role_cleanup.load_unreported_role_cleanup_summaries()
    except Exception as exc:
        log.warning(
            "failed to load fusion role cleanup summaries",
            extra={
                "component": "role_audit",
                "operation": "load_fusion_role_cleanup_summaries",
                "exception_type": type(exc).__name__,
                "exception_message": str(exc),
            },
            exc_info=True,
        )
        fusion_cleanup_summaries = fusion_role_cleanup.get_recent_role_cleanup_summaries()
    aggregated = AuditResult(
        checked=0,
        auto_fixed_strays=[],
        auto_fixed_wanderers=[],
        wanderers_with_clans=[],
        visitors_no_ticket=[],
        visitors_closed_only=[],
        visitors_extra_roles=[],
        members_only_everyone=[],
        fusion_role_cleanup=fusion_cleanup_summaries,
        proposed_role_mutations=[],
        action_roles_removed=[],
        action_roles_added=[],
        action_users_kicked=[],
        action_failed_or_skipped=[],
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
        aggregated.members_only_everyone.extend(result.members_only_everyone or [])
        aggregated.proposed_role_mutations.extend(result.proposed_role_mutations or [])
        aggregated.action_roles_removed.extend(result.action_roles_removed or [])
        aggregated.action_roles_added.extend(result.action_roles_added or [])
        aggregated.action_users_kicked.extend(result.action_users_kicked or [])
        aggregated.action_failed_or_skipped.extend(result.action_failed_or_skipped or [])

    if aggregated.checked == 0:
        return False, "no-members"

    proposed_adds = len(aggregated.auto_fixed_strays or [])
    proposed_removes = len(aggregated.auto_fixed_strays or []) + len(aggregated.auto_fixed_wanderers or [])
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

    embeds = _render_report_embeds(
        summary=aggregated,
        raid_role_name=raid_role_name,
        wanderer_role_name=wanderer_role_name,
        dry_run=dry_run or actor_normalized in scheduled_actors,
    )

    try:
        for embed in embeds:
            await channel.send(embed=embed)
    except Exception as exc:
        log.warning("failed to send role audit report", exc_info=True)
        return False, f"send:{type(exc).__name__}"
    if actor_normalized == "scheduled":
        try:
            await fusion_role_cleanup.mark_role_cleanup_summaries_reported(fusion_cleanup_summaries)
        except Exception as exc:
            log.warning(
                "failed to mark fusion role cleanup summaries reported",
                extra={
                    "component": "role_audit",
                    "operation": "mark_fusion_role_cleanup_summaries_reported",
                    "exception_type": type(exc).__name__,
                    "exception_message": str(exc),
                },
                exc_info=True,
            )

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
        members_only_everyone=[],
        proposed_role_mutations=[],
        action_roles_removed=[],
        action_roles_added=[],
        action_users_kicked=[],
        action_failed_or_skipped=[],
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
