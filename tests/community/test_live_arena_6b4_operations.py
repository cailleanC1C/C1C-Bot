from datetime import UTC, datetime
from types import SimpleNamespace

from modules.community.live_arena import swiss_manual
from modules.community.live_arena.bye_support import choose_ranked_bye, previous_bye_users
from modules.community.live_arena.competition_operations import _mandatory_time
from modules.community.live_arena.result_views import MatchResultView
from modules.community.live_arena.scheduling_resolution_ux import SchedulingMatchPicker, SchedulingOutcomeView
from modules.community.live_arena.withdrawal_hardening import (
    _mark_withdrawal_advance,
    _withdrawal_marker,
)


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


def test_knockout_withdrawal_marker_advances_only_the_opponent():
    match = {
        "player_a_discord_user_id": "10",
        "player_b_discord_user_id": "20",
        "notes": "Fixed knockout bracket slot",
    }

    _mark_withdrawal_advance(match, "10", "withdrew")

    assert _withdrawal_marker(match) == "20"
    assert "withdrawn=10" in match["notes"]


def test_scheduling_button_is_disabled_when_result_reporting_is_disabled():
    view = MatchResultView("sheet", report_disabled=True, dispute_disabled=True)
    scheduling = next(
        item
        for item in view.children
        if getattr(item, "custom_id", "")
        == "live_arena:match:report_scheduling_problem"
    )

    assert scheduling.disabled is True


def test_guided_scheduling_picker_shows_real_participant_names():
    row = {
        "match_id": "T1-Q1-M2",
        "match_number": "2",
        "player_a_discord_user_id": "11",
        "player_a_display_name": "Atlantic5penguin",
        "player_b_discord_user_id": "22",
        "player_b_display_name": "Caillean",
    }
    picker = SchedulingMatchPicker(SimpleNamespace(), [row])

    assert picker.options[0].label == "M2 · Atlantic5penguin vs Caillean"
    assert picker.options[0].value == "T1-Q1-M2"


def test_guided_scheduling_outcomes_use_real_participant_names():
    row = {
        "match_id": "T1-Q1-M2",
        "player_a_discord_user_id": "11",
        "player_a_display_name": "Atlantic5penguin",
        "player_b_discord_user_id": "22",
        "player_b_display_name": "Caillean",
    }
    view = SchedulingOutcomeView(SimpleNamespace(), row)
    labels = [item.label for item in view.children]

    assert labels[0].startswith("Atlantic5penguin")
    assert labels[1].startswith("Caillean")
    assert labels[2] == "Both players forfeit"


def test_swiss_manual_validation_accepts_one_bye_plus_complete_pairs():
    current = [
        {
            "player_a_discord_user_id": "1",
            "player_b_discord_user_id": "2",
            "notes": "Swiss pairing",
        },
        {
            "player_a_discord_user_id": "3",
            "player_b_discord_user_id": "4",
            "notes": "Swiss pairing",
        },
        {
            "player_a_discord_user_id": "5",
            "player_b_discord_user_id": "",
            "notes": "QUALIFICATION_BYE",
        },
    ]
    players = {
        uid: SimpleNamespace(wins=1, losses=0)
        for uid in ("1", "2", "3", "4", "5")
    }

    swiss_manual._validate_complete_candidate(
        current,
        set(players),
        players,
        set(),
    )


def test_swiss_conflict_detection_does_not_treat_the_bye_as_missing_pairing():
    current = [
        {
            "player_a_discord_user_id": "1",
            "player_b_discord_user_id": "2",
            "notes": "Swiss pairing",
        },
        {
            "player_a_discord_user_id": "3",
            "player_b_discord_user_id": "4",
            "notes": "Swiss pairing",
        },
        {
            "player_a_discord_user_id": "5",
            "player_b_discord_user_id": "",
            "notes": "QUALIFICATION_BYE",
        },
    ]
    players = {
        uid: SimpleNamespace(wins=1, losses=0)
        for uid in ("1", "2", "3", "4", "5")
    }

    conflicted = swiss_manual.conflicted_preview_players(
        current,
        set(players),
        players,
        set(),
    )

    assert conflicted == set()