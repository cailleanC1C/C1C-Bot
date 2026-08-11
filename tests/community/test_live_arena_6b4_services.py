from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime

from modules.community.live_arena import extension_confirmation, withdrawal_atomic
from modules.community.live_arena.competition_operations import CompetitionOperationsService
from modules.community.live_arena.qualification import MATCH_HEADERS, ROUND_HEADERS

TID = "T-6B4"
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def run(awaitable):
    return asyncio.run(awaitable)


def blank(headers):
    return {header: "" for header in headers}


class RegistrationRepo:
    def __init__(self, participants=None):
        self.p = deepcopy(participants or [])
        self.audit = []

    async def initialize(self):
        pass

    async def participants(self):
        return deepcopy(self.p)

    async def tournaments(self):
        return [{"tournament_id": TID, "status": "active"}]

    async def persist_participants(self, rows, *, previous_participants):
        self.p = deepcopy(rows)

    async def append_audit(self, row):
        self.audit.append(deepcopy(row))


class QualificationRepo:
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

    async def persist_matches(self, matches, *, previous_matches):
        self.m = deepcopy(matches)


def round_row(round_id: str, stage: str, number: int, status: str, deadline: str):
    row = blank(ROUND_HEADERS)
    row.update(
        tournament_id=TID,
        round_id=round_id,
        round_name=round_id,
        round_stage=stage,
        round_number=str(number),
        status=status,
        deadline_at_utc=deadline,
    )
    return row


def match_row(round_id: str, match_id: str, a: str, b: str, status: str):
    row = blank(MATCH_HEADERS)
    row.update(
        tournament_id=TID,
        round_id=round_id,
        match_id=match_id,
        player_a_discord_user_id=a,
        player_a_display_name=f"P{a}",
        player_b_discord_user_id=b,
        player_b_display_name=f"P{b}",
        status=status,
        extension_count="0",
    )
    return row


def test_round_extension_recalculates_confirmation_window(monkeypatch):
    rid = f"{TID}-Q2"
    rounds = [round_row(rid, "qualification", 2, "open", "2026-08-20T18:00:00Z")]
    match = match_row(rid, f"{rid}-M01", "1", "2", "pending_confirmation")
    match.update(
        reported_at_utc="2026-08-20T12:00:00Z",
        confirm_due_at_utc="2026-08-20T18:00:00Z",
        deadline_at_utc="2026-08-20T18:00:00Z",
    )
    reg = RegistrationRepo()
    qrepo = QualificationRepo(rounds, [match])
    service = CompetitionOperationsService(
        "sheet-extension-6b4",
        registration_repository=reg,
        qualification_repository=qrepo,
        clock=lambda: NOW,
    )

    async def config(_sheet_id):
        return {"ACTIVE_TOURNAMENT_ID": TID}

    scheduled = []
    monkeypatch.setattr(extension_confirmation, "load_config", config)
    monkeypatch.setattr(
        extension_confirmation,
        "schedule_match_finalization",
        lambda sheet_id, match_id, due: scheduled.append((sheet_id, match_id, due)),
    )

    run(
        service.extend_round(
            "42",
            rid,
            "2026-08-21T18:00:00Z",
            reason="players need more time",
        )
    )

    saved = qrepo.m[0]
    assert saved["deadline_at_utc"] == "2026-08-21T18:00:00Z"
    assert saved["confirm_due_at_utc"] == "2026-08-21T12:00:00Z"
    assert saved["extension_count"] == "1"
    assert scheduled == [
        ("sheet-extension-6b4", f"{rid}-M01", "2026-08-21T12:00:00Z")
    ]


def test_active_withdrawal_preserves_final_result_forfeits_open_and_removes_preview(monkeypatch):
    q1 = f"{TID}-Q1"
    q2 = f"{TID}-Q2"
    q3 = f"{TID}-Q3"
    rounds = [
        round_row(q1, "qualification", 1, "closed", "2026-08-18T12:00:00Z"),
        round_row(q2, "qualification", 2, "open", "2026-08-25T12:00:00Z"),
        round_row(q3, "qualification", 3, "preview", ""),
    ]
    completed = match_row(q1, f"{q1}-M01", "1", "9", "finalized")
    completed.update(
        final_result_type="played",
        final_score_a="2",
        final_score_b="1",
        final_winner_discord_user_id="1",
    )
    active = match_row(q2, f"{q2}-M01", "1", "2", "published")
    preview = match_row(q3, f"{q3}-M01", "1", "3", "preview")
    reg = RegistrationRepo(
        [
            {
                "tournament_id": TID,
                "discord_user_id": uid,
                "status": "confirmed",
                "withdrawn_at_utc": "",
                "withdrawal_reason": "",
                "updated_at_utc": "",
            }
            for uid in ("1", "2", "3", "9")
        ]
    )
    qrepo = QualificationRepo(rounds, [completed, active, preview])
    service = CompetitionOperationsService(
        "sheet-withdrawal-6b4",
        registration_repository=reg,
        qualification_repository=qrepo,
        clock=lambda: NOW,
    )

    async def config(_sheet_id):
        return {"ACTIVE_TOURNAMENT_ID": TID}

    monkeypatch.setattr(withdrawal_atomic, "load_config", config)

    run(service.withdraw_active_participant("42", "1", reason="cannot continue"))

    participant = next(row for row in reg.p if row["discord_user_id"] == "1")
    assert participant["status"] == "withdrawn"
    assert participant["withdrawal_reason"] == "cannot continue"

    saved_completed = next(row for row in qrepo.m if row["match_id"] == f"{q1}-M01")
    assert saved_completed["status"] == "finalized"
    assert saved_completed["final_winner_discord_user_id"] == "1"

    saved_active = next(row for row in qrepo.m if row["match_id"] == f"{q2}-M01")
    assert saved_active["status"] == "forfeit"
    assert saved_active["final_winner_discord_user_id"] == "2"

    assert all(row["round_id"] != q3 for row in qrepo.r)
    assert all(row["round_id"] != q3 for row in qrepo.m)
