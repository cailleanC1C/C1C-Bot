from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from modules.community.live_arena import competition
from modules.community.live_arena.competition import (
    LiveArenaCompetitionService,
    calculate_qualification_standings,
)
from modules.community.live_arena.qualification import MATCH_HEADERS, ROUND_HEADERS
from modules.community.live_arena.registration import RegistrationError

TID = "LA-2026-TRIAL-01"
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def run(awaitable):
    return asyncio.run(awaitable)


def blank(headers):
    return {header: "" for header in headers}


def round_row(status="active", number="1"):
    row = blank(ROUND_HEADERS)
    row.update(
        tournament_id=TID,
        round_id=f"{TID}-Q{number}",
        round_name=f"Qualification Round {number}",
        round_stage="qualification",
        round_number=number,
        status=status,
        opens_at_utc=NOW.isoformat().replace("+00:00", "Z"),
        deadline_at_utc=(NOW + timedelta(days=6)).isoformat().replace("+00:00", "Z"),
    )
    return row


def match_row(number=1, a="1", b="2", status="published", round_number="1"):
    row = blank(MATCH_HEADERS)
    row.update(
        tournament_id=TID,
        round_id=f"{TID}-Q{round_number}",
        match_id=f"{TID}-Q{round_number}-M{number:02d}",
        match_number=str(number),
        player_a_discord_user_id=a,
        player_a_display_name=f"P{a}",
        player_b_discord_user_id=b,
        player_b_display_name=f"P{b}",
        status=status,
        deadline_at_utc=(NOW + timedelta(days=6)).isoformat().replace("+00:00", "Z"),
        thread_id=str(1000 + number),
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
    def __init__(self, rounds=None, matches=None):
        self.r = deepcopy(rounds or [])
        self.m = deepcopy(matches or [])

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


def make_service(monkeypatch, *, rounds=None, matches=None, now=NOW):
    registration_repo = MemoryRegistrationRepository()
    qrepo = MemoryQualificationRepository(rounds or [round_row()], matches or [match_row()])
    service = LiveArenaCompetitionService(
        "sheet",
        registration_repository=registration_repo,
        qualification_repository=qrepo,
        clock=lambda: now,
    )
    monkeypatch.setattr(
        competition,
        "load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": TID}),
    )
    return service, registration_repo, qrepo


def test_report_result_requires_screenshot_and_match_player(monkeypatch):
    service, _, _ = make_service(monkeypatch)
    run(service.initialize())

    with pytest.raises(RegistrationError, match="screenshot"):
        run(service.report_result("1", f"{TID}-Q1-M01", 2, 1, screenshot_present=False))

    with pytest.raises(RegistrationError, match="Only a player"):
        run(service.report_result("99", f"{TID}-Q1-M01", 2, 1, screenshot_present=True))


def test_report_result_sets_bounded_confirmation_window(monkeypatch):
    deadline = NOW + timedelta(hours=6)
    row = round_row()
    row["deadline_at_utc"] = deadline.isoformat().replace("+00:00", "Z")
    service, registration_repo, qrepo = make_service(monkeypatch, rounds=[row])
    run(service.initialize())

    updated = run(
        service.report_result("1", f"{TID}-Q1-M01", 2, 1, screenshot_present=True)
    )

    assert updated["status"] == "pending_confirmation"
    assert updated["reported_by_discord_user_id"] == "1"
    assert updated["reported_score_a"] == "2"
    assert updated["reported_score_b"] == "1"
    assert updated["confirm_due_at_utc"] == deadline.isoformat().replace("+00:00", "Z")
    assert qrepo.m[0]["status"] == "pending_confirmation"
    assert registration_repo.audit[-1]["event_type"] == "match_result_reported"


