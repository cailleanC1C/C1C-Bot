import asyncio
from types import SimpleNamespace

from modules.community.live_arena import knockout_progression_finalizer
from modules.community.live_arena import round_finish_refresh


TID = "LA-TEST"


def _install_round_state(monkeypatch, rounds):
    from modules.community.live_arena import competition_resolution

    class Repository:
        async def rounds(self):
            return [dict(row) for row in rounds]

    class Service:
        def __init__(self, sheet_id):
            self.sheet_id = sheet_id
            self.repository = Repository()

        async def initialize(self):
            return None

    async def load_config(_sheet_id):
        return {"ACTIVE_TOURNAMENT_ID": TID}

    monkeypatch.setattr(
        competition_resolution,
        "CompetitionResolutionService",
        Service,
    )
    monkeypatch.setattr(
        knockout_progression_finalizer,
        "load_config",
        load_config,
    )


def test_ready_to_close_refreshes_captains_table_for_any_stage(monkeypatch):
    calls = {"base": 0, "panel": 0}

    async def base_sync(_manager):
        calls["base"] += 1
        return []

    async def panel_sync():
        calls["panel"] += 1
        return SimpleNamespace(ok=True)

    _install_round_state(
        monkeypatch,
        [
            {
                "tournament_id": TID,
                "round_id": f"{TID}-F",
                "round_stage": "final",
                "round_number": "6",
                "status": "ready_to_close",
            }
        ],
    )
    manager = SimpleNamespace(sheet_id="sheet", sync=panel_sync)

    warnings = asyncio.run(
        round_finish_refresh._sync_competition_and_maybe_panel(manager, base_sync)
    )

    assert warnings == []
    assert calls == {"base": 1, "panel": 1}


def test_unchanged_active_round_does_not_spend_panel_edit(monkeypatch):
    calls = {"panel": 0}

    async def base_sync(_manager):
        return []

    async def panel_sync():
        calls["panel"] += 1
        return SimpleNamespace(ok=True)

    _install_round_state(
        monkeypatch,
        [
            {
                "tournament_id": TID,
                "round_id": f"{TID}-QF",
                "round_stage": "quarterfinal",
                "round_number": "4",
                "status": "open",
            }
        ],
    )
    manager = SimpleNamespace(sheet_id="sheet", sync=panel_sync)

    warnings = asyncio.run(
        round_finish_refresh._sync_competition_and_maybe_panel(manager, base_sync)
    )

    assert warnings == []
    assert calls["panel"] == 0


def test_closable_round_refresh_failure_is_reported(monkeypatch):
    async def base_sync(_manager):
        return ["existing warning"]

    async def panel_sync():
        return SimpleNamespace(ok=False)

    _install_round_state(
        monkeypatch,
        [
            {
                "tournament_id": TID,
                "round_id": f"{TID}-SF",
                "round_stage": "semifinal",
                "round_number": "5",
                "status": "correction_in_progress",
            }
        ],
    )
    manager = SimpleNamespace(sheet_id="sheet", sync=panel_sync)

    warnings = asyncio.run(
        round_finish_refresh._sync_competition_and_maybe_panel(manager, base_sync)
    )

    assert warnings == ["existing warning", "organizer panel"]
