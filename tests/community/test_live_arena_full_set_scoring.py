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


class _FakeView:
    def __init__(self, labels):
        self.children = [SimpleNamespace(label=label, row=None) for label in labels]

    def remove_item(self, item):
        self.children.remove(item)


def test_visible_captains_table_keeps_only_allowed_actions_and_renames_them():
    view = _FakeView(
        [
            "Open Registration",
            "Close Current Round",
            "View Standings",
            "Review Result Issues",
            "Competition Ops",
            "View Roster",
            "Repair Discord State",
            "Freeze Top 8",
        ]
    )

    result = full_set_scoring._finalize_visible_view(
        view,
        {
            "View Standings",
            "Review Result Issues",
            "Competition Ops",
            "View Roster",
            "Repair Discord State",
        },
    )

    assert [item.label for item in result.children] == [
        "View Standings",
        "Review Match Issues",
        "Organizer Actions",
        "View Players",
        "Repair Tournament",
    ]
    assert [item.row for item in result.children] == [0, 1, 1, 2, 3]


def test_visible_captains_table_accepts_already_friendly_labels():
    view = _FakeView(["View Players", "Repair Tournament", "Finish Tournament"])

    result = full_set_scoring._finalize_visible_view(
        view,
        {"View Roster", "Repair Discord State"},
    )

    assert [item.label for item in result.children] == [
        "View Players",
        "Repair Tournament",
    ]
