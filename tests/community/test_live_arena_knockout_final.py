from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from modules.community.live_arena import competition_resolution
from modules.community.live_arena.competition_resolution import CompetitionResolutionService
from modules.community.live_arena.qualification import MATCH_HEADERS, ROUND_HEADERS

TID = "T1"
NOW = datetime(2026, 8, 30, 18, 0, tzinfo=UTC)


def run(awaitable):
    return asyncio.run(awaitable)


def blank(headers):
    return {header: "" for header in headers}


class RegistrationRepo:
    def __init__(self):
        self.audit = []

    async def initialize(self):
        pass

    async def append_audit(self, row):
        self.audit.append(deepcopy(row))


class QualificationRepo:
    def __init__(self):
        round_row = blank(ROUND_HEADERS)
        round_row.update(
            tournament_id=TID,
            round_id=f"{TID}-F",
            round_name="Final",
            round_stage="final",
            round_number="6",
            status="open",
            deadline_at_utc=(NOW + timedelta(days=6)).isoformat().replace("+00:00", "Z"),
        )
        match = blank(MATCH_HEADERS)
        match.update(
            tournament_id=TID,
            round_id=f"{TID}-F",
            match_id=f"{TID}-F-M01",
            match_number="1",
            player_a_discord_user_id="1",
            player_a_display_name="P1",
            player_b_discord_user_id="2",
            player_b_display_name="P2",
            status="published",
            deadline_at_utc=round_row["deadline_at_utc"],
        )
        self.r = [round_row]
        self.m = [match]

    async def initialize(self):
        pass

    async def rounds(self):
        return deepcopy(self.r)

    async def matches(self):
        return deepcopy(self.m)

    async def persist_matches(self, matches, *, previous_matches):
        self.m = deepcopy(matches)

    async def persist_state(self, rounds, matches, *, previous_rounds, previous_matches):
        self.r = deepcopy(rounds)
        self.m = deepcopy(matches)


def make_service(monkeypatch):
    reg = RegistrationRepo()
    qrepo = QualificationRepo()
    service = CompetitionResolutionService(
        "sheet",
        registration_repository=reg,
        qualification_repository=qrepo,
        clock=lambda: NOW,
    )
    monkeypatch.setattr(
        competition_resolution,
        "load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": TID}),
    )
    return service, reg, qrepo


def test_final_report_enters_explicit_organizer_queue(monkeypatch):
    service, _, qrepo = make_service(monkeypatch)
    run(service.initialize())

    updated = run(
        service.report_result(
            "1",
            f"{TID}-F-M01",
            3,
            2,
            screenshot_present=True,
        )
    )

    assert updated["status"] == "organizer_review"
    assert updated["confirm_due_at_utc"] == ""
    assert qrepo.m[0]["status"] == "organizer_review"
    reviewable = run(service.reviewable_matches())
    assert [row["match_id"] for row in reviewable] == [f"{TID}-F-M01"]


def test_organizer_accept_finalizes_final_report(monkeypatch):
    service, _, _ = make_service(monkeypatch)
    run(service.initialize())
    run(service.report_result("1", f"{TID}-F-M01", 4, 1, screenshot_present=True))

    resolved = run(
        service.resolve_match(
            "99",
            f"{TID}-F-M01",
            "accept",
            reason="Final result verified from the posted evidence",
        )
    )

    assert resolved["status"] == "finalized"
    assert resolved["final_score_a"] == "4"
    assert resolved["final_score_b"] == "1"
    assert resolved["final_winner_discord_user_id"] == "1"
