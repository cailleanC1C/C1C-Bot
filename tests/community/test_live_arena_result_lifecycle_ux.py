from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.community.live_arena import result_lifecycle_ux as ux
from modules.community.live_arena.competition import calculate_qualification_standings
from modules.community.live_arena.competition_resolution import CompetitionResolutionService
from modules.community.live_arena.qualification import MATCH_HEADERS, ROUND_HEADERS
from modules.community.live_arena.registration import RegistrationError

TID = "LA-2026-TRIAL-01"
NOW = datetime(2026, 8, 16, 12, 0, tzinfo=UTC)


def run(awaitable):
    return asyncio.run(awaitable)


def blank(headers):
    return {header: "" for header in headers}


def round_row(number="2", status="active"):
    row = blank(ROUND_HEADERS)
    row.update(
        tournament_id=TID,
        round_id=f"{TID}-Q{number}",
        round_name=f"Qualification Round {number}",
        round_stage="qualification",
        round_number=number,
        status=status,
        deadline_at_utc=(NOW + timedelta(days=5)).isoformat().replace("+00:00", "Z"),
    )
    return row


def pending_match(*, reporter="1", a="1", b="2"):
    row = blank(MATCH_HEADERS)
    row.update(
        tournament_id=TID,
        round_id=f"{TID}-Q2",
        match_id=f"{TID}-Q2-M01",
        match_number="1",
        player_a_discord_user_id=a,
        player_a_display_name=f"P{a}",
        player_b_discord_user_id=b,
        player_b_display_name=f"P{b}",
        status="pending_confirmation",
        reported_by_discord_user_id=reporter,
        reported_score_a="2",
        reported_score_b="1",
        reported_at_utc=NOW.isoformat().replace("+00:00", "Z"),
        confirm_due_at_utc=(NOW + timedelta(hours=12)).isoformat().replace("+00:00", "Z"),
        thread_id="1001",
    )
    return row


class RegistrationRepo:
    def __init__(self):
        self.audit = []

    async def initialize(self):
        return None

    async def append_audit(self, row):
        self.audit.append(deepcopy(row))


class QualificationRepo:
    def __init__(self, rounds, matches):
        self.r = deepcopy(rounds)
        self.m = deepcopy(matches)

    async def initialize(self):
        return None

    async def rounds(self):
        return deepcopy(self.r)

    async def matches(self):
        return deepcopy(self.m)

    async def persist_state(self, rounds, matches, *, previous_rounds, previous_matches):
        self.r = deepcopy(rounds)
        self.m = deepcopy(matches)

    async def persist_matches(self, matches, *, previous_matches):
        self.m = deepcopy(matches)

    async def persist_rounds(self, rounds, *, previous_rounds):
        self.r = deepcopy(rounds)


def service(monkeypatch, *, match=None):
    registration = RegistrationRepo()
    qualification = QualificationRepo([round_row()], [match or pending_match()])
    result = CompetitionResolutionService(
        "sheet",
        registration_repository=registration,
        qualification_repository=qualification,
        clock=lambda: NOW,
    )
    monkeypatch.setattr(
        ux,
        "load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": TID}),
    )
    return result, registration, qualification


def _copy(key, title, description=""):
    return ux.CopyTemplate(key, title, description, 0x1A73E8)


def prime_lifecycle_copy(sheet_id="sheet"):
    ux._COPY[sheet_id] = {
        "button_confirm_result": _copy("button_confirm_result", "Confirm Result"),
        "button_dispute_result": _copy("button_dispute_result", "Dispute Result"),
        "result_confirmed_player": _copy(
            "result_confirmed_player",
            "Result confirmed",
            "{participant_mention} confirmed **{score}**. The result is final immediately.",
        ),
        "result_confirmed_staff": _copy(
            "result_confirmed_staff",
            "Result confirmed by organizer",
            "{staff_mention} confirmed **{score}** on behalf of {participant_mention}. The result is final immediately.",
        ),
        "result_finalized_expired": _copy(
            "result_finalized_expired",
            "Result finalized",
            "The objection window closed with no dispute. **{score}** is final.",
        ),
        "result_finalized_organizer": _copy(
            "result_finalized_organizer",
            "Result resolved",
            "Organizer review is complete. **{score}** is final.",
        ),
        "round_result_finalized_confirmed": _copy(
            "round_result_finalized_confirmed",
            "✅ Result final: **{score_a}-{score_b}** · confirmed",
        ),
        "round_result_finalized_expired": _copy(
            "round_result_finalized_expired",
            "✅ Result final: **{score_a}-{score_b}** · objection window expired",
        ),
        "round_result_finalized_organizer": _copy(
            "round_result_finalized_organizer",
            "✅ Result final: **{score_a}-{score_b}** · organizer resolved",
        ),
        "round_standings_heading_live": _copy(
            "round_standings_heading_live", "🏆 Current qualification standings"
        ),
        "round_standings_heading_after": _copy(
            "round_standings_heading_after",
            "🏆 Qualification standings after Round {round_number}",
        ),
        "round_standings_heading_final": _copy(
            "round_standings_heading_final", "🏆 Final qualification standings"
        ),
        "round_standings_context_live": _copy(
            "round_standings_context_live",
            "",
            "*Includes finalized results through Round {round_number}.*\n\n",
        ),
    }
    ux._ACTIVE_SHEET_ID = sheet_id


