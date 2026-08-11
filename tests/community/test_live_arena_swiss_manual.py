from __future__ import annotations

import pytest

from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.swiss import SwissPlayer
from modules.community.live_arena.swiss_manual import (
    _validate_complete_candidate,
    conflicted_preview_players,
    parse_manual_pairs,
)


def player(uid: str, wins: int, losses: int, rank: int) -> SwissPlayer:
    return SwissPlayer(
        user_id=uid,
        display_name=f"P{uid}",
        wins=wins,
        losses=losses,
        rank=rank,
        ranking_index=rank - 1,
    )


def match(a: str, b: str):
    return {
        "player_a_discord_user_id": a,
        "player_b_discord_user_id": b,
    }


def test_valid_preview_has_no_manual_repair_subset():
    players = {
        "1": player("1", 1, 0, 1),
        "2": player("2", 1, 0, 2),
        "3": player("3", 0, 1, 3),
        "4": player("4", 0, 1, 4),
    }
    current = [match("1", "2"), match("3", "4")]

    assert conflicted_preview_players(current, set(players), players, set()) == set()


def test_manual_subset_contains_only_players_implicated_by_rematch():
    players = {
        "1": player("1", 1, 0, 1),
        "2": player("2", 1, 0, 2),
        "3": player("3", 0, 1, 3),
        "4": player("4", 0, 1, 4),
    }
    current = [match("1", "2"), match("3", "4")]
    history = {frozenset(("1", "2"))}

    assert conflicted_preview_players(current, set(players), players, history) == {"1", "2"}


def test_missing_and_duplicate_players_expand_conflicted_subset_without_touching_valid_pair():
    players = {
        "1": player("1", 1, 0, 1),
        "2": player("2", 1, 0, 2),
        "3": player("3", 0, 1, 3),
        "4": player("4", 0, 1, 4),
        "5": player("5", 0, 1, 5),
        "6": player("6", 0, 1, 6),
    }
    current = [match("1", "2"), match("3", "4"), match("3", "5")]

    conflicted = conflicted_preview_players(current, set(players), players, set())

    assert conflicted == {"3", "6"}
    assert "1" not in conflicted and "2" not in conflicted


def test_manual_candidate_full_validation_rejects_rematch():
    players = {
        "1": player("1", 1, 0, 1),
        "2": player("2", 1, 0, 2),
        "3": player("3", 0, 1, 3),
        "4": player("4", 0, 1, 4),
    }
    candidate = [match("1", "3"), match("2", "4")]

    with pytest.raises(RegistrationError, match="rematch"):
        _validate_complete_candidate(
            candidate,
            set(players),
            players,
            {frozenset(("1", "3"))},
        )


def test_manual_candidate_full_validation_rejects_non_adjacent_record_cross():
    players = {
        "1": player("1", 2, 0, 1),
        "2": player("2", 2, 0, 2),
        "3": player("3", 0, 2, 3),
        "4": player("4", 0, 2, 4),
    }
    candidate = [match("1", "3"), match("2", "4")]

    with pytest.raises(RegistrationError, match="non-adjacent"):
        _validate_complete_candidate(candidate, set(players), players, set())


def test_manual_pair_parser_accepts_lines_and_commas_and_rejects_duplicates():
    assert parse_manual_pairs("1-2, 3-4\n5-6") == [("1", "2"), ("3", "4"), ("5", "6")]

    with pytest.raises(RegistrationError, match="only once"):
        parse_manual_pairs("1-2, 1-3")
