from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from modules.community.live_arena import knockout
from modules.community.live_arena.knockout import KnockoutService
from modules.community.live_arena.qualification import MATCH_HEADERS, ROUND_HEADERS

TID = "T1"
NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def run(awaitable):
    return asyncio.run(awaitable)


def blank(headers):
    return {header: "" for header in headers}


def closed_qf():
    row = blank(ROUND_HEADERS)
    row.update(
        tournament_id=TID,
        round_id=f"{TID}-QF",
        round_name="Quarterfinals",
        round_stage="quarterfinal",
        round_number="4",
        status="closed",
    )
    return row


def final_qf(number, a, b, winner):
    row = blank(MATCH_HEADERS)
    row.update(
        tournament_id=TID,
        round_id=f"{TID}-QF",
        match_id=f"{TID}-QF-M{number:02d}",
        match_number=str(number),
        player_a_discord_user_id=str(a),
        player_a_display_name=f"P{a}",
        player_b_discord_user_id=str(b),
        player_b_display_name=f"P{b}",
        status="finalized",
        final_result_type="played",
        final_score_a="2" if str(winner) == str(a) else "0",
        final_score_b="2" if str(winner) == str(b) else "0",
        final_winner_discord_user_id=str(winner),
    )
    return row


class RegistrationRepo:
    async def initialize(self):
        pass

    async def append_audit(self, _row):
        pass


class QualificationRepo:
    def __init__(self):
        self.r = [closed_qf()]
        self.m = [
            final_qf(1, 1, 8, 1),
            final_qf(2, 4, 5, 4),
            final_qf(3, 2, 7, 2),
            final_qf(4, 3, 6, 3),
        ]

    async def initialize(self):
        pass

    async def rounds(self):
        return deepcopy(self.r)

    async def matches(self):
        return deepcopy(self.m)

    async def persist_state(self, rounds, matches, *, previous_rounds, previous_matches):
        self.r = deepcopy(rounds)
        self.m = deepcopy(matches)

    async def persist_rounds(self, rounds, *, previous_rounds):
        self.r = deepcopy(rounds)


def test_semifinal_preview_regenerates_if_qf_winner_changes_before_open(monkeypatch):
    reg = RegistrationRepo()
    qrepo = QualificationRepo()
    service = KnockoutService(
        "sheet",
        registration_repository=reg,
        qualification_repository=qrepo,
        clock=lambda: NOW,
    )
    monkeypatch.setattr(
        knockout,
        "load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": TID}),
    )
    run(service.initialize())

    first = run(service.generate_next_preview("system", "semifinal"))
    assert first.matches[0]["player_a_discord_user_id"] == "1"

    # A permitted upstream correction before the semifinal opens changes QF1 winner.
    qf1 = next(row for row in qrepo.m if row["match_id"] == f"{TID}-QF-M01")
    qf1["final_winner_discord_user_id"] = "8"
    qf1["final_score_a"] = "0"
    qf1["final_score_b"] = "2"

    refreshed = run(service.refresh_preview_if_stale("system", "semifinal"))

    assert refreshed is not None
    assert refreshed.matches[0]["player_a_discord_user_id"] == "8"
    assert refreshed.matches[0]["player_b_discord_user_id"] == "4"
