from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import AsyncMock

from modules.community.live_arena import swiss
from modules.community.live_arena.qualification import MATCH_HEADERS, ROUND_HEADERS
from modules.community.live_arena.swiss import SwissQualificationService

TID = "LA-2026-TRIAL-01"
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def run(awaitable):
    return asyncio.run(awaitable)


def blank(headers):
    return {header: "" for header in headers}


def q1_round():
    row = blank(ROUND_HEADERS)
    row.update(
        tournament_id=TID,
        round_id=f"{TID}-Q1",
        round_name="Qualification Round 1",
        round_stage="qualification",
        round_number="1",
        status="closed",
    )
    return row


def q1_match(number, a, b, score_a, score_b):
    row = blank(MATCH_HEADERS)
    row.update(
        tournament_id=TID,
        round_id=f"{TID}-Q1",
        match_id=f"{TID}-Q1-M{number:02d}",
        match_number=str(number),
        player_a_discord_user_id=str(a),
        player_a_display_name=f"P{a}",
        player_b_discord_user_id=str(b),
        player_b_display_name=f"P{b}",
        status="finalized",
        final_result_type="played",
        final_score_a=str(score_a),
        final_score_b=str(score_b),
        final_winner_discord_user_id=str(a if score_a > score_b else b),
    )
    return row


SLOTS = [
    {
        "slot_id": "MON-00",
        "enabled": "TRUE",
        "sort_order": "1",
    }
]


class RegistrationRepo:
    def __init__(self):
        self.audit = []

    async def initialize(self):
        pass

    async def participants(self):
        return [
            {
                "tournament_id": TID,
                "discord_user_id": str(uid),
                "display_name_at_signup": f"P{uid}",
                "status": "confirmed",
            }
            for uid in (1, 2, 3, 4)
        ]

    async def availability(self):
        return []

    async def append_audit(self, row):
        self.audit.append(deepcopy(row))


class QualificationRepo:
    def __init__(self):
        self.r = [q1_round()]
        self.m = [
            q1_match(1, 1, 4, 2, 0),
            q1_match(2, 2, 3, 2, 1),
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


def make_service(monkeypatch):
    reg = RegistrationRepo()
    qrepo = QualificationRepo()
    service = SwissQualificationService(
        "sheet",
        registration_repository=reg,
        qualification_repository=qrepo,
        clock=lambda: NOW,
    )
    service.context = AsyncMock(return_value=(None, None, None, SLOTS))
    monkeypatch.setattr(
        swiss,
        "load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": TID}),
    )
    return service, reg, qrepo


def test_q2_preview_approve_publish_lifecycle(monkeypatch):
    service, reg, qrepo = make_service(monkeypatch)
    run(service.initialize())

    preview = run(service.generate_preview("42", 2))
    assert preview.status == "preview"
    assert len(preview.matches) == 2
    assert all(row["status"] == "preview" for row in preview.matches)
    q1_pairs = {frozenset(("1", "4")), frozenset(("2", "3"))}
    q2_pairs = {
        frozenset(
            (
                row["player_a_discord_user_id"],
                row["player_b_discord_user_id"],
            )
        )
        for row in preview.matches
    }
    assert not (q1_pairs & q2_pairs)

    approved = run(service.approve_preview("42", 2))
    assert approved.status == "approved"

    published = run(service.publish_approved("42", 2))
    assert published.status == "open"
    assert published.round_row["opens_at_utc"]
    assert published.round_row["deadline_at_utc"]
    assert all(row["status"] == "published" for row in published.matches)
    assert reg.audit[-1]["event_type"] == "swiss_draw_published"
    assert any(row["round_id"] == f"{TID}-Q2" for row in qrepo.r)
