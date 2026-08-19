import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from modules.community.live_arena import panel, reconciliation_hardening as hardening
from modules.community.live_arena import round_overview, runtime_hooks, victory_ledger_workspace


class _Service:
    def __init__(self, tournament_status):
        self.sheet_id = "sheet"
        self.registration_repository = object()
        self._tournament_status = tournament_status

    async def context(self):
        return (
            {},
            (2, {"tournament_id": "T", "status": self._tournament_status}),
            [],
            [],
        )


def _snapshot(*, round_status="closed", match_status="finalized"):
    return SimpleNamespace(
        status=round_status,
        round_row={
            "tournament_id": "T",
            "round_id": "T-F",
            "round_stage": "final",
            "status": round_status,
        },
        matches=(
            {
                "match_id": "T-F-M01",
                "match_number": "1",
                "thread_id": "123",
                "status": match_status,
            },
        ),
    )


def _patch_overview(monkeypatch):
    workspace = SimpleNamespace(parent=SimpleNamespace(guild=SimpleNamespace(id=99)))
    ensure = AsyncMock(return_value=workspace)
    render = AsyncMock(return_value=["embed"])
    sync = AsyncMock()
    monkeypatch.setattr(victory_ledger_workspace, "ensure_workspace", ensure)
    monkeypatch.setattr(round_overview, "render_round_overview_embeds", render)
    monkeypatch.setattr(victory_ledger_workspace, "sync_round_overview", sync)
    return ensure, render, sync


@pytest.mark.parametrize("tournament_status", ["completed", "archived"])
def test_terminal_tournament_skips_live_result_controls_but_keeps_history_sync(
    monkeypatch, tournament_status
):
    _ensure, _render, sync = _patch_overview(monkeypatch)
    ensure_result_view = AsyncMock()
    monkeypatch.setattr(runtime_hooks, "_ensure_match_result_view", ensure_result_view)

    bot = SimpleNamespace(
        get_channel=Mock(side_effect=AssertionError("terminal history must not resolve match threads")),
        fetch_channel=AsyncMock(side_effect=AssertionError("terminal history must not fetch match threads")),
    )

    warnings = asyncio.run(
        hardening._sync_round_discord(bot, _Service(tournament_status), _snapshot())
    )

    assert warnings == []
    ensure_result_view.assert_not_awaited()
    sync.assert_awaited_once()


def test_active_tournament_result_control_failure_is_actionable_and_returned(
    monkeypatch, caplog
):
    _patch_overview(monkeypatch)
    ensure_result_view = AsyncMock(
        side_effect=RuntimeError("starter message cannot be edited")
    )
    monkeypatch.setattr(runtime_hooks, "_ensure_match_result_view", ensure_result_view)
    bot = SimpleNamespace(get_channel=Mock(return_value=object()), fetch_channel=AsyncMock())

    caplog.set_level(logging.ERROR, logger="c1c.community.live_arena.reconciliation_hardening")
    warnings = asyncio.run(
        hardening._sync_round_discord(
            bot,
            _Service("active"),
            _snapshot(round_status="active", match_status="published"),
        )
    )

    assert warnings == ["Match 1 result controls"]
    ensure_result_view.assert_awaited_once()
    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "tournament=T" in text
    assert "tournament_status=active" in text
    assert "round=T-F" in text
    assert "match=T-F-M01" in text
    assert "thread=123" in text
    assert "RuntimeError: starter message cannot be edited" in text


def test_startup_health_line_includes_stage_reconciliation_warnings(monkeypatch, caplog):
    monkeypatch.setattr(panel, "_STARTUP_SYNC_DELAY_SECONDS", 0)
    monkeypatch.setattr(panel, "_STARTUP_MAX_ATTEMPTS", 1)

    manager = SimpleNamespace(sync=AsyncMock(return_value=panel.PanelSyncResult(True)))
    organizer = SimpleNamespace(
        sync=AsyncMock(return_value=panel.PanelSyncResult(True)),
        _competition_sync=AsyncMock(return_value=["Victory Ledger overview"]),
    )
    refresh = AsyncMock()
    fallback = AsyncMock()

    caplog.set_level(logging.INFO, logger="c1c.community.live_arena.reconciliation_hardening")
    asyncio.run(
        hardening._run_startup_sync(
            manager,
            organizer,
            True,
            refresh,
            fallback,
        )
    )

    health = [
        record.getMessage()
        for record in caplog.records
        if "Live Arena startup reconciliation finished" in record.getMessage()
    ]
    assert len(health) == 1
    assert "warnings=Victory Ledger overview" in health[0]
    organizer._competition_sync.assert_awaited_once()
    fallback.assert_not_awaited()
