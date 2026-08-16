from __future__ import annotations

from types import SimpleNamespace

import pytest

from modules.community.live_arena import captains_table_control_center as control
from modules.community.live_arena import runtime_hooks
from modules.community.live_arena import tiebreak_completion_boundary as boundary


@pytest.mark.asyncio
async def test_result_mutation_sync_reconciles_tiebreak_and_captains_table(monkeypatch):
    """A finalized tiebreak must advance the organizer UI in the same result sync."""
    events = []
    state = SimpleNamespace(
        tournament_id="LA-TEST",
        tiebreak_required=True,
        tiebreak_complete=True,
        tiebreak_resolved=True,
    )

    async def base_sync(manager):
        events.append(("competition", manager.sheet_id))
        return ["existing warning"]

    async def ensure(manager):
        events.append(("tiebreak", manager.sheet_id))
        return state

    async def render(manager, rendered_state):
        events.append(("captains_table", manager.sheet_id))
        assert rendered_state is state

    monkeypatch.setattr(runtime_hooks, "_sync_manager_competition", base_sync)
    monkeypatch.setattr(control, "_ensure_tiebreak_flow", ensure)
    monkeypatch.setattr(control, "_render_control_center", render)
    monkeypatch.setattr(boundary, "_installed", False)

    boundary.install()
    manager = SimpleNamespace(sheet_id="sheet")
    warnings = await runtime_hooks._sync_manager_competition(manager)

    assert warnings == ["existing warning"]
    assert events == [
        ("competition", "sheet"),
        ("tiebreak", "sheet"),
        ("captains_table", "sheet"),
    ]


@pytest.mark.asyncio
async def test_tiebreak_refresh_failure_is_best_effort_and_does_not_lose_result_sync(monkeypatch):
    async def ensure(_manager):
        raise RuntimeError("temporary Discord failure")

    monkeypatch.setattr(control, "_ensure_tiebreak_flow", ensure)

    warnings = await boundary._sync_tiebreak_after_result(SimpleNamespace(sheet_id="sheet"))

    assert warnings == ["qualification tiebreak / Captain's Table"]
