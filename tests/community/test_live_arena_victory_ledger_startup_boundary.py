import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from modules.community.live_arena import victory_ledger_workspace_fallback as fallback


def test_final_register_boundary_schedules_workspace_without_qualification_side_effect(monkeypatch):
    organizer = SimpleNamespace(sheet_id="sheet-abcdef", bot=object())
    manager = SimpleNamespace(organizer_manager=organizer)
    bot = object()
    scheduled = []

    async def original_register(received_bot):
        assert received_bot is bot
        return manager

    def schedule(workspace, received_manager, **_kwargs):
        scheduled.append((workspace, received_manager))
        return None

    workspace = object()
    monkeypatch.setattr(fallback, "_schedule_workspace_reconcile", schedule)

    result = asyncio.run(
        fallback._register_and_schedule_workspace(original_register, workspace, bot)
    )

    assert result is manager
    assert scheduled == [(workspace, organizer)]


def test_startup_scheduler_is_observable_and_runs_reconciliation(monkeypatch, caplog):
    async def scenario():
        workspace = SimpleNamespace(
            _reconcile_tasks={},
            reconcile_workspace=AsyncMock(),
        )
        manager = SimpleNamespace(sheet_id="sheet-abcdef")

        async def no_delay(_seconds):
            return None

        monkeypatch.setattr(fallback.asyncio, "sleep", no_delay)
        caplog.set_level(logging.INFO, logger=fallback.log.name)

        task = fallback._schedule_workspace_reconcile(
            workspace, manager, delay_seconds=240
        )
        assert task is not None
        assert workspace._reconcile_tasks[manager.sheet_id] is task

        await task

        workspace.reconcile_workspace.assert_awaited_once_with(manager)
        assert manager.sheet_id not in workspace._reconcile_tasks

    asyncio.run(scenario())

    messages = [record.getMessage() for record in caplog.records]
    assert any("startup migration scheduled" in message for message in messages)
    assert any("startup migration started" in message for message in messages)
    assert any("startup migration completed" in message for message in messages)


def test_startup_scheduler_deduplicates_existing_task(monkeypatch):
    async def scenario():
        blocker = asyncio.Event()
        workspace = SimpleNamespace(
            _reconcile_tasks={},
            reconcile_workspace=AsyncMock(),
        )
        manager = SimpleNamespace(sheet_id="sheet-abcdef")

        async def blocked_sleep(_seconds):
            await blocker.wait()

        monkeypatch.setattr(fallback.asyncio, "sleep", blocked_sleep)

        first = fallback._schedule_workspace_reconcile(
            workspace, manager, delay_seconds=240
        )
        second = fallback._schedule_workspace_reconcile(
            workspace, manager, delay_seconds=240
        )

        assert first is second
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)

    asyncio.run(scenario())


def test_missing_organizer_does_not_break_live_arena_registration(monkeypatch):
    manager = SimpleNamespace()
    scheduled = []

    async def original_register(_bot):
        return manager

    monkeypatch.setattr(
        fallback,
        "_schedule_workspace_reconcile",
        lambda *_args, **_kwargs: scheduled.append(True),
    )

    result = asyncio.run(
        fallback._register_and_schedule_workspace(original_register, object(), object())
    )

    assert result is manager
    assert scheduled == []
