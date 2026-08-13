from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from modules.community.live_arena import hall_of_fame, organizer_panel
from modules.community.live_arena import sheets_read_hardening as hardening
from shared.sheets import async_core


def run(awaitable):
    return asyncio.run(awaitable)


def test_transition_deduplicates_reads_but_refreshes_after_write(monkeypatch):
    raw = AsyncMock(side_effect=[[["before"]], [["after"]]])

    class Service:
        def __init__(self, sheet_id):
            self.sheet_id = sheet_id

        async def initialize(self):
            await async_core.afetch_values(self.sheet_id, "CONFIG")
            await async_core.afetch_values(self.sheet_id, "CONFIG")

        async def transition(self, action, actor_id):
            assert action == "close"
            assert actor_id == "42"
            await async_core.afetch_values(self.sheet_id, "CONFIG")

    async def secondary_sync():
        first = await async_core.afetch_values("sheet", "CONFIG")
        second = await async_core.afetch_values("sheet", "CONFIG")
        assert first == second == [["after"]]
        return []

    manager = SimpleNamespace(sheet_id="sheet", secondary_sync=secondary_sync)
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=42),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    monkeypatch.setattr(organizer_panel, "OrganizerService", Service)
    with patch.object(async_core._core, "afetch_values", raw):
        run(hardening._budgeted_execute_transition(interaction, manager, "close"))

    # One physical CONFIG read before the mutation, then one fresh physical read for
    # the post-write panel refresh. The second scope must not reuse pre-write state.
    assert raw.await_count == 2
    interaction.followup.send.assert_awaited_once()
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert embed.title == "Registration updated"
    assert "successfully closed" in embed.description


def test_hall_of_fame_startup_sync_reuses_identical_reads(monkeypatch):
    raw = AsyncMock(return_value=[["header"], ["value"]])

    async def sync(_manager):
        await async_core.afetch_values("sheet", "CONFIG")
        await async_core.afetch_values("sheet", "CONFIG")

    monkeypatch.setattr(hardening, "_HALL_OF_FAME_STARTUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(hall_of_fame, "sync_hall_of_fame", sync)

    with patch.object(async_core._core, "afetch_values", raw):
        run(hardening._run_hall_of_fame_startup_sync(SimpleNamespace(sheet_id="sheet")))

    assert raw.await_count == 1


def test_hall_of_fame_quota_retry_is_bounded(monkeypatch):
    attempts = 0

    async def sync(_manager):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("429 RESOURCE_EXHAUSTED ReadRequestsPerMinutePerUser")

    monkeypatch.setattr(hardening, "_HALL_OF_FAME_STARTUP_DELAY_SECONDS", 0)
    monkeypatch.setattr(hardening, "_HALL_OF_FAME_RETRY_DELAY_SECONDS", 0)
    monkeypatch.setattr(hardening, "_HALL_OF_FAME_MAX_ATTEMPTS", 2)
    monkeypatch.setattr(hall_of_fame, "sync_hall_of_fame", sync)

    run(hardening._run_hall_of_fame_startup_sync(SimpleNamespace(sheet_id="sheet")))

    assert attempts == 2
