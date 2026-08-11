from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from modules.community.live_arena.bye_support import choose_ranked_bye, previous_bye_users
from modules.community.live_arena.competition_operations import _mandatory_time


def player(uid: str, index: int):
    return SimpleNamespace(user_id=uid, ranking_index=index)


def test_q2_q3_bye_uses_lowest_ranked_player_without_previous_bye():
    players = [player("1", 0), player("2", 1), player("3", 2), player("4", 3), player("5", 4)]
    matches = [
        {
            "tournament_id": "T1",
            "status": "bye",
            "final_result_type": "bye",
            "player_a_discord_user_id": "5",
        }
    ]

    selected = choose_ranked_bye(players, matches, "T1")

    assert selected.user_id == "4"


def test_second_bye_only_after_every_remaining_player_has_one():
    players = [player("1", 0), player("2", 1), player("3", 2)]
    matches = [
        {
            "tournament_id": "T1",
            "status": "bye",
            "final_result_type": "bye",
            "player_a_discord_user_id": uid,
        }
        for uid in ("1", "2", "3")
    ]

    selected = choose_ranked_bye(players, matches, "T1")

    assert selected.user_id == "3"


def test_previous_byes_are_scoped_to_tournament():
    matches = [
        {
            "tournament_id": "T1",
            "status": "bye",
            "player_a_discord_user_id": "1",
        },
        {
            "tournament_id": "T2",
            "final_result_type": "bye",
            "player_a_discord_user_id": "2",
        },
    ]

    assert previous_bye_users(matches, "T1") == {"1"}


def test_mandatory_time_is_parsed_from_existing_match_notes():
    value = _mandatory_time(
        "No shared enabled availability slot\n"
        "MANDATORY_TIME=2026-08-24T18:00:00Z | organizer=42 | reason=no overlap"
    )

    assert value == datetime(2026, 8, 24, 18, 0, tzinfo=UTC)


def test_mandatory_time_absent_returns_none():
    assert _mandatory_time("No shared enabled availability slot") is None
