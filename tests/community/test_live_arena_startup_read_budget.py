from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from modules.community.live_arena import panel as panel_module
from modules.community.live_arena.panel import PanelSyncResult
from shared.sheets import async_core


def run(awaitable):
    return asyncio.run(awaitable)


def test_sheet_read_scope_deduplicates_same_tab_sequentially_and_concurrently():
    raw = AsyncMock(return_value=[["header"], ["value"]])

    async def scenario():
        with patch.object(async_core._core, "afetch_values", raw):
            with async_core.sheet_read_scope() as scope:
                first = await async_core.afetch_values("sheet", "CONFIG")
                second, third = await asyncio.gather(
                    async_core.afetch_values("sheet", "CONFIG"),
                    async_core.afetch_values("sheet", "CONFIG"),
                )
                other = await async_core.afetch_values("sheet", "MESSAGES")
        return first, second, third, other, scope

    first, second, third, other, scope = run(scenario())

    assert first == second == third == [["header"], ["value"]]
    assert other == [["header"], ["value"]]
    assert raw.await_count == 2
    assert scope.misses == 2
    assert scope.hits == 2


def test_live_arena_startup_refresh_reuses_identical_sheet_reads(monkeypatch):
    raw = AsyncMock(return_value=[["header"], ["value"]])

    async def shared_read(*_args, **_kwargs):
        await async_core.afetch_values("sheet", "CONFIG")

    manager = SimpleNamespace(sync=AsyncMock(side_effect=shared_read))
    organizer = SimpleNamespace(sync=AsyncMock(side_effect=shared_read))
    refresh_q = AsyncMock(side_effect=shared_read)

    async def reconcile(_organizer):
        await shared_read()
        return []

    monkeypatch.setattr(panel_module, "_STARTUP_SYNC_DELAY_SECONDS", 0)
    monkeypatch.setattr(panel_module, "_STARTUP_RETRY_DELAY_SECONDS", 0)

    with patch.object(async_core._core, "afetch_values", raw):
        run(
            panel_module._run_startup_sync(
                manager,
                organizer,
                True,
                refresh_q,
                reconcile,
            )
        )

    assert raw.await_count == 1
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
