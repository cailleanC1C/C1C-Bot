from __future__ import annotations

import discord

from modules.community.live_arena import captains_table_action_state as action_state
from modules.community.live_arena import captains_table_control_center as control
from modules.community.live_arena import full_set_scoring


def _state(*, q3_status="closed", tiebreak_status="resolved", winner="2", seeded=False):
    match = {
        "tournament_id": "LA-TEST",
        "round_id": "LA-TEST-TB",
        "match_id": "LA-TEST-TB-M01",
        "match_number": "1",
        "player_a_discord_user_id": "1",
        "player_a_display_name": "smurf",
        "player_b_discord_user_id": "2",
        "player_b_display_name": "Glove",
        "status": "finalized" if winner else "published",
        "final_winner_discord_user_id": winner,
    }
    rounds = [
        {
            "tournament_id": "LA-TEST",
            "round_id": "LA-TEST-Q1",
            "round_stage": "qualification",
            "round_number": "1",
            "status": "closed",
        },
        {
            "tournament_id": "LA-TEST",
            "round_id": "LA-TEST-Q2",
            "round_stage": "qualification",
            "round_number": "2",
            "status": "closed",
        },
        {
            "tournament_id": "LA-TEST",
            "round_id": "LA-TEST-Q3",
            "round_stage": "qualification",
            "round_number": "3",
            "status": q3_status,
        },
        {
            "tournament_id": "LA-TEST",
            "round_id": "LA-TEST-TB",
            "round_stage": "qualification_tiebreak",
            "round_number": "3",
            "status": tiebreak_status,
        },
    ]
    if seeded:
        rounds.append(
            {
                "tournament_id": "LA-TEST",
                "round_id": "LA-TEST-TOP8",
                "round_stage": "top8_seeding",
                "round_number": "3",
                "status": "frozen",
            }
        )
    return control.ControlState(
        "LA-TEST",
        rounds,
        [match],
        [],
        [["1", "2"]],
        [match],
        tiebreak_status == "resolved",
    )


class _Manager:
    def __init__(self, allowed):
        self._captains_table_allowed = set(allowed)
        self._qualification_q1_status = "ready_to_close"

    def view(self, _status):
        view = discord.ui.View(timeout=None)
        for index, label in enumerate(
            (
                "Close Current Round",
                "Freeze Top 8",
                "Open Tiebreak Match",
                "View Roster",
                "Repair Discord State",
            )
        ):
            view.add_item(
                discord.ui.Button(
                    label=label,
                    custom_id=f"test:{index}",
                )
            )
        return full_set_scoring._finalize_visible_view(
            view, set(self._captains_table_allowed)
        )


def _labels(manager):
    return [item.label for item in manager.view("active").children]


def test_resolved_live_tiebreak_replaces_stale_finish_round_with_lock_top8():
    """Exact production shape: stale ready-to-close cache, but tiebreak is final."""

    manager = _Manager(
        {
            "Close Current Round",
            "Record BO3 Tiebreak",
            "View Roster",
            "Repair Discord State",
        }
    )
    state = _state()

    action_state._apply_final_action_state(manager, state)
    labels = _labels(manager)

    assert "Lock Top 8" in labels
    assert "Finish Round" not in labels
    assert "Open Tiebreak Match" not in labels


def test_unresolved_tiebreak_exposes_match_not_finish_or_lock():
    manager = _Manager(
        {
            "Close Current Round",
            "Freeze Top 8",
            "View Roster",
            "Repair Discord State",
        }
    )
    state = _state(tiebreak_status="active", winner="")

    action_state._apply_final_action_state(manager, state)
    labels = _labels(manager)

    assert "Open Tiebreak Match" in labels
    assert "Finish Round" not in labels
    assert "Lock Top 8" not in labels


def test_q3_ready_to_finish_keeps_finish_round_before_post_qualification_state():
    manager = _Manager(
        {"Close Current Round", "View Roster", "Repair Discord State"}
    )
    state = _state(q3_status="ready_to_close", tiebreak_status="", winner="")

    action_state._apply_final_action_state(manager, state)

    assert "Finish Round" in _labels(manager)


def test_locked_top8_removes_all_post_qualification_progression_buttons():
    manager = _Manager(
        {
            "Close Current Round",
            "Freeze Top 8",
            "Record BO3 Tiebreak",
            "View Roster",
            "Repair Discord State",
        }
    )
    state = _state(seeded=True)

    action_state._apply_final_action_state(manager, state)
    labels = _labels(manager)

    assert "Finish Round" not in labels
    assert "Lock Top 8" not in labels
    assert "Open Tiebreak Match" not in labels
