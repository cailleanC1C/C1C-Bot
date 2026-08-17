from __future__ import annotations

import json
from types import SimpleNamespace

from modules.community.live_arena import captains_table_control_center as control
from modules.community.live_arena import knockout_progression_finalizer as finalizer
from modules.community.live_arena import knockout_transition_repair


def _seed_round():
    seeds = [
        {
            "seed": index,
            "discord_user_id": str(index),
            "display_name": f"Player {index}",
            "qualification_rank": index,
            "record": "0-0",
        }
        for index in range(1, 9)
    ]
    return {
        "tournament_id": "LA-TEST",
        "round_id": "LA-TEST-TOP8",
        "round_name": "Top 8 Seeding",
        "round_stage": "top8_seeding",
        "round_number": "3",
        "status": "frozen",
        "notes": json.dumps({"seeds": seeds}),
    }


def _round(stage: str, status: str, suffix: str, number: int):
    return {
        "tournament_id": "LA-TEST",
        "round_id": f"LA-TEST-{suffix}",
        "round_name": stage.title(),
        "round_stage": stage,
        "round_number": str(number),
        "status": status,
    }


def _match(round_id: str, number: int, a: int, b: int, winner: int, score_a: int, score_b: int):
    return {
        "tournament_id": "LA-TEST",
        "round_id": round_id,
        "match_id": f"{round_id}-M{number:02d}",
        "match_number": str(number),
        "player_a_discord_user_id": str(a),
        "player_b_discord_user_id": str(b),
        "final_winner_discord_user_id": str(winner),
        "final_score_a": str(score_a),
        "final_score_b": str(score_b),
        "status": "finalized",
    }


def test_quarterfinal_results_replace_frozen_seed_order():
    rounds = [_seed_round(), _round("quarterfinal", "ready_to_close", "QF", 4)]
    # Fixed QF slots: 1v8, 4v5, 2v7, 3v6.  Winners are seeds 1, 4, 7, 3.
    matches = [
        _match("LA-TEST-QF", 1, 1, 8, 1, 2, 1),
        _match("LA-TEST-QF", 2, 4, 5, 4, 2, 1),
        _match("LA-TEST-QF", 3, 2, 7, 7, 0, 2),
        _match("LA-TEST-QF", 4, 3, 6, 3, 2, 1),
    ]

    standings = finalizer.calculate_knockout_standings(rounds, matches, "LA-TEST")

    assert [entry.discord_user_id for entry in standings] == [
        "1",
        "3",
        "4",
        "7",
        "2",
        "5",
        "6",
        "8",
    ]
    assert [entry.match_record for entry in standings] == [
        "1-0",
        "1-0",
        "1-0",
        "1-0",
        "0-1",
        "0-1",
        "0-1",
        "0-1",
    ]


def test_semifinal_results_keep_all_top8_in_current_order():
    rounds = [
        _seed_round(),
        _round("quarterfinal", "closed", "QF", 4),
        _round("semifinal", "ready_to_close", "SF", 5),
    ]
    matches = [
        _match("LA-TEST-QF", 1, 1, 8, 1, 2, 1),
        _match("LA-TEST-QF", 2, 4, 5, 4, 2, 1),
        _match("LA-TEST-QF", 3, 2, 7, 7, 0, 2),
        _match("LA-TEST-QF", 4, 3, 6, 3, 2, 1),
        _match("LA-TEST-SF", 1, 1, 4, 4, 1, 2),
        _match("LA-TEST-SF", 2, 7, 3, 3, 0, 2),
    ]

    standings = finalizer.calculate_knockout_standings(rounds, matches, "LA-TEST")

    assert [entry.discord_user_id for entry in standings] == [
        "3",
        "4",
        "1",
        "7",
        "2",
        "5",
        "6",
        "8",
    ]
    assert [entry.match_record for entry in standings[:4]] == [
        "2-0",
        "2-0",
        "1-1",
        "1-1",
    ]


def test_any_knockout_round_ready_to_close_requires_panel_refresh():
    for stage, suffix, number in (
        ("quarterfinal", "QF", 4),
        ("semifinal", "SF", 5),
        ("final", "F", 6),
    ):
        rounds = [_round(stage, "ready_to_close", suffix, number)]
        assert finalizer._has_closable_round(rounds, "LA-TEST") is True


def test_qf_close_to_semifinal_preview_exposes_next_progression_button():
    rounds = [
        _round("qualification", "closed", "Q3", 3),
        _seed_round(),
        _round("quarterfinal", "closed", "QF", 4),
        _round("semifinal", "preview", "SF", 5),
    ]
    state = control.ControlState(
        tournament_id="LA-TEST",
        rounds=rounds,
        matches=[],
        standings=[],
        tie_groups=[],
        tiebreak_matches=[],
        tiebreak_resolved=True,
    )
    manager = SimpleNamespace(
        _captains_table_allowed={
            "View Standings",
            "Close Current Round",
        }
    )

    knockout_transition_repair._apply_progression_state(manager, state)

    assert "Approve & Open Knockout" in manager._captains_table_allowed
    assert "Close Current Round" not in manager._captains_table_allowed


def test_semifinal_close_to_final_preview_exposes_next_progression_button():
    rounds = [
        _round("qualification", "closed", "Q3", 3),
        _seed_round(),
        _round("quarterfinal", "closed", "QF", 4),
        _round("semifinal", "closed", "SF", 5),
        _round("final", "preview", "F", 6),
    ]
    state = control.ControlState(
        tournament_id="LA-TEST",
        rounds=rounds,
        matches=[],
        standings=[],
        tie_groups=[],
        tiebreak_matches=[],
        tiebreak_resolved=True,
    )
    manager = SimpleNamespace(_captains_table_allowed={"View Standings"})

    knockout_transition_repair._apply_progression_state(manager, state)

    assert "Approve & Open Knockout" in manager._captains_table_allowed
