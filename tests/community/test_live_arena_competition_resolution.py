from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from modules.community.live_arena import competition_resolution
from modules.community.live_arena.competition_resolution import CompetitionResolutionService
from modules.community.live_arena.qualification import MATCH_HEADERS, ROUND_HEADERS
from modules.community.live_arena.registration import RegistrationError

TID = "LA-2026-TRIAL-01"
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def run(awaitable):
    return asyncio.run(awaitable)


def blank(headers):
    return {header: "" for header in headers}


def round_row(status="active", number="1", *, deadline=None):
    row = blank(ROUND_HEADERS)
    row.update(
        tournament_id=TID,
        round_id=f"{TID}-Q{number}",
        round_name=f"Qualification Round {number}",
        round_stage="qualification",
        round_number=number,
        status=status,
        opens_at_utc=(NOW - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        deadline_at_utc=(deadline or NOW + timedelta(days=5)).isoformat().replace("+00:00", "Z"),
    )
    return row


def match_row(status="published"):
    row = blank(MATCH_HEADERS)
    row.update(
        tournament_id=TID,
        round_id=f"{TID}-Q1",
        match_id=f"{TID}-Q1-M01",
        match_number="1",
        player_a_discord_user_id="1",
        player_a_display_name="P1",
        player_b_discord_user_id="2",
        player_b_display_name="P2",
        status=status,
        deadline_at_utc=(NOW + timedelta(days=5)).isoformat().replace("+00:00", "Z"),
        thread_id="1001",
    )
    return row


class MemoryRegistrationRepository:
    def __init__(self):
        self.audit = []

    async def initialize(self):
        pass

    async def append_audit(self, row):
        self.audit.append(deepcopy(row))


class MemoryQualificationRepository:
    def __init__(self, rounds, matches):
        self.r = deepcopy(rounds)
        self.m = deepcopy(matches)

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

    async def persist_matches(self, matches, *, previous_matches):
        self.m = deepcopy(matches)


def service(monkeypatch, rounds, matches, *, now=NOW):
    registration = MemoryRegistrationRepository()
    qualification = MemoryQualificationRepository(rounds, matches)
    result = CompetitionResolutionService(
        "sheet",
        registration_repository=registration,
        qualification_repository=qualification,
        clock=lambda: now,
    )
    monkeypatch.setattr(
        competition_resolution,
        "load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": TID}),
    )
    return result, registration, qualification


def test_late_report_is_saved_for_review_instead_of_finalized(monkeypatch):
    expired = round_row(deadline=NOW - timedelta(minutes=1))
    result, registration, qualification = service(
        monkeypatch, [expired], [match_row()]
    )

    updated = run(
        result.report_result("1", f"{TID}-Q1-M01", 2, 1, screenshot_present=True)
    )

    assert updated["status"] == "late_review"
    assert updated["confirm_due_at_utc"] == ""
    assert qualification.m[0]["final_result_type"] == ""
    assert registration.audit[-1]["event_type"] == "match_result_late_reported"


def test_normal_report_keeps_bounded_objection_window(monkeypatch):
    deadline = NOW + timedelta(hours=3)
    result, _, _ = service(
        monkeypatch, [round_row(deadline=deadline)], [match_row()]
    )

    updated = run(
        result.report_result("1", f"{TID}-Q1-M01", 2, 0, screenshot_present=True)
    )

    assert updated["status"] == "pending_confirmation"
    assert updated["confirm_due_at_utc"] == deadline.isoformat().replace("+00:00", "Z")


def test_organizer_ruling_requires_reason(monkeypatch):
    disputed = match_row("disputed")
    disputed.update(reported_score_a="2", reported_score_b="1", reported_by_discord_user_id="1")
    result, _, _ = service(monkeypatch, [round_row()], [disputed])

    with pytest.raises(RegistrationError, match="reason"):
        run(result.resolve_match("900", disputed["match_id"], "accept", reason=""))


