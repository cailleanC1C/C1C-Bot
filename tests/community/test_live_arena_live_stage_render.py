from types import SimpleNamespace

import pytest

from modules.community.live_arena import live_stage_render


@pytest.mark.asyncio
async def test_hydrate_existing_q2_preview_before_final_panel_render(monkeypatch):
    manager = SimpleNamespace(sheet_id="sheet")
    snapshot = SimpleNamespace(
        status="preview",
        round_row={
            "round_id": "T-Q2",
            "round_number": "2",
            "status": "preview",
            "generated_at_utc": "2026-08-14T20:00:00Z",
        },
    )
    service = object()
    calls = []

    async def current(_manager):
        return service, snapshot, 2

    def cache(_manager, _snapshot):
        _manager._captains_table_swiss_preview_round = 2
        _manager._captains_table_swiss_preview_status = "preview"
        calls.append("cache")
        return True

    async def sync_preview(_manager, _service, _snapshot):
        assert _manager._captains_table_swiss_preview_round == 2
        calls.append("preview")

    async def refresh_previous(_manager, number):
        assert number == 2
        calls.append("previous")

    monkeypatch.setattr(live_stage_render, "_current_swiss_preview", current)

    from modules.community.live_arena import swiss_progression_controls, swiss_runtime

    monkeypatch.setattr(swiss_progression_controls, "_cache_preview_state", cache)
    monkeypatch.setattr(swiss_runtime, "_sync_preview_message", sync_preview)
    monkeypatch.setattr(
        swiss_progression_controls,
        "_refresh_previous_closed_overview",
        refresh_previous,
    )

    await live_stage_render._hydrate_live_swiss_stage(manager)

    assert calls == ["cache", "preview", "previous"]
    assert manager._live_stage_render_signature == (
        "T-Q2",
        "preview",
        "2026-08-14T20:00:00Z",
    )


@pytest.mark.asyncio
async def test_hydrate_same_preview_does_not_reedit_discord(monkeypatch):
    manager = SimpleNamespace(
        sheet_id="sheet",
        _live_stage_render_signature=(
            "T-Q3",
            "approved",
            "2026-08-14T21:00:00Z",
        ),
    )
    snapshot = SimpleNamespace(
        status="approved",
        round_row={
            "round_id": "T-Q3",
            "round_number": "3",
            "status": "approved",
            "generated_at_utc": "2026-08-14T21:00:00Z",
        },
    )

    async def current(_manager):
        return object(), snapshot, 3

    monkeypatch.setattr(live_stage_render, "_current_swiss_preview", current)

    from modules.community.live_arena import swiss_progression_controls, swiss_runtime

    cached = []
    monkeypatch.setattr(
        swiss_progression_controls,
        "_cache_preview_state",
        lambda *_args: cached.append(True) or False,
    )

    async def should_not_run(*_args, **_kwargs):
        raise AssertionError("unchanged preview should not re-edit Discord")

    monkeypatch.setattr(swiss_runtime, "_sync_preview_message", should_not_run)
    monkeypatch.setattr(
        swiss_progression_controls,
        "_refresh_previous_closed_overview",
        should_not_run,
    )

    await live_stage_render._hydrate_live_swiss_stage(manager)
    assert cached == [True]


@pytest.mark.asyncio
async def test_hydrate_without_preview_clears_stale_q2_q3_state(monkeypatch):
    manager = SimpleNamespace(
        sheet_id="sheet",
        _live_stage_render_signature=("old", "preview", "old"),
    )

    async def current(_manager):
        return object(), None, None

    monkeypatch.setattr(live_stage_render, "_current_swiss_preview", current)

    from modules.community.live_arena import swiss_progression_controls

    cleared = []
    monkeypatch.setattr(
        swiss_progression_controls,
        "_clear_preview_state",
        lambda _manager, number: cleared.append(number) or True,
    )

    await live_stage_render._hydrate_live_swiss_stage(manager)

    assert cleared == [2, 3]
    assert manager._live_stage_render_signature is None
