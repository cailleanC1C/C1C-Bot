from types import SimpleNamespace

import discord

from modules.community.live_arena import captains_table_quota_safe, full_set_scoring


def _view_with_labels(labels):
    view = discord.ui.View(timeout=None)
    for index, label in enumerate(labels):
        view.add_item(
            discord.ui.Button(
                label=label,
                custom_id=f"test:{index}",
                style=discord.ButtonStyle.secondary,
            )
        )
    return view


def test_active_quota_fallback_is_compact_and_fail_closed():
    manager = SimpleNamespace(_qualification_q1_status="active")
    allowed = captains_table_quota_safe._safe_panel_actions(manager, "active")

    assert allowed == {
        "View Standings",
        "Review Result Issues",
        "Competition Ops",
        "View Roster",
        "Repair Discord State",
    }
    assert "Complete Tournament" not in allowed
    assert "Approve & Publish Swiss" not in allowed
    assert "Freeze Top 8" not in allowed


def test_active_ready_to_close_fallback_exposes_finish_round_only_then():
    manager = SimpleNamespace(_qualification_q1_status="ready_to_close")
    allowed = captains_table_quota_safe._safe_panel_actions(manager, "active")
    assert "Close Current Round" in allowed


def test_safe_fallback_prunes_mega_view_and_applies_friendly_labels():
    manager = SimpleNamespace(_qualification_q1_status="active")
    allowed = captains_table_quota_safe._safe_panel_actions(manager, "active")
    view = _view_with_labels(
        [
            "Open Registration",
            "Complete Tournament",
            "Close Current Round",
            "Review Result Issues",
            "Repair Discord State",
            "View Standings",
            "Approve & Publish Swiss",
            "Freeze Top 8",
            "Competition Ops",
            "View Roster",
        ]
    )

    visible = full_set_scoring._finalize_visible_view(view, allowed)
    labels = [item.label for item in visible.children]

    assert labels == [
        "Review Match Issues",
        "Repair Tournament",
        "View Standings",
        "Organizer Actions",
        "View Players",
    ]
    assert "Finish Tournament" not in labels
    assert "Publish Next Round" not in labels
    assert "Lock Top 8" not in labels
