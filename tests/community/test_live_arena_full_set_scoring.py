from types import SimpleNamespace

import pytest

from modules.community.live_arena import full_set_scoring
from modules.community.live_arena.registration import RegistrationError


def test_bo3_requires_all_three_fights():
    round_row = {"round_stage": "qualification"}
    for score in ((3, 0), (2, 1), (1, 2), (0, 3)):
        full_set_scoring._validate_full_set_score(round_row, *score)

    for score in ((2, 0), (0, 2), (3, 1), (1, 1)):
        with pytest.raises(RegistrationError):
            full_set_scoring._validate_full_set_score(round_row, *score)


def test_final_requires_all_five_fights():
    round_row = {"round_stage": "final"}
    for score in ((5, 0), (4, 1), (3, 2), (2, 3), (1, 4), (0, 5)):
        full_set_scoring._validate_full_set_score(round_row, *score)

    for score in ((3, 0), (3, 1), (2, 2), (5, 1)):
        with pytest.raises(RegistrationError):
            full_set_scoring._validate_full_set_score(round_row, *score)


def test_match_embed_explains_play_all_three(monkeypatch):
    from modules.community.live_arena import qualification_panel

    embed = qualification_panel.match_embed(
        {"tournament_name": "Cup"},
        {
            "round_stage": "qualification",
            "round_name": "Qualification Round 1",
            "deadline_at_utc": "2026-08-19T11:27:00+00:00",
            "opens_at_utc": "2026-08-13T11:27:00+00:00",
        },
        {
            "match_number": "1",
            "player_a_discord_user_id": "1",
            "player_b_discord_user_id": "2",
            "shared_slot_ids_csv": "",
        },
        [],
    )
    assert "3 fights · play all 3" in (embed.description or "")


def test_match_embed_explains_play_all_five_in_final():
    from modules.community.live_arena import qualification_panel

    embed = qualification_panel.match_embed(
        {"tournament_name": "Cup"},
        {
            "round_stage": "final",
            "round_name": "Final",
            "deadline_at_utc": "2026-08-19T11:27:00+00:00",
            "opens_at_utc": "2026-08-13T11:27:00+00:00",
        },
        {
            "match_number": "1",
            "player_a_discord_user_id": "1",
            "player_b_discord_user_id": "2",
            "shared_slot_ids_csv": "",
        },
        [],
    )
    assert "5 fights · play all 5" in (embed.description or "")


def test_captains_table_labels_are_organizer_friendly():
    assert full_set_scoring._FRIENDLY_LABELS["Close Current Round"] == "Finish Round"
    assert full_set_scoring._FRIENDLY_LABELS["Review Result Issues"] == "Review Match Issues"
    assert full_set_scoring._FRIENDLY_LABELS["View Roster"] == "View Players"
    assert full_set_scoring._FRIENDLY_LABELS["Competition Ops"] == "Organizer Actions"
    assert full_set_scoring._FRIENDLY_LABELS["Repair Discord State"] == "Repair Tournament"


def test_captains_table_groups_primary_and_support_actions_for_mobile():
    assert full_set_scoring._FRIENDLY_ROWS["Finish Round"] == 0
    assert full_set_scoring._FRIENDLY_ROWS["Review Match Issues"] == 1
    assert full_set_scoring._FRIENDLY_ROWS["View Players"] == 2
    assert full_set_scoring._FRIENDLY_ROWS["Repair Tournament"] == 3
