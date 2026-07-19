"""Config-driven RealmWalker access auditing and repair helpers."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Sequence

import discord

from modules.common.embeds import get_embed_colour
from shared.sheets import recruitment

log = logging.getLogger("c1c.housekeeping.realmwalker")

ACCESS_ROLE_KEY = "REALMWALKER_ACCESS_ROLE_ID"
GAME_ROLES_KEY = "REALMWALKER_GAME_ROLE_IDS"
FIX_REASON = "Housekeeping RealmWalker access audit"


@dataclass(frozen=True, slots=True)
class RealmWalkerConfig:
    access_role_id: int
    game_role_ids: frozenset[int]


@dataclass(frozen=True, slots=True)
class RealmWalkerIssue:
    member: discord.Member
    matched_game_roles: tuple[discord.Role, ...]


@dataclass(slots=True)
class RealmWalkerAuditResult:
    checked: int = 0
    issues: list[RealmWalkerIssue] = field(default_factory=list)
    fixed: list[RealmWalkerIssue] = field(default_factory=list)
    skipped: list[RealmWalkerIssue] = field(default_factory=list)
    failures: list[tuple[RealmWalkerIssue, str]] = field(default_factory=list)


def _parse_role_id(value: str | None) -> int | None:
    try:
        role_id = int((value or "").strip())
    except (TypeError, ValueError):
        return None
    return role_id if role_id > 0 else None


async def resolve_config() -> tuple[RealmWalkerConfig | None, str | None]:
    """Resolve and validate RealmWalker role IDs from the existing Config tab."""
    access_value = await recruitment.get_config_value_async(ACCESS_ROLE_KEY, None)
    games_value = await recruitment.get_config_value_async(GAME_ROLES_KEY, None)
    access_id = _parse_role_id(access_value)
    game_ids: set[int] = set()
    invalid_games: list[str] = []
    for item in (games_value or "").split(","):
        item = item.strip()
        if not item:
            continue
        role_id = _parse_role_id(item)
        if role_id is None:
            invalid_games.append(item)
        else:
            game_ids.add(role_id)
    if access_id is None or not game_ids or invalid_games:
        details = []
        if access_id is None:
            details.append(f"{ACCESS_ROLE_KEY} is missing or invalid")
        if not game_ids:
            details.append(f"{GAME_ROLES_KEY} has no valid role IDs")
        if invalid_games:
            details.append(f"{GAME_ROLES_KEY} contains invalid values")
        return None, "; ".join(details)
    return RealmWalkerConfig(access_id, frozenset(game_ids)), None


def scan_members(
    members: Sequence[discord.Member], config: RealmWalkerConfig
) -> RealmWalkerAuditResult:
    """Find humans with configured game roles but without the access role."""
    result = RealmWalkerAuditResult()
    for member in members:
        if getattr(member, "bot", False):
            continue
        result.checked += 1
        roles = tuple(
            role
            for role in getattr(member, "roles", ())
            if getattr(role, "id", None) in config.game_role_ids
        )
        role_ids = {getattr(role, "id", None) for role in getattr(member, "roles", ())}
        if roles and config.access_role_id not in role_ids:
            result.issues.append(RealmWalkerIssue(member, roles))
    return result


async def fix_issues(
    issues: Sequence[RealmWalkerIssue], access_role: discord.Role | None
) -> RealmWalkerAuditResult:
    """Add the access role independently for each issue, retaining all existing roles."""
    result = RealmWalkerAuditResult(issues=list(issues))
    for issue in issues:
        if access_role is None:
            result.skipped.append(issue)
            continue
        try:
            await issue.member.add_roles(access_role, reason=FIX_REASON)
        except discord.Forbidden:
            result.failures.append((issue, "missing permission or role hierarchy"))
        except discord.HTTPException as exc:
            result.failures.append((issue, f"Discord API error: {exc}"))
        else:
            result.fixed.append(issue)
    return result


def format_issue(issue: RealmWalkerIssue) -> str:
    member = issue.member
    mention = getattr(member, "mention", None) or str(getattr(member, "id", "unknown"))
    name = (
        getattr(member, "display_name", None)
        or getattr(member, "name", None)
        or "unknown"
    )
    roles = ", ".join(
        getattr(role, "name", "unknown") for role in issue.matched_game_roles
    )
    return f"• {mention} – {name} – game roles: {roles}"


def build_embeds(
    result: RealmWalkerAuditResult, *, fixing: bool = False, error: str | None = None
) -> list[discord.Embed]:
    """Render bounded, mention-safe manual command output."""
    if error:
        sections = ["**Configuration warning**", error]
    elif fixing:
        sections = [
            "**Fixed members**",
            *([format_issue(item) for item in result.fixed] or ["• None"]),
            "",
            "**Skipped members**",
            *([format_issue(item) for item in result.skipped] or ["• None"]),
            "",
            "**Failures**",
            *(
                [f"{format_issue(item)} – {reason}" for item, reason in result.failures]
                or ["• None"]
            ),
        ]
    elif result.issues:
        sections = ["**Missing RealmWalker access**", *map(format_issue, result.issues)]
    else:
        sections = ["✅ No RealmWalker access issues found."]

    chunks: list[str] = []
    current = ""
    for line in sections:
        candidate = f"{current}\n{line}".strip()
        if current and len(candidate) > 3800:
            chunks.append(current)
            current = line
        else:
            current = candidate
    chunks.append(current)
    embeds = []
    for index, chunk in enumerate(chunks, 1):
        title = "🧹 RealmWalker Access Audit"
        if len(chunks) > 1:
            title += f" ({index}/{len(chunks)})"
        embed = discord.Embed(
            title=title, description=chunk, colour=get_embed_colour("admin")
        )
        affected = len(result.issues)
        footer = f"Checked: {result.checked} members • Affected: {affected}"
        if fixing:
            footer += f" • Fixed: {len(result.fixed)} • Skipped: {len(result.skipped)} • Failed: {len(result.failures)}"
        embed.set_footer(text=footer)
        embeds.append(embed)
    return embeds


__all__ = [
    "RealmWalkerAuditResult",
    "RealmWalkerConfig",
    "RealmWalkerIssue",
    "build_embeds",
    "fix_issues",
    "format_issue",
    "resolve_config",
    "scan_members",
]
