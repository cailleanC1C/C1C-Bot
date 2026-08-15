from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from modules.community.live_arena import panel as panel_module
from modules.community.live_arena.panel import PanelSyncResult


def run(awaitable):
    return asyncio.run(awaitable)


def test_startup_reconciles_active_q2_through_stage_aware_competition_sync(monkeypatch):
    """Regression: a redeploy during active Q2 must touch Swiss Discord state."""
    events: list[str] = []

    async def refresh_q(_organizer):
        events.append("refresh qualification state")

    async def sync_public():
        events.append("public panel")
        return PanelSyncResult(True)

    async def sync_organizer():
        events.append("organizer panel")
        return PanelSyncResult(True)

    async def sync_active_q2():
        # In the real decorated manager this is swiss_runtime's Q2/Q3-aware
        # _competition_sync chain, which reaches SwissPublisher.reconcile() and
        # the matchup-thread rerender installed by PR #1119.
        events.append("active Q2 competition Discord")
        return []

    manager = SimpleNamespace(sync=AsyncMock(side_effect=sync_public))
    organizer = SimpleNamespace(
        sync=AsyncMock(side_effect=sync_organizer),
        _competition_sync=AsyncMock(side_effect=sync_active_q2),
    )
    legacy_q1_reconcile = AsyncMock(return_value=[])

    monkeypatch.setattr(panel_module, "_STARTUP_SYNC_DELAY_SECONDS", 0)
    monkeypatch.setattr(panel_module, "_STARTUP_RETRY_DELAY_SECONDS", 0)

    run(
        panel_module._run_startup_sync(
            manager,
            organizer,
            True,
            AsyncMock(side_effect=refresh_q),
            legacy_q1_reconcile,
        )
    )

    assert events == [
        "refresh qualification state",
        "public panel",
        "organizer panel",
        "active Q2 competition Discord",
    ]
    organizer._competition_sync.assert_awaited_once_with()
    legacy_q1_reconcile.assert_not_awaited()


def test_startup_keeps_qualification_publication_as_compatibility_fallback(monkeypatch):
    manager = SimpleNamespace(sync=AsyncMock(return_value=PanelSyncResult(True)))
    organizer = SimpleNamespace(sync=AsyncMock(return_value=PanelSyncResult(True)))
    legacy_q1_reconcile = AsyncMock(return_value=[])

    monkeypatch.setattr(panel_module, "_STARTUP_SYNC_DELAY_SECONDS", 0)
    monkeypatch.setattr(panel_module, "_STARTUP_RETRY_DELAY_SECONDS", 0)

    run(
        panel_module._run_startup_sync(
            manager,
            organizer,
            True,
            AsyncMock(),
            legacy_q1_reconcile,
        )
    )

    legacy_q1_reconcile.assert_awaited_once_with(organizer)
