from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from modules.community.live_arena import (
    qualification_panel,
    simulation_ux_finalizer,
    swiss_progression_controls as progression,
    swiss_runtime,
    tournament_lifecycle,
)
from modules.community.live_arena.organizer_panel import OrganizerPanelManager
from modules.community.live_arena.panel import PanelSyncResult


def run(awaitable):
    return asyncio.run(awaitable)


def _snapshot(number: int, status: str = "preview"):
    return SimpleNamespace(
        round_row={"round_number": str(number), "status": status},
        matches=(),
    )


@pytest.mark.parametrize("number", [2, 3])
def test_final_captains_table_render_reconciles_swiss_before_quota_fallback(
    monkeypatch, number
):
    """The deployed final manager.sync path must seed Q2/Q3 controls itself."""
    manager = OrganizerPanelManager(SimpleNamespace(), f"sheet-final-q{number}", SimpleNamespace())
    assert qualification_panel.install_qualification(manager) is True
    manager._qualification_q1_status = "closed"

    calls = []

    async def reconcile_preview(current_manager):
        calls.append("reconcile")
        progression._cache_preview_state(current_manager, _snapshot(number))

    async def quota_failure(_manager):
        calls.append("allowed")
        raise RuntimeError("429 RESOURCE_EXHAUSTED ReadRequestsPerMinutePerUser")

    async def render(current_manager):
        calls.append("render")
        labels = {item.label for item in current_manager.view("active").children}
        assert f"Publish Qualification Round {number}" in labels
        assert f"Redo Qualification Round {number}" in labels
        assert "Finish Round" not in labels
        return PanelSyncResult(True)

    monkeypatch.setattr(swiss_runtime, "_reconcile_preview", reconcile_preview)
    monkeypatch.setattr(simulation_ux_finalizer, "_allowed_panel_actions", quota_failure)
    monkeypatch.setattr(tournament_lifecycle, "_sync_organizer_panel", render)

    result = run(manager.sync())

    assert result == PanelSyncResult(True)
    assert calls == ["reconcile", "allowed", "render"]
    assert manager._captains_table_stage_reconciling is False


def test_legacy_zero_win_float_wording_is_normalized_from_full_record_strength():
    match = {
        "player_a_display_name": "Atlantic5penguin",
        "player_b_display_name": "OtherPlayer",
        "notes": (
            "Swiss adjacent-group float · Atlantic5penguin (0-1) floated down to 0-0 · "
            "no-rematch constraint preserved"
        ),
    }

    fixed = progression._normalize_float_rationale(match)

    assert fixed == (
        "Swiss adjacent-group float · OtherPlayer (0-0) floated down to 0-1 · "
        "no-rematch constraint preserved"
    )


def test_correct_float_wording_is_left_unchanged():
    match = {
        "player_a_display_name": "Stronger",
        "player_b_display_name": "Weaker",
        "notes": (
            "Swiss adjacent-group float · Stronger (1-0) floated down to 0-1 · "
            "no-rematch constraint preserved"
        ),
    }

    assert progression._normalize_float_rationale(match) == match["notes"]
