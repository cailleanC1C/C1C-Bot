"""Use the master clan lookup for Create Next Tournament eligibility.

``ELIGIBLE_CLANS`` is tournament-instance history.  It must not double as the master
list of clans available to a future tournament, otherwise the next-tournament wizard
can only offer clans that happened to appear in an older tournament.

The workbook CONFIG key ``CLAN_TAB`` points at a compact master lookup containing
``clan_name`` and ``clan_tag``.  This boundary uses that lookup for the Discord picker,
resolves the chosen tags to configured Discord clan roles, and then lets the existing
motto-aware create transaction persist normal tournament-scoped ``ELIGIBLE_CLANS``
rows.
"""

from __future__ import annotations

import contextvars
import logging
import re
import unicodedata
from dataclasses import dataclass, replace

from shared import config as runtime_config

log = logging.getLogger("c1c.community.live_arena.next_tournament_clan_lookup")
_installed = False
_CLAN_LOOKUP_HEADERS = ("clan_name", "clan_tag")
_injected_eligible_rows: contextvars.ContextVar[tuple[dict[str, object], ...]] = (
    contextvars.ContextVar("live_arena_next_tournament_clans", default=())
)


def _norm(value: object) -> str:
    """Normalize ordinary and stylized Discord role names for stable matching."""

    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^a-z0-9]+", "", text)


def _config_value(rows, key: str, *, required: bool = True) -> str:
    from modules.community.live_arena import next_tournament

    matches = [
        row for row in rows if next_tournament._text(row.get("Key")) == key
    ]
    if len(matches) != 1:
        qualifier = "exactly" if required else "at most"
        raise next_tournament.LiveArenaConfigError(
            f"CONFIG: key {key} must occur {qualifier} once"
        )
    value = next_tournament._text(matches[0].get("Value"))
    if required and not value:
        raise next_tournament.LiveArenaConfigError(
            f"CONFIG: missing required value {key}"
        )
    return value


async def _load_clan_lookup(sheet_id: str):
    """Load and validate the CONFIG-driven master clan lookup."""

    from modules.community.live_arena import next_tournament

    config_matrix = await next_tournament.afetch_values(
        sheet_id, next_tournament.CONFIG_TAB
    )
    config_rows = next_tournament._rows(
        config_matrix or [],
        next_tournament.CONFIG_HEADERS,
        next_tournament.CONFIG_TAB,
    )
    tab = _config_value(config_rows, "CLAN_TAB")
    matrix = await next_tournament.afetch_values(sheet_id, tab)
    rows = next_tournament._rows(matrix or [], _CLAN_LOOKUP_HEADERS, tab)

    if not rows:
        raise next_tournament.RegistrationError(
            f"{tab}: no clan definitions are available"
        )

    seen_tags: set[str] = set()
    result = []
    for row_number, row in enumerate(rows, start=2):
        name = next_tournament._text(row.get("clan_name"))
        tag = next_tournament._text(row.get("clan_tag"))
        if not name or not tag:
            raise next_tournament.LiveArenaConfigError(
                f"{tab}: clan_name and clan_tag are required on row {row_number}"
            )
        key = tag.casefold()
        if key in seen_tags:
            raise next_tournament.LiveArenaConfigError(
                f"{tab}: duplicate clan_tag: {tag}"
            )
        seen_tags.add(key)
        result.append((name, tag))

    if len(result) > 25:
        raise next_tournament.RegistrationError(
            "more than 25 clans exist in CLAN_TAB; Create Next Tournament needs a paged selector before it can be used"
        )
    return result


def _configured_clan_roles(guild):
    configured = {int(value) for value in runtime_config.get_clan_role_ids()}
    if not configured:
        raise RuntimeError("CLAN_ROLE_IDS is empty; clan Discord roles cannot be resolved")
    if guild is None:
        raise RuntimeError("Discord guild is unavailable; clan roles cannot be resolved")

    roles = []
    getter = getattr(guild, "get_role", None)
    if callable(getter):
        for role_id in configured:
            role = getter(role_id)
            if role is not None:
                roles.append(role)
    else:
        roles = [
            role
            for role in getattr(guild, "roles", [])
            if getattr(role, "id", None) in configured
        ]

    if not roles:
        raise RuntimeError("none of the configured CLAN_ROLE_IDS exist in this server")
    return roles


def _role_score(role_name: object, *, clan_name: str, clan_tag: str) -> int:
    role = _norm(role_name)
    name = _norm(clan_name)
    tag = _norm(clan_tag)
    if not role:
        return 0
    if role == name or role == tag:
        return 100
    if name and tag and name in role and tag in role:
        return 90
    if name and name in role:
        return 80
    if tag and tag in role:
        return 70
    return 0


def _resolve_discord_role_id(guild, option) -> str:
    """Resolve one lookup clan to exactly one configured Discord clan role."""

    # Compatibility: older tests/callers already provide a concrete numeric role ID.
    existing = str(getattr(option, "discord_role_id", "") or "").strip()
    if existing.isdigit():
        return existing

    scored = []
    for role in _configured_clan_roles(guild):
        score = _role_score(
            getattr(role, "name", ""),
            clan_name=option.clan_name,
            clan_tag=option.clan_tag,
        )
        if score:
            scored.append((score, role))

    if not scored:
        raise RuntimeError(
            f"No configured Discord clan role matches {option.clan_tag} · {option.clan_name}"
        )
    best = max(score for score, _role in scored)
    winners = [role for score, role in scored if score == best]
    if len(winners) != 1:
        labels = ", ".join(
            f"{getattr(role, 'name', 'unknown')} ({getattr(role, 'id', 'unknown')})"
            for role in winners
        )
        raise RuntimeError(
            f"Multiple configured Discord clan roles match {option.clan_tag} · {option.clan_name}: {labels}"
        )
    return str(winners[0].id)


