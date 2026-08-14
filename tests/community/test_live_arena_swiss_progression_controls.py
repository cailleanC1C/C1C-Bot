from types import SimpleNamespace

import discord

from modules.community.live_arena import swiss_progression_controls as progression


def _snapshot(number: int, status: str = "preview"):
    return SimpleNamespace(round_row={"round_number": str(number), "status": status})


def test_cache_preview_state_covers_q2_and_q3():
    manager = SimpleNamespace()

    assert progression._cache_preview_state(manager, _snapshot(2)) is True
    assert manager._captains_table_swiss_preview_round == 2
    assert manager._captains_table_swiss_preview_status == "preview"

    assert progression._cache_preview_state(manager, _snapshot(2)) is False
    assert progression._cache_preview_state(manager, _snapshot(3, "approved")) is True
    assert manager._captains_table_swiss_preview_round == 3
    assert manager._captains_table_swiss_preview_status == "approved"


def test_quota_fallback_exposes_publish_and_redo_for_swiss_preview():
    manager = SimpleNamespace(
        _captains_table_swiss_preview_round=2,
        _captains_table_swiss_preview_status="preview",
    )

    actions = progression._swiss_fallback_actions(manager, "active", {"View Roster"})

    assert "Approve & Publish Swiss" in actions
    assert "Regenerate Swiss Preview" in actions
    assert "View Standings" in actions
    assert "Close Current Round" not in actions


def test_quota_fallback_does_not_leak_preview_controls_when_not_active():
    manager = SimpleNamespace(
        _captains_table_swiss_preview_round=2,
        _captains_table_swiss_preview_status="preview",
    )
    base = {"View Roster"}

    assert progression._swiss_fallback_actions(manager, "completed", base) == base


def test_dynamic_labels_name_the_actual_qualification_round():
    manager = SimpleNamespace(_captains_table_swiss_preview_round=2)
    view = discord.ui.View(timeout=None)
    publish = discord.ui.Button(label="Publish Next Round")
    publish.manager = manager
    redo = discord.ui.Button(label="Redo Next Round")
    redo.manager = manager
    view.add_item(publish)
    view.add_item(redo)

    progression._apply_dynamic_labels(view, manager)

    labels = {item.label for item in view.children}
    assert "Publish Qualification Round 2" in labels
    assert "Redo Qualification Round 2" in labels


def test_clear_preview_state_removes_stale_actions_after_publication():
    manager = SimpleNamespace(
        _captains_table_swiss_preview_round=3,
        _captains_table_swiss_preview_status="approved",
    )

    assert progression._clear_preview_state(manager, 3) is True
    assert manager._captains_table_swiss_preview_round is None
    assert manager._captains_table_swiss_preview_status == ""