def test_accept_disputed_result_finalizes_and_marks_round_ready(monkeypatch):
    disputed = match_row("disputed")
    disputed.update(
        reported_score_a="2",
        reported_score_b="1",
        reported_by_discord_user_id="1",
    )
    result, registration, qualification = service(
        monkeypatch, [round_row()], [disputed]
    )

    updated = run(
        result.resolve_match(
            "900", disputed["match_id"], "accept", reason="Screenshot confirms score"
        )
    )

    assert updated["status"] == "finalized"
    assert updated["final_score_a"] == "2"
    assert updated["final_score_b"] == "1"
    assert qualification.r[0]["status"] == "ready_to_close"
    assert "Screenshot confirms score" in updated["notes"]
    assert registration.audit[-1]["event_type"] == "match_result_organizer_resolved"


def test_correct_disputed_result_uses_organizer_score(monkeypatch):
    disputed = match_row("disputed")
    disputed.update(reported_score_a="2", reported_score_b="0")
    result, _, _ = service(monkeypatch, [round_row()], [disputed])

    updated = run(
        result.resolve_match(
            "900",
            disputed["match_id"],
            "correct",
            reason="Typed score was reversed",
            score_a=1,
            score_b=2,
        )
    )

    assert updated["status"] == "finalized"
    assert updated["final_score_a"] == "1"
    assert updated["final_score_b"] == "2"
    assert updated["final_winner_discord_user_id"] == "2"


def test_replay_resets_result_without_erasing_audit_note(monkeypatch):
    disputed = match_row("disputed")
    disputed.update(
        reported_by_discord_user_id="1",
        reported_score_a="2",
        reported_score_b="1",
        disputed_by_discord_user_id="2",
    )
    result, _, qualification = service(monkeypatch, [round_row()], [disputed])

    updated = run(
        result.resolve_match(
            "900", disputed["match_id"], "replay", reason="Evidence is inconclusive"
        )
    )

    assert updated["status"] == "published"
    assert updated["reported_score_a"] == ""
    assert updated["final_score_a"] == ""
    assert "Evidence is inconclusive" in updated["notes"]
    assert qualification.r[0]["status"] == "active"


def test_forfeit_and_double_forfeit_are_terminal_without_fake_scores(monkeypatch):
    late = match_row("late_review")
    late.update(reported_score_a="2", reported_score_b="1")
    result, _, _ = service(monkeypatch, [round_row()], [late])

    forfeited = run(
        result.resolve_match(
            "900", late["match_id"], "forfeit_a", reason="Player A did not attend"
        )
    )
    assert forfeited["status"] == "forfeit"
    assert forfeited["final_winner_discord_user_id"] == "2"
    assert forfeited["final_score_a"] == ""
    assert forfeited["final_score_b"] == ""

    second = match_row("late_review")
    result, _, _ = service(monkeypatch, [round_row()], [second])
    double = run(
        result.resolve_match(
            "900", second["match_id"], "double_forfeit", reason="Neither player attended"
        )
    )
    assert double["status"] == "double_forfeit"
    assert double["final_winner_discord_user_id"] == ""
    assert double["final_score_a"] == ""


def test_correction_round_exposes_terminal_results_for_review(monkeypatch):
    final = match_row("finalized")
    final.update(
        final_result_type="played",
        final_score_a="2",
        final_score_b="1",
        final_winner_discord_user_id="1",
    )
    result, _, _ = service(
        monkeypatch, [round_row(status="correction_in_progress")], [final]
    )

    rows = run(result.reviewable_matches())

    assert [row["match_id"] for row in rows] == [final["match_id"]]


def test_replay_can_be_reported_while_round_is_in_correction(monkeypatch):
    replay = match_row("published")
    result, _, _ = service(
        monkeypatch, [round_row(status="correction_in_progress")], [replay]
    )

    updated = run(
        result.report_result("2", replay["match_id"], 0, 2, screenshot_present=True)
    )

    assert updated["status"] == "pending_confirmation"
    assert updated["reported_score_a"] == "0"
    assert updated["reported_score_b"] == "2"
