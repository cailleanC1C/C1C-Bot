from __future__ import annotations

import pytest

from modules.community.live_arena.swiss import (
    SwissPairingError,
    SwissPlayer,
    pair_swiss,
    source_fingerprint,
)


def p(uid, wins, losses, rank, index):
    return SwissPlayer(
        user_id=str(uid),
        display_name=f"P{uid}",
        wins=wins,
        losses=losses,
        rank=rank,
        ranking_index=index,
    )


def pair_ids(pairs):
    return {frozenset((pair.player_a.user_id, pair.player_b.user_id)) for pair in pairs}


def test_same_group_uses_high_vs_low_deterministically():
    players = [
        p(1, 1, 0, 1, 0),
        p(2, 1, 0, 2, 1),
        p(3, 1, 0, 3, 2),
        p(4, 1, 0, 4, 3),
    ]

    pairs = pair_swiss(players, set())

    assert pair_ids(pairs) == {frozenset(("1", "4")), frozenset(("2", "3"))}
    assert all("same-group" in pair.rationale for pair in pairs)


def test_rematch_is_absolute_and_uses_closest_valid_same_group_alternative():
    players = [
        p(1, 1, 0, 1, 0),
        p(2, 1, 0, 2, 1),
        p(3, 1, 0, 3, 2),
        p(4, 1, 0, 4, 3),
    ]
    history = {frozenset(("1", "4"))}

    pairs = pair_swiss(players, history)

    assert frozenset(("1", "4")) not in pair_ids(pairs)
    assert pair_ids(pairs) == {frozenset(("1", "3")), frozenset(("2", "4"))}


def test_odd_stronger_group_floats_lowest_ranked_eligible_down():
    players = [
        p(1, 2, 0, 1, 0),
        p(2, 2, 0, 2, 1),
        p(3, 2, 0, 3, 2),
        p(4, 1, 1, 4, 3),
        p(5, 1, 1, 5, 4),
        p(6, 1, 1, 6, 5),
    ]

    pairs = pair_swiss(players, set())
    ids = pair_ids(pairs)

    # Player 3 is the lowest-ranked 2-0 player and should be the floater.
    assert any("3" in pair and ("4" in pair or "5" in pair or "6" in pair) for pair in ids)
    floated = [pair for pair in pairs if pair.player_a.record != pair.player_b.record]
    assert len(floated) == 1
    assert "P3" in floated[0].rationale


def test_default_floater_changes_when_rematch_blocks_it():
    players = [
        p(1, 2, 0, 1, 0),
        p(2, 2, 0, 2, 1),
        p(3, 2, 0, 3, 2),
        p(4, 1, 1, 4, 3),
        p(5, 1, 1, 5, 4),
        p(6, 1, 1, 6, 5),
    ]
    # Block player 3 from all immediately weaker candidates.
    history = {
        frozenset(("3", "4")),
        frozenset(("3", "5")),
        frozenset(("3", "6")),
    }

    pairs = pair_swiss(players, history)
    floated = [pair for pair in pairs if pair.player_a.record != pair.player_b.record]

    assert len(floated) == 1
    assert "3" not in {floated[0].player_a.user_id, floated[0].player_b.user_id}


def test_never_crosses_two_record_groups():
    players = [
        p(1, 2, 0, 1, 0),
        p(2, 2, 0, 2, 1),
        p(3, 1, 1, 3, 2),
        p(4, 1, 1, 4, 3),
        p(5, 0, 2, 5, 4),
        p(6, 0, 2, 6, 5),
    ]

    pairs = pair_swiss(players, set())

    for pair in pairs:
        assert abs(pair.player_a.wins - pair.player_b.wins) <= 1
        assert {pair.player_a.record_label, pair.player_b.record_label} != {"2-0", "0-2"}


def test_no_valid_pairing_stops_for_organizer_review_instead_of_relaxing_rules():
    players = [
        p(1, 1, 0, 1, 0),
        p(2, 1, 0, 2, 1),
        p(3, 0, 1, 3, 2),
        p(4, 0, 1, 4, 3),
    ]
    history = {
        frozenset(("1", "2")),
        frozenset(("1", "3")),
        frozenset(("1", "4")),
    }

    with pytest.raises(SwissPairingError, match="Organizer review is required"):
        pair_swiss(players, history)


def test_source_fingerprint_changes_when_finalized_result_truth_changes():
    base = {
        "tournament_id": "T1",
        "round_id": "T1-Q1",
        "match_id": "T1-Q1-M01",
        "status": "finalized",
        "final_result_type": "played",
        "final_score_a": "2",
        "final_score_b": "1",
        "final_winner_discord_user_id": "1",
    }
    first = source_fingerprint([base], "T1", before_round=2)
    changed = dict(base, final_score_b="0")
    second = source_fingerprint([changed], "T1", before_round=2)

    assert first != second


def test_source_fingerprint_ignores_preview_rows_for_the_target_round():
    q1 = {
        "tournament_id": "T1",
        "round_id": "T1-Q1",
        "match_id": "T1-Q1-M01",
        "status": "finalized",
        "final_result_type": "played",
        "final_score_a": "2",
        "final_score_b": "0",
        "final_winner_discord_user_id": "1",
    }
    q2_preview = {
        "tournament_id": "T1",
        "round_id": "T1-Q2",
        "match_id": "T1-Q2-M01",
        "status": "preview",
    }

    assert source_fingerprint([q1], "T1", before_round=2) == source_fingerprint(
        [q1, q2_preview], "T1", before_round=2
    )
