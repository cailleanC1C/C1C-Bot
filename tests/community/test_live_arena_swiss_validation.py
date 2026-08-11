from __future__ import annotations

import pytest

from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.swiss_validation import validate_persisted_swiss_draw

TID = "T1"


def finalized(round_number, number, a, b, score_a, score_b):
    return {
        "tournament_id": TID,
        "round_id": f"{TID}-Q{round_number}",
        "match_id": f"Q{round_number}-M{number}",
        "player_a_discord_user_id": str(a),
        "player_a_display_name": f"P{a}",
        "player_b_discord_user_id": str(b),
        "player_b_display_name": f"P{b}",
        "status": "finalized",
        "final_result_type": "played",
        "final_score_a": str(score_a),
        "final_score_b": str(score_b),
        "final_winner_discord_user_id": str(a if score_a > score_b else b),
    }


def preview(round_number, number, a, b):
    return {
        "tournament_id": TID,
        "round_id": f"{TID}-Q{round_number}",
        "match_id": f"Q{round_number}-M{number}",
        "player_a_discord_user_id": str(a),
        "player_b_discord_user_id": str(b),
        "status": "preview",
    }


def q1_truth():
    return [
        finalized(1, 1, 1, 4, 2, 0),
        finalized(1, 2, 2, 3, 2, 1),
    ]


def test_full_validator_accepts_complete_rematch_free_adjacent_draw():
    rows = q1_truth() + [preview(2, 1, 1, 3), preview(2, 2, 2, 4)]
    validate_persisted_swiss_draw(rows, TID, 2)


def test_full_validator_rejects_missing_prior_round_player():
    rows = q1_truth() + [preview(2, 1, 1, 3)]
    with pytest.raises(RegistrationError, match="full prior-round field"):
        validate_persisted_swiss_draw(rows, TID, 2)


def test_full_validator_rejects_rematch():
    rows = q1_truth() + [preview(2, 1, 1, 4), preview(2, 2, 2, 3)]
    with pytest.raises(RegistrationError, match="rematch"):
        validate_persisted_swiss_draw(rows, TID, 2)


def test_full_validator_rejects_non_adjacent_records_without_rematch_overlap():
    q1 = [
        finalized(1, 1, 1, 8, 2, 0),
        finalized(1, 2, 2, 7, 2, 0),
        finalized(1, 3, 3, 6, 2, 0),
        finalized(1, 4, 4, 5, 2, 0),
    ]
    q2 = [
        finalized(2, 1, 1, 2, 2, 0),
        finalized(2, 2, 3, 4, 2, 0),
        finalized(2, 3, 5, 6, 2, 0),
        finalized(2, 4, 7, 8, 2, 0),
    ]
    # 1/3 are 2-0; 6/8 are 0-2, and these particular crossings are not rematches.
    q3 = [
        preview(3, 1, 1, 6),
        preview(3, 2, 3, 8),
        preview(3, 3, 2, 5),
        preview(3, 4, 4, 7),
    ]
    with pytest.raises(RegistrationError, match="non-adjacent"):
        validate_persisted_swiss_draw(q1 + q2 + q3, TID, 3)
