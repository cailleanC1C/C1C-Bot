from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from modules.community.live_arena import knockout
from modules.community.live_arena.competition import StandingEntry
from modules.community.live_arena.knockout import KnockoutService
from modules.community.live_arena.qualification import MATCH_HEADERS, ROUND_HEADERS
from modules.community.live_arena.registration import RegistrationError

TID = "LA-2026-TRIAL-01"
NOW = datetime(2026, 8, 20, 12, 0, tzinfo=UTC)


def run(awaitable):
    return asyncio.run(awaitable)


def blank(headers):
    return {header: "" for header in headers}


def round_row(round_id: str, stage: str, number: int, status: str):
    row = blank(ROUND_HEADERS)
    row.update(
        tournament_id=TID,
        round_id=round_id,
        round_name=round_id.rsplit("-", 1)[-1],
        round_stage=stage,
        round_number=str(number),
        status=status,
    )
    return row


def finalized(round_id: str, number: int, a: str, b: str, winner: str, *, score="2-0"):
    sa, sb = score.split("-")
    if winner == b:
        sa, sb = sb, sa
    row = blank(MATCH_HEADERS)
    row.update(
        tournament_id=TID,
        round_id=round_id,
        match_id=f"{round_id}-M{number:02d}",
        match_number=str(number),
        player_a_discord_user_id=a,
        player_a_display_name=f"P{a}",
        player_b_discord_user_id=b,
        player_b_display_name=f"P{b}",
        status="finalized",
        final_result_type="played",
        final_score_a=sa,
        final_score_b=sb,
        final_winner_discord_user_id=winner,
    )
    return row


def standings(*, tied=False):
    rows = []
    for index in range(1, 9):
        rows.append(
            StandingEntry(
                discord_user_id=str(index),
                display_name=f"P{index}",
                match_wins=3 if index <= 2 else 2 if index <= 5 else 1,
                match_losses=0 if index <= 2 else 1 if index <= 5 else 2,
                game_wins=6,
                game_losses=index - 1,
                game_differential=7 - index,
                strength_of_opponents=20 - index,
                rank=index if not (tied and index in {7, 8}) else 7,
                tied=tied and index in {7, 8},
            )
        )
    return rows


class RegistrationRepo:
    def __init__(self):
        self.audit = []

    async def initialize(self):
        pass

    async def append_audit(self, row):
        self.audit.append(deepcopy(row))

    async def participants(self):
        return []


class QualificationRepo:
    def __init__(self, rounds=None, matches=None):
        self.r = deepcopy(rounds or [])
        self.m = deepcopy(matches or [])
        self.config = {
            "MATCH_FORUM_CHANNEL_ID": "1",
            "ROUND_OVERVIEW_CHANNEL_ID": "2",
        }

    async def initialize(self):
        pass

    async def rounds(self):
        return deepcopy(self.r)

    async def matches(self):
        return deepcopy(self.m)

    async def persist_rounds(self, rounds, *, previous_rounds):
        self.r = deepcopy(rounds)

    async def persist_matches(self, matches, *, previous_matches):
        self.m = deepcopy(matches)

    async def persist_state(self, rounds, matches, *, previous_rounds, previous_matches):
        self.r = deepcopy(rounds)
        self.m = deepcopy(matches)


def make_service(monkeypatch, *, rounds=None, matches=None, table=None):
    reg = RegistrationRepo()
    qrepo = QualificationRepo(
        rounds=rounds or [round_row(f"{TID}-Q3", "qualification", 3, "closed")],
        matches=matches or [],
    )
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
    monkeypatch.setattr(
        knockout,
        "calculate_qualification_standings",
        lambda _matches, _tid: list(table or standings()),
    )
    return service, reg, qrepo


def test_freeze_top8_uses_canonical_order_and_is_immutable(monkeypatch):
    service, reg, qrepo = make_service(monkeypatch)
    run(service.initialize())

    seeds = run(service.freeze_top8("42"))

    assert [seed["discord_user_id"] for seed in seeds] == [str(i) for i in range(1, 9)]
    assert [seed["seed"] for seed in seeds] == list(range(1, 9))
    seed_row = next(row for row in qrepo.r if row["round_stage"] == "top8_seeding")
    assert seed_row["status"] == "frozen"
    assert seed_row["approved_by_discord_user_id"] == "42"
    assert reg.audit[-1]["event_type"] == "top8_seeds_frozen"

    # Calling freeze again never reorders or rewrites the approved seeds.
    again = run(service.freeze_top8("99"))
    assert again == seeds
    assert len([row for row in qrepo.r if row["round_stage"] == "top8_seeding"]) == 1


