import asyncio
from types import SimpleNamespace

from modules.community.live_arena import round_finish_refresh


def test_ready_to_close_refreshes_captains_table(monkeypatch):
    calls = {"base": 0, "refresh": 0, "panel": 0}

    async def base_sync(_manager):
        calls["base"] += 1
        return []

    async def refresh_state(manager):
        calls["refresh"] += 1
        manager._qualification_q1_status = "ready_to_close"
        return SimpleNamespace(status="ready_to_close")

    async def panel_sync():
        calls["panel"] += 1
        return SimpleNamespace(ok=True)

    from modules.community.live_arena import qualification_panel

    monkeypatch.setattr(
        qualification_panel, "refresh_qualification_state", refresh_state
    )
    manager = SimpleNamespace(
        _qualification_q1_status="active",
        sync=panel_sync,
    )

    warnings = asyncio.run(
        round_finish_refresh._sync_competition_and_maybe_panel(manager, base_sync)
    )

    assert warnings == []
    assert calls == {"base": 1, "refresh": 1, "panel": 1}
    assert manager._qualification_q1_status == "ready_to_close"


def test_unchanged_active_round_does_not_spend_panel_edit(monkeypatch):
    calls = {"panel": 0}

    async def base_sync(_manager):
        return []

    async def refresh_state(manager):
        manager._qualification_q1_status = "active"
        return SimpleNamespace(status="active")

    async def panel_sync():
        calls["panel"] += 1
        return SimpleNamespace(ok=True)

    from modules.community.live_arena import qualification_panel

    monkeypatch.setattr(
        qualification_panel, "refresh_qualification_state", refresh_state
    )
    manager = SimpleNamespace(
        _qualification_q1_status="active",
        sync=panel_sync,
    )

    warnings = asyncio.run(
        round_finish_refresh._sync_competition_and_maybe_panel(manager, base_sync)
    )

    assert warnings == []
    assert calls["panel"] == 0


def test_closable_round_refresh_failure_is_reported(monkeypatch):
    async def base_sync(_manager):
        return ["existing warning"]

    async def refresh_state(manager):
        manager._qualification_q1_status = "ready_to_close"
        return SimpleNamespace(status="ready_to_close")

    async def panel_sync():
        return SimpleNamespace(ok=False)

    from modules.community.live_arena import qualification_panel

    monkeypatch.setattr(
        qualification_panel, "refresh_qualification_state", refresh_state
    )
    manager = SimpleNamespace(
        _qualification_q1_status="active",
        sync=panel_sync,
    )

    warnings = asyncio.run(
        round_finish_refresh._sync_competition_and_maybe_panel(manager, base_sync)
    )

    assert warnings == ["existing warning", "organizer panel"]
