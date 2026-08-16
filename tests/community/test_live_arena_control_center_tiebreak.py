from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from modules.community.live_arena import captains_table_control_center as control
from modules.community.live_arena import plain_language_ux
from modules.community.live_arena.competition import StandingEntry
from modules.community.live_arena.registration import RegistrationError


def standing(uid: str, name: str, rank: int, wins: int = 2, losses: int = 1):
    return StandingEntry(
        discord_user_id=uid,
        display_name=name,
        match_wins=wins,
        match_losses=losses,
        game_wins=4,
        game_losses=2,
        game_differential=2,
        strength_of_opponents=4,
        rank=rank,
        tied=True,
    )


def test_tiebreak_groups_keep_only_shared_rank_groups():
    standings = [
        standing("1", "One", 1),
        standing("2", "Two", 2),
        standing("3", "Three", 2),
        standing("4", "Four", 4),
    ]
    assert control._tie_groups(standings, standings[1:3]) == [["2", "3"]]


def test_new_tiebreak_round_uses_auditable_resolution_payload():
    row = control._new_round("LA-TEST", "2026-08-16T18:00:00Z")
    assert row["round_stage"] == "qualification_tiebreak"
    assert json.loads(row["notes"]) == {"resolutions": []}


def test_control_center_calls_out_tiebreak_as_current_action():
    tid = "LA-TEST"
    rounds = [
        {"tournament_id": tid, "round_id": f"{tid}-Q1", "round_stage": "qualification", "round_number": "1", "status": "closed"},
        {"tournament_id": tid, "round_id": f"{tid}-Q2", "round_stage": "qualification", "round_number": "2", "status": "closed"},
        {"tournament_id": tid, "round_id": f"{tid}-Q3", "round_stage": "qualification", "round_number": "3", "status": "closed"},
    ]
    standings = [standing("1", "smurf", 7), standing("2", "Glove", 7)]
    match = {
        "tournament_id": tid,
        "round_id": f"{tid}-TB",
        "match_id": f"{tid}-TB-M01",
        "player_a_discord_user_id": "1",
        "player_a_display_name": "smurf",
        "player_b_discord_user_id": "2",
        "player_b_display_name": "Glove",
        "status": "published",
        "thread_id": "123",
    }
    state = control.ControlState(
        tid,
        rounds,
        [match],
        standings,
        [["1", "2"]],
        [match],
        False,
    )

    stage, current, next_step = control._stage_summary(state)
    assert stage == "Qualification finished"
    assert "tiebreak" in current.lower()
    assert next_step == "Lock the Top 8"
    progress = control._progress_lines(state)
    assert "Qualification tiebreak — ⚠️ Waiting" in progress
    assert "Top 8 — 🔒 Not locked" in progress


def test_plain_language_preview_hides_internal_pairing_jargon():
    snapshot = SimpleNamespace(
        round_row={"round_number": "3"},
        matches=(
            {
                "match_number": "1",
                "player_a_display_name": "smurf",
                "player_b_display_name": "Glove",
                "has_scheduling_conflict": "false",
                "notes": "Swiss adjacent-group float · no-rematch constraint preserved",
            },
        ),
    )
    embed = plain_language_ux._qualification_preview(snapshot, official=False)
    rendered = str(embed.to_dict())
    assert "Swiss" not in rendered
    assert "adjacent-group" not in rendered
    assert "qualification standings" in rendered


def test_score_validation_uses_plain_language_without_tournament_abbreviations():
    with pytest.raises(RegistrationError) as exc:
        plain_language_ux._plain_score_validation({"round_stage": "qualification"}, 3, 0)
    assert "BO3" not in str(exc.value)
    assert "2-0 or 2-1" in str(exc.value)

    with pytest.raises(RegistrationError) as exc:
        plain_language_ux._plain_score_validation({"round_stage": "final"}, 2, 1)
    assert "BO5" not in str(exc.value)
    assert "Final result" in str(exc.value)