def test_freeze_top8_rejects_competitive_tie(monkeypatch):
    service, _, _ = make_service(monkeypatch, table=standings(tied=True))
    run(service.initialize())

    with pytest.raises(RegistrationError, match="Qualification tiebreak required") as exc:
        run(service.freeze_top8("42"))
    assert "BO3" not in str(exc.value)


def test_quarterfinal_preview_has_fixed_seed_slots(monkeypatch):
    service, _, _ = make_service(monkeypatch)
    run(service.initialize())
    run(service.freeze_top8("42"))

    preview = run(service.generate_quarterfinal_preview("42"))

    pairs = [
        (row["player_a_discord_user_id"], row["player_b_discord_user_id"])
        for row in preview.matches
    ]
    assert pairs == [("1", "8"), ("4", "5"), ("2", "7"), ("3", "6")]
    assert preview.status == "preview"
    assert all(row["status"] == "preview" for row in preview.matches)


def test_knockout_preview_opens_with_six_day_window(monkeypatch):
    service, reg, _ = make_service(monkeypatch)
    run(service.initialize())
    run(service.freeze_top8("42"))
    run(service.generate_quarterfinal_preview("42"))

    opened = run(service.approve_and_open("42", "quarterfinal"))

    assert opened.status == "open"
    assert opened.round_row["approved_by_discord_user_id"] == "42"
    assert opened.round_row["opens_at_utc"] == "2026-08-20T12:00:00Z"
    assert opened.round_row["deadline_at_utc"] == "2026-08-26T12:00:00Z"
    assert all(row["status"] == "published" for row in opened.matches)
    assert reg.audit[-1]["event_type"] == "knockout_round_opened"


def test_semifinal_slots_follow_fixed_qf_bracket(monkeypatch):
    qf_round = round_row(f"{TID}-QF", "quarterfinal", 4, "closed")
    qf = [
        finalized(f"{TID}-QF", 1, "1", "8", "1"),
        finalized(f"{TID}-QF", 2, "4", "5", "5"),
        finalized(f"{TID}-QF", 3, "2", "7", "2"),
        finalized(f"{TID}-QF", 4, "3", "6", "6"),
    ]
    service, _, _ = make_service(monkeypatch, rounds=[qf_round], matches=qf)
    run(service.initialize())

    preview = run(service.generate_next_preview("system", "semifinal"))

    pairs = [
        (row["player_a_discord_user_id"], row["player_b_discord_user_id"])
        for row in preview.matches
    ]
    assert pairs == [("1", "5"), ("2", "6")]


def test_final_is_generated_from_semifinal_winners(monkeypatch):
    sf_round = round_row(f"{TID}-SF", "semifinal", 5, "closed")
    sf = [
        finalized(f"{TID}-SF", 1, "1", "5", "5"),
        finalized(f"{TID}-SF", 2, "2", "6", "2"),
    ]
    service, _, _ = make_service(monkeypatch, rounds=[sf_round], matches=sf)
    run(service.initialize())

    preview = run(service.generate_next_preview("system", "final"))

    assert len(preview.matches) == 1
    assert preview.matches[0]["player_a_discord_user_id"] == "5"
    assert preview.matches[0]["player_b_discord_user_id"] == "2"
    assert preview.round_row["round_stage"] == "final"


def test_completion_requires_closed_final_with_champion(monkeypatch):
    final_round = round_row(f"{TID}-F", "final", 6, "closed")
    final = [finalized(f"{TID}-F", 1, "5", "2", "5", score="3-2")]
    service, reg, _ = make_service(monkeypatch, rounds=[final_round], matches=final)
    run(service.initialize())

    summary = run(service.complete_tournament("42"))

    assert summary["champion_discord_user_id"] == "5"
    assert summary["runner_up_discord_user_id"] == "2"
    # The competitive gate itself is validation-only. The existing tournament
    # lifecycle service owns the authoritative completed transition and audit.
    assert reg.audit == []


def test_completion_rejects_unclosed_final(monkeypatch):
    final_round = round_row(f"{TID}-F", "final", 6, "ready_to_close")
    final = [finalized(f"{TID}-F", 1, "5", "2", "5", score="3-1")]
    service, _, _ = make_service(monkeypatch, rounds=[final_round], matches=final)
    run(service.initialize())

    with pytest.raises(RegistrationError, match="Close the finalized Final"):
        run(service.complete_tournament("42"))
