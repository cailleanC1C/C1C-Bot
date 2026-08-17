from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from modules.community.live_arena import panel as panel_module
from modules.community.live_arena.panel import PanelSyncResult
from shared.sheets import async_core
from shared.sheets.read_broker import SheetsReadBroker


def run(awaitable):
    return asyncio.run(awaitable)


def _new_broker() -> SheetsReadBroker:
    return SheetsReadBroker(
        read_budget_rpm=1_000_000,
        rate_window_seconds=0.001,
        retry_attempts=1,
        retry_base_delay_sec=0,
    )


def _prime_sheet_handles(*tabs: str) -> None:
    """Avoid credential/metadata setup so tests count physical value reads only."""

    async_core._core._WorkbookCache.clear()
    async_core._core._WorksheetCache.clear()
    async_core._core._WorkbookCache["sheet"] = object()
    for tab in tabs:
        async_core._core._WorksheetCache[("sheet", tab)] = object()


def test_sheet_read_scope_deduplicates_same_tab_sequentially_and_concurrently(monkeypatch):
    raw = AsyncMock(return_value=[["header"], ["value"]])

    async def scenario():
        test_broker = _new_broker()
        monkeypatch.setattr(async_core, "broker", test_broker)
        _prime_sheet_handles("CONFIG", "MESSAGES")
        monkeypatch.setattr(
            async_core._core.async_adapter,
            "aworksheet_values_all",
            raw,
        )
        try:
            with async_core.sheet_read_scope() as scope:
                first = await async_core.afetch_values("sheet", "CONFIG")
                second, third = await asyncio.gather(
                    async_core.afetch_values("sheet", "CONFIG"),
                    async_core.afetch_values("sheet", "CONFIG"),
                )
                other = await async_core.afetch_values("sheet", "MESSAGES")
            return first, second, third, other, scope
        finally:
            await test_broker.close()
            async_core._core._WorkbookCache.clear()
            async_core._core._WorksheetCache.clear()

    first, second, third, other, scope = run(scenario())

    assert first == second == third == [["header"], ["value"]]
    assert other == [["header"], ["value"]]
    assert raw.await_count == 2
    assert scope.misses == 2
    assert scope.hits == 2


def test_live_arena_startup_reuses_read_only_panel_reads(monkeypatch):
    raw = AsyncMock(return_value=[["header"], ["value"]])

    async def shared_read(*_args, **_kwargs):
        await async_core.afetch_values("sheet", "CONFIG")

    manager = SimpleNamespace(sync=AsyncMock(side_effect=shared_read))
    organizer = SimpleNamespace(sync=AsyncMock(side_effect=shared_read))
    refresh_q = AsyncMock(side_effect=shared_read)

    async def reconcile(_organizer):
        # Reconciliation is deliberately outside the scope because production
        # reconciliation may write ROUNDS/MATCHES thread and overview IDs.
        await shared_read()
        return []

    monkeypatch.setattr(panel_module, "_STARTUP_SYNC_DELAY_SECONDS", 0)
    monkeypatch.setattr(panel_module, "_STARTUP_RETRY_DELAY_SECONDS", 0)

    async def scenario():
        test_broker = _new_broker()
        monkeypatch.setattr(async_core, "broker", test_broker)
        _prime_sheet_handles("CONFIG")
        monkeypatch.setattr(
            async_core._core.async_adapter,
            "aworksheet_values_all",
            raw,
        )
        try:
            await panel_module._run_startup_sync(
                manager,
                organizer,
                True,
                refresh_q,
                reconcile,
            )
        finally:
            await test_broker.close()
            async_core._core._WorkbookCache.clear()
            async_core._core._WorksheetCache.clear()

    run(scenario())

    # qualification-state + public-panel + organizer-panel all reuse one read;
    # the write-capable reconciliation phase performs its own fresh read.
    assert raw.await_count == 2
    refresh_q.assert_awaited_once()
    manager.sync.assert_awaited_once()
    organizer.sync.assert_awaited_once()


def test_live_arena_startup_quota_error_retries_after_deferral(monkeypatch):
    manager = SimpleNamespace(
        sync=AsyncMock(
            side_effect=[
                RuntimeError("429 RESOURCE_EXHAUSTED ReadRequestsPerMinutePerUser"),
                PanelSyncResult(True),
            ]
        )
    )
    organizer = SimpleNamespace(sync=AsyncMock(return_value=PanelSyncResult(True)))

    monkeypatch.setattr(panel_module, "_STARTUP_SYNC_DELAY_SECONDS", 0)
    monkeypatch.setattr(panel_module, "_STARTUP_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(panel_module, "_STARTUP_MAX_ATTEMPTS", 2)

    run(
        panel_module._run_startup_sync(
            manager,
            organizer,
            False,
            AsyncMock(),
            AsyncMock(),
        )
    )

    assert manager.sync.await_count == 2
    organizer.sync.assert_awaited_once()
