from types import SimpleNamespace

import pytest

from modules.community.live_arena import captains_table_render, full_set_scoring
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


class _FakeItem:
    def __init__(self, label):
        self.label = label
        self.row = None


class _FakeView:
    def __init__(self, labels):
        self.children = [_FakeItem(label) for label in labels]

    def remove_item(self, item):
        self.children.remove(item)


def test_captains_table_active_round_is_small_and_friendly():
    view = _FakeView(
        [
            "Open Registration",
            "View Roster",
            "Reconcile Roles",
            "Close Current Round",
            "Review Result Issues",
            "Competition Ops",
            "View Standings",
            "Approve & Publish Swiss",
            "Freeze Top 8",
            "Repair Discord State",
            "Player History",
        ]
    )
    allowed = {
        "View Roster",
        "Reconcile Roles",
        "Close Current Round",
        "Review Result Issues",
        "Competition Ops",
        "View Standings",
        "Repair Discord State",
        "Player History",
    }

    result = captains_table_render._canonicalize_view(view, allowed)

    assert [item.label for item in result.children] == [
        "View Players",
        "Fix Player Roles",
        "Finish Round",
        "Review Match Issues",
        "Organizer Actions",
        "View Standings",
        "Repair Tournament",
    ]
    rows = {item.label: item.row for item in result.children}
    assert rows["Finish Round"] == 0
    assert rows["Review Match Issues"] == 1
    assert rows["View Players"] == 2
    assert rows["Repair Tournament"] == 3


def test_captains_table_filters_labels_already_renamed_by_older_layer():
    view = _FakeView(
        [
            "Finish Round",
            "Review Match Issues",
            "View Players",
            "Repair Tournament",
            "Publish Next Round",
        ]
    )
    allowed = {
        "Close Current Round",
        "Review Result Issues",
        "View Roster",
        "Repair Discord State",
    }

    result = captains_table_render._canonicalize_view(view, allowed)

    assert [item.label for item in result.children] == [
        "Finish Round",
        "Review Match Issues",
        "View Players",
        "Repair Tournament",
    ]