def test_non_reporting_opponent_can_confirm_immediately(monkeypatch):
    result, registration, qualification = service(monkeypatch)
    run(result.initialize())

    updated = run(result.confirm_result("2", f"{TID}-Q2-M01"))

    assert updated["status"] == "finalized"
    assert updated["confirmed_by_discord_user_id"] == "2"
    assert updated["final_score_a"] == "2"
    assert updated["final_score_b"] == "1"
    assert qualification.r[0]["status"] == "ready_to_close"
    assert registration.audit[-1]["event_type"] == "match_result_confirmed"


def test_reporter_cannot_confirm_own_report(monkeypatch):
    result, _, _ = service(monkeypatch)
    run(result.initialize())

    with pytest.raises(RegistrationError, match="non-reporting opponent"):
        run(result.confirm_result("1", f"{TID}-Q2-M01"))


def test_organizer_proxy_resolves_to_non_reporting_participant(monkeypatch):
    interaction = SimpleNamespace(user=SimpleNamespace(id=900, roles=[]))
    monkeypatch.setattr(ux, "_is_organizer", AsyncMock(return_value=True))

    participant, proxied = run(
        ux._represented_opponent(interaction, "sheet", pending_match())
    )

    assert participant == "2"
    assert proxied is True


def test_reporter_cannot_use_organizer_proxy_to_self_confirm(monkeypatch):
    interaction = SimpleNamespace(user=SimpleNamespace(id=1, roles=[]))
    monkeypatch.setattr(ux, "_is_organizer", AsyncMock(return_value=True))

    with pytest.raises(RegistrationError, match="cannot confirm or dispute"):
        run(ux._represented_opponent(interaction, "sheet", pending_match()))


def finalized(number, a, b, sa, sb, round_number, **extra):
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
        status="finalized",
        final_result_type="played",
        final_score_a=str(sa),
        final_score_b=str(sb),
        final_winner_discord_user_id=a if sa > sb else b,
        **extra,
    )
    return row


def test_qualification_standings_accumulate_finalized_q1_and_q2_only():
    q1 = finalized(1, "1", "2", 2, 1, "1")
    q2 = finalized(1, "1", "3", 0, 2, "2")
    pending_q2 = pending_match(reporter="2", a="2", b="3")
    pending_q2["match_id"] = f"{TID}-Q2-M02"
    pending_q2["match_number"] = "2"

    standings = calculate_qualification_standings([q1, q2, pending_q2], TID)
    by_id = {entry.discord_user_id: entry for entry in standings}

    assert by_id["1"].match_record == "1-1"
    assert by_id["2"].match_record == "0-1"
    assert by_id["3"].match_record == "1-0"


def test_victory_ledger_finalized_reason_is_state_specific():
    prime_lifecycle_copy()
    from modules.community.live_arena import round_overview

    confirmed = finalized(
        1,
        "1",
        "2",
        2,
        1,
        "2",
        confirmed_by_discord_user_id="2",
        finalized_by_discord_user_id="2",
    )
    expired = finalized(
        2,
        "3",
        "4",
        2,
        0,
        "2",
        finalized_by_discord_user_id="system",
    )
    organizer = finalized(
        3,
        "5",
        "6",
        1,
        2,
        "2",
        disputed_at_utc=NOW.isoformat().replace("+00:00", "Z"),
        finalized_by_discord_user_id="900",
    )

    assert "confirmed" in round_overview._result_line({}, confirmed)
    assert "objection window expired" in round_overview._result_line({}, expired)
    assert "organizer resolved" in round_overview._result_line({}, organizer)


def test_result_decision_view_has_confirm_and_dispute_but_starter_view_has_no_dispute():
    prime_lifecycle_copy()
    from modules.community.live_arena import result_views

    decision_ids = {item.custom_id for item in ux.ResultDecisionView("sheet").children}
    assert decision_ids == {
        "live_arena:match:confirm_reported_result",
        "live_arena:match:dispute_reported_result",
    }

    starter_ids = {item.custom_id for item in result_views.MatchResultView("sheet").children}
    assert "live_arena:match:dispute_result" not in starter_ids