async def _clan_options_from_lookup(self):
    from modules.community.live_arena import next_tournament

    rows = await _load_clan_lookup(self.sheet_id)
    # ``discord_role_id`` intentionally carries the clan tag as the select value until
    # the interaction callback can resolve it against the actual guild role list.
    return [
        next_tournament.ClanOption(
            clan_tag=tag,
            clan_name=name,
            discord_role_id=tag,
            active_current=False,
        )
        for name, tag in rows
    ]


async def _wizard_clan_select(self, interaction):
    """Turn selected lookup tags into concrete Discord role IDs, then review."""

    from modules.community.live_arena import next_tournament
    from modules.community.live_arena import next_tournament_wizard_ux as wizard

    await interaction.response.defer()
    try:
        by_value = {item.discord_role_id: item for item in self.clan_options}
        selected = [by_value[value] for value in self.values]
        role_ids = tuple(
            _resolve_discord_role_id(getattr(interaction, "guild", None), item)
            for item in selected
        )
        tags = tuple(item.clan_tag for item in selected)
        draft = replace(
            self.draft,
            eligible_role_ids=role_ids,
            eligible_clan_tags=tags,
        )
        templates = await next_tournament._load_next_messages(
            self.manager.sheet_id, {"next_tournament_wizard_review"}
        )
        embed = templates["next_tournament_wizard_review"].embed(
            **wizard._details_values(draft),
            signup_opens=next_tournament.discord_timestamp(
                draft.signup_opens_at_utc
            ),
            signup_closes=next_tournament.discord_timestamp(
                draft.signup_closes_at_utc
            ),
            eligible_clans=", ".join(item.label for item in selected),
        )
        await wizard._edit_after_defer(
            interaction,
            embed=embed,
            view=next_tournament.ConfirmCreateNextTournamentView(
                self.manager, draft
            ),
        )
    except Exception as exc:
        log.exception(
            "Live Arena next-tournament clan lookup selection failed • sheet=%s • error=%s: %s",
            getattr(self.manager, "sheet_id", "unknown"),
            type(exc).__name__,
            exc,
        )
        await wizard._send_error(interaction, exc)


def _rows_with_injected_clans(original_rows):
    """Expose selected master-lookup clans to the existing create transaction only."""

    from modules.community.live_arena import next_tournament

    def wrapped(matrix, expected, tab):
        rows = original_rows(matrix, expected, tab)
        injected = _injected_eligible_rows.get()
        if injected and tuple(expected) == tuple(next_tournament.ELIGIBLE_CLAN_HEADERS):
            # Append so a newly-resolved role wins if an old tournament happened to use
            # the same role ID with stale metadata.
            return [*rows, *[dict(row) for row in injected]]
        return rows

    wrapped._live_arena_clan_lookup_injection = True
    return wrapped


async def _create_with_lookup(self, actor_id: str, draft):
    """Feed selected lookup metadata into the existing motto-aware create path."""

    from modules.community.live_arena import next_tournament

    original = getattr(type(self), "_clan_lookup_original_create", None)
    if original is None:
        raise RuntimeError("Create Next Tournament clan lookup boundary is not initialized")

    tags = tuple(getattr(draft, "eligible_clan_tags", ()) or ())
    if not tags:
        return await original(self, actor_id, draft)
    if len(tags) != len(draft.eligible_role_ids):
        raise next_tournament.RegistrationError(
            "selected clan metadata is inconsistent; restart tournament setup"
        )

    lookup = await _load_clan_lookup(self.sheet_id)
    by_tag = {tag.casefold(): (name, tag) for name, tag in lookup}
    injected = []
    for selected_tag, role_id in zip(tags, draft.eligible_role_ids):
        match = by_tag.get(str(selected_tag).casefold())
        if match is None:
            raise next_tournament.RegistrationError(
                f"selected clan no longer exists in CLAN_TAB: {selected_tag}"
            )
        name, canonical_tag = match
        injected.append(
            {
                "tournament_id": "__lookup__",
                "clan_tag": canonical_tag,
                "clan_name": name,
                "discord_role_id": str(role_id),
                "active": "TRUE",
                "notes": "Resolved from CLAN_TAB for Create Next Tournament.",
            }
        )

    token = _injected_eligible_rows.set(tuple(injected))
    try:
        return await original(self, actor_id, draft)
    finally:
        _injected_eligible_rows.reset(token)


def install() -> None:
    """Install after motto support and the visible wizard UX boundary."""

    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import next_tournament
    from modules.community.live_arena import next_tournament_wizard_ux as wizard

    original_draft = next_tournament.NextTournamentDraft
    if not hasattr(original_draft, "eligible_clan_tags"):

        @dataclass(frozen=True)
        class ClanLookupNextTournamentDraft(original_draft):
            eligible_clan_tags: tuple[str, ...] = ()

        next_tournament.NextTournamentDraft = ClanLookupNextTournamentDraft

    next_tournament.NextTournamentService.clan_options = _clan_options_from_lookup

    # New lookup options must never inherit selections from the archived tournament.
    # The wizard class still accepts a defaults argument for compatibility, but this
    # source marks every option as not-current so Discord renders an explicit choice.
    wizard.TournamentClanSelect.callback = _wizard_clan_select

    current_rows = next_tournament._rows
    if not getattr(current_rows, "_live_arena_clan_lookup_injection", False):
        next_tournament._rows = _rows_with_injected_clans(current_rows)

    service_cls = next_tournament.NextTournamentService
    if not hasattr(service_cls, "_clan_lookup_original_create"):
        service_cls._clan_lookup_original_create = service_cls.create
        service_cls.create = _create_with_lookup
