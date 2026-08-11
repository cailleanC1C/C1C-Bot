from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from modules.community.live_arena import service


def run(awaitable):
    return asyncio.run(awaitable)


def test_active_tournament_lookup_ignores_other_historical_and_draft_rows():
    config = [
        list(service.CONFIG_HEADERS),
        ["ACTIVE_TOURNAMENT_ID", "arena-2", ""],
        ["TOURNAMENTS_TAB", "TOURNAMENTS", ""],
        ["ELIGIBLE_CLANS_TAB", "ELIGIBLE_CLANS", ""],
        ["AVAILABILITY_SLOTS_TAB", "AVAILABILITY_SLOTS", ""],
        ["ORGANIZER_ROLE_ID", "1", ""],
        ["PARTICIPANTS_TAB", "PARTICIPANTS", ""],
        ["PARTICIPANT_AVAILABILITY_TAB", "PARTICIPANT_AVAILABILITY", ""],
        ["AUDIT_LOG_TAB", "AUDIT_LOG", ""],
        ["TOURNAMENT_DISCORD_RESOURCES_TAB", "RESOURCES", ""],
    ]
    base = {
        "eligibility_scope": "open",
        "min_participants": "8",
        "max_participants": "16",
        "signup_opens_at_utc": "",
        "signup_closes_at_utc": "",
        "notes": "",
        "created_at_utc": "",
        "completed_at_utc": "",
        "archived_at_utc": "",
        "timezone": "UTC",
    }

    def tournament_row(tid, name, short, status):
        values = dict(
            base,
            tournament_id=tid,
            tournament_name=name,
            tournament_short_name=short,
            status=status,
        )
        return [values[h] for h in service.TOURNAMENT_HEADERS]

    matrices = {
        "CONFIG": config,
        "TOURNAMENTS": [
            list(service.TOURNAMENT_HEADERS),
            tournament_row("arena-1", "Arena I", "I", "archived"),
            tournament_row("arena-2", "Arena II", "II", "signup_open"),
            tournament_row("arena-3", "Arena III", "III", "draft"),
        ],
        "ELIGIBLE_CLANS": [list(service.ELIGIBLE_CLAN_HEADERS)],
        "AVAILABILITY_SLOTS": [
            list(service.AVAILABILITY_SLOT_HEADERS),
            ["slot", "Monday", "00:00", "02:00", "TRUE", "1", "00-02"],
        ],
    }

    async def fetch(_sheet, tab):
        return matrices[tab]

    with patch(
        "modules.community.live_arena.service.afetch_values",
        AsyncMock(side_effect=fetch),
    ):
        snapshot = run(service.load_tournament_snapshot("sheet"))

    assert snapshot.tournament_id == "arena-2"
    assert snapshot.tournament_name == "Arena II"
    assert snapshot.tournament_short_name == "II"
