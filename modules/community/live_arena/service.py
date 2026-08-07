"""Load and validate the read-only Live Arena workbook contract."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Sequence

from shared.sheets.async_core import afetch_values

CONFIG_TAB = "CONFIG"
CONFIG_HEADERS = ("Key", "Value", "Notes / clear name")
CONFIG_KEYS = (
    "ACTIVE_TOURNAMENT_ID",
    "TOURNAMENTS_TAB",
    "ELIGIBLE_CLANS_TAB",
    "AVAILABILITY_SLOTS_TAB",
    "ORGANIZER_ROLE_ID",
    "PARTICIPANTS_TAB",
    "PARTICIPANT_AVAILABILITY_TAB",
    "AUDIT_LOG_TAB",
)
TOURNAMENT_HEADERS = (
    "tournament_id",
    "tournament_name",
    "status",
    "eligibility_scope",
    "min_participants",
    "max_participants",
    "signup_opens_at_utc",
    "signup_closes_at_utc",
    "notes",
)
ELIGIBLE_CLAN_HEADERS = (
    "tournament_id",
    "clan_tag",
    "clan_name",
    "discord_role_id",
    "active",
    "notes",
)
AVAILABILITY_SLOT_HEADERS = (
    "slot_id",
    "weekday_utc",
    "start_time_utc",
    "end_time_utc",
    "enabled",
    "sort_order",
    "display_label",
)
WEEKDAYS = {
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
}


class LiveArenaConfigError(RuntimeError):
    """The configured Live Arena workbook does not match the PR1 contract."""


@dataclass(frozen=True)
class TournamentSnapshot:
    tournament_id: str
    tournament_name: str
    status: str
    eligibility_scope: str
    min_participants: int
    max_participants: int
    signup_opens_at_utc: str
    signup_closes_at_utc: str
    active_eligible_clans: int
    enabled_availability_windows: int
    organizer_role_id: int


def _text(value: object) -> str:
    return str(value or "").strip()


def _rows(
    matrix: Sequence[Sequence[object]], expected: tuple[str, ...], tab: str
) -> list[dict[str, object]]:
    if not matrix:
        raise LiveArenaConfigError(f"{tab}: required header row missing")
    headers = tuple(_text(value) for value in matrix[0])
    missing = [header for header in expected if header not in headers]
    if missing:
        raise LiveArenaConfigError(
            f"{tab}: required header missing: {', '.join(missing)}"
        )
    unexpected = [header for header in headers if header not in expected]
    if unexpected:
        raise LiveArenaConfigError(f"{tab}: unexpected header: {', '.join(unexpected)}")
    return [
        dict(zip(headers, row))
        for row in matrix[1:]
        if any(_text(value) for value in row)
    ]


def _enabled(value: object) -> bool:
    return _text(value).lower() in {"1", "true", "yes", "on"}


def _required_int(value: object, label: str) -> int:
    try:
        parsed = int(_text(value))
    except (TypeError, ValueError) as exc:
        raise LiveArenaConfigError(f"{label}: expected an integer") from exc
    if parsed <= 0:
        raise LiveArenaConfigError(f"{label}: expected a positive integer")
    return parsed


async def load_config(sheet_id: str) -> dict[str, str]:
    """Read the literal CONFIG tab and return the Live Arena routing keys."""

    matrix = await afetch_values(sheet_id, CONFIG_TAB)
    rows = _rows(matrix or [], CONFIG_HEADERS, CONFIG_TAB)
    values = {_text(row["Key"]): _text(row["Value"]) for row in rows}
    for key in CONFIG_KEYS:
        if not values.get(key):
            raise LiveArenaConfigError(f"CONFIG: missing required key {key}")
    return {key: values[key] for key in CONFIG_KEYS}


async def load_tournament_snapshot(sheet_id: str) -> TournamentSnapshot:
    """Load the active tournament and read-only supporting row counts."""

    config = await load_config(sheet_id)
    tournament_tab = config["TOURNAMENTS_TAB"]
    clans_tab = config["ELIGIBLE_CLANS_TAB"]
    slots_tab = config["AVAILABILITY_SLOTS_TAB"]
    tournaments_matrix, clans_matrix, slots_matrix = await asyncio.gather(
        afetch_values(sheet_id, tournament_tab),
        afetch_values(sheet_id, clans_tab),
        afetch_values(sheet_id, slots_tab),
    )
    tournaments = _rows(tournaments_matrix or [], TOURNAMENT_HEADERS, tournament_tab)
    clans = _rows(clans_matrix or [], ELIGIBLE_CLAN_HEADERS, clans_tab)
    slots = _rows(slots_matrix or [], AVAILABILITY_SLOT_HEADERS, slots_tab)

    active_id = config["ACTIVE_TOURNAMENT_ID"]
    tournament = next(
        (row for row in tournaments if _text(row["tournament_id"]) == active_id), None
    )
    if tournament is None:
        raise LiveArenaConfigError(
            f"{tournament_tab}: active tournament not found: {active_id}"
        )

    for row in slots:
        weekday = _text(row["weekday_utc"])
        if weekday not in WEEKDAYS:
            raise LiveArenaConfigError(
                f"{slots_tab}: invalid weekday_utc: {weekday or '(blank)'}"
            )

    return TournamentSnapshot(
        tournament_id=active_id,
        tournament_name=_text(tournament["tournament_name"]),
        status=_text(tournament["status"]),
        eligibility_scope=_text(tournament["eligibility_scope"]),
        min_participants=_required_int(
            tournament["min_participants"], f"{tournament_tab}.min_participants"
        ),
        max_participants=_required_int(
            tournament["max_participants"], f"{tournament_tab}.max_participants"
        ),
        signup_opens_at_utc=_text(tournament["signup_opens_at_utc"]),
        signup_closes_at_utc=_text(tournament["signup_closes_at_utc"]),
        active_eligible_clans=sum(
            1
            for row in clans
            if _text(row["tournament_id"]) == active_id and _enabled(row["active"])
        ),
        enabled_availability_windows=sum(
            1 for row in slots if _enabled(row["enabled"])
        ),
        organizer_role_id=_required_int(
            config["ORGANIZER_ROLE_ID"], "CONFIG.ORGANIZER_ROLE_ID"
        ),
    )