def test_non_reporting_opponent_can_dispute(monkeypatch):
    pending = match_row(status="pending_confirmation")
    pending.update(
        reported_by_discord_user_id="1",
        reported_score_a="2",
        reported_score_b="1",
        reported_at_utc=NOW.isoformat().replace("+00:00", "Z"),
        confirm_due_at_utc=(NOW + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
    )
    service, registration_repo, qrepo = make_service(monkeypatch, matches=[pending])
    run(service.initialize())

    updated = run(service.dispute_result("2", pending["match_id"]))

    assert updated["status"] == "disputed"
    assert updated["disputed_by_discord_user_id"] == "2"
    assert qrepo.m[0]["status"] == "disputed"
    assert registration_repo.audit[-1]["event_type"] == "match_result_disputed"


def test_reporter_cannot_dispute_own_result(monkeypatch):
    pending = match_row(status="pending_confirmation")
    pending.update(
        reported_by_discord_user_id="1",
        confirm_due_at_utc=(NOW + timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
    )
    service, _, _ = make_service(monkeypatch, matches=[pending])
    run(service.initialize())

    with pytest.raises(RegistrationError, match="non-reporting opponent"):
        run(service.dispute_result("1", pending["match_id"]))


def test_due_result_auto_finalizes_and_marks_round_ready(monkeypatch):
    pending = match_row(status="pending_confirmation")
    pending.update(
        reported_by_discord_user_id="1",
        reported_score_a="2",
        reported_score_b="0",
        reported_at_utc=(NOW - timedelta(hours=24)).isoformat().replace("+00:00", "Z"),
        confirm_due_at_utc=NOW.isoformat().replace("+00:00", "Z"),
    )
    service, registration_repo, qrepo = make_service(monkeypatch, matches=[pending])
    run(service.initialize())

    result = run(service.finalize_match_if_due(pending["match_id"]))

    assert result["status"] == "finalized"
    assert result["final_result_type"] == "played"
    assert result["final_score_a"] == "2"
    assert result["final_score_b"] == "0"
    assert result["final_winner_discord_user_id"] == "1"
    assert qrepo.r[0]["status"] == "ready_to_close"
    assert registration_repo.audit[-1]["event_type"] == "match_result_auto_finalized"


def test_disputed_result_never_auto_finalizes(monkeypatch):
    disputed = match_row(status="disputed")
    disputed.update(
        reported_by_discord_user_id="1",
        reported_score_a="2",
        reported_score_b="0",
        confirm_due_at_utc=NOW.isoformat().replace("+00:00", "Z"),
    )
    service, _, qrepo = make_service(monkeypatch, matches=[disputed])
    run(service.initialize())

    assert run(service.finalize_match_if_due(disputed["match_id"])) is None
    assert qrepo.m[0]["status"] == "disputed"


def test_round_close_requires_all_matches_final_and_is_audited(monkeypatch):
    ready = round_row(status="ready_to_close")
    finalized = match_row(status="finalized")
    finalized.update(
        final_result_type="played",
        final_score_a="2",
        final_score_b="1",
        final_winner_discord_user_id="1",
    )
    service, registration_repo, qrepo = make_service(
        monkeypatch, rounds=[ready], matches=[finalized]
    )
    run(service.initialize())

    closed = run(service.close_round("900", ready["round_id"]))

    assert closed["status"] == "closed"
    assert closed["completed_at_utc"]
    assert qrepo.r[0]["status"] == "closed"
    assert registration_repo.audit[-1]["event_type"] == "round_closed"


def test_closed_round_can_reopen_only_before_next_round_opens(monkeypatch):
    q1 = round_row(status="closed", number="1")
    q2 = round_row(status="preview", number="2")
    service, _, qrepo = make_service(monkeypatch, rounds=[q1, q2])
    run(service.initialize())

    reopened = run(service.reopen_round("900", q1["round_id"]))
    assert reopened["status"] == "correction_in_progress"
    assert qrepo.r[0]["completed_at_utc"] == ""

    qrepo.r[0] = deepcopy(q1)
    qrepo.r[1]["status"] = "active"
    with pytest.raises(RegistrationError, match="competitively final"):
        run(service.reopen_round("900", q1["round_id"]))


def finalized_match(number, a, b, score_a, score_b, *, round_number="1"):
    row = match_row(number, a, b, status="finalized", round_number=round_number)
    row.update(
        final_result_type="played",
        final_score_a=str(score_a),
        final_score_b=str(score_b),
        final_winner_discord_user_id=a if score_a > score_b else b,
        finalized_at_utc=NOW.isoformat().replace("+00:00", "Z"),
    )
    return row


def test_standings_use_wins_game_diff_sos_then_head_to_head():
    rows = [
        finalized_match(1, "1", "2", 2, 1, round_number="1"),
        finalized_match(2, "3", "4", 2, 0, round_number="1"),
        finalized_match(1, "1", "3", 0, 2, round_number="2"),
        finalized_match(2, "2", "4", 2, 0, round_number="2"),
        finalized_match(1, "1", "4", 2, 0, round_number="3"),
        finalized_match(2, "2", "3", 2, 1, round_number="3"),
    ]

    standings = calculate_qualification_standings(rows, TID)

    assert [entry.discord_user_id for entry in standings] == ["3", "2", "1", "4"]
    assert standings[0].match_wins == 2
    assert standings[0].game_differential == 3
    assert standings[1].match_record == "2-1"
    assert standings[2].match_record == "2-1"
    assert standings[1].game_differential > standings[2].game_differential
    assert standings[1].rank == 2
    assert standings[2].rank == 3


def test_unresolved_lower_tie_uses_shared_competition_rank():
    rows = [
        finalized_match(1, "1", "3", 2, 0),
        finalized_match(2, "2", "4", 2, 0),
    ]

    standings = calculate_qualification_standings(rows, TID)
    by_id = {entry.discord_user_id: entry for entry in standings}

    assert by_id["1"].rank == 1
    assert by_id["2"].rank == 1
    assert by_id["1"].tied is True
    assert by_id["2"].tied is True
    assert by_id["3"].rank == 3
    assert by_id["4"].rank == 3
