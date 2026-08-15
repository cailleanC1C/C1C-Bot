from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from modules.community.live_arena import result_control_refresh as refresh


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture(autouse=True)
def reset_mutation_channel():
    refresh._mutation_channel.set(None)
    yield
    refresh._mutation_channel.set(None)


def _disabled_by_custom_id(view):
    return {
        getattr(item, "custom_id", ""): bool(getattr(item, "disabled", False))
        for item in view.children
        if getattr(item, "custom_id", "")
    }


def test_pending_confirmation_enables_dispute_and_disables_report_and_scheduling(monkeypatch):
    starter = SimpleNamespace(edit=AsyncMock())
    thread = SimpleNamespace(
        id=1538130704028672043,
        get_partial_message=Mock(return_value=starter),
    )

    class FakeCompetitionResolutionService:
        def __init__(self, sheet_id):
            assert sheet_id == "sheet-live"

        async def initialize(self):
            return None

        async def match_for_thread(self, thread_id):
            assert thread_id == "1538130704028672043"
            return {
                "match_id": "LA-2026-TRIAL-01-Q2-M03",
                "status": "pending_confirmation",
                "reported_by_discord_user_id": "1050998068943781928",
                "player_a_discord_user_id": "1050998068943781928",
                "player_b_discord_user_id": "728674284603572327",
            }

    monkeypatch.setattr(
        refresh,
        "CompetitionResolutionService",
        FakeCompetitionResolutionService,
    )

    run(refresh._refresh_channel_controls(thread, "sheet-live"))

    starter.edit.assert_awaited_once()
    view = starter.edit.await_args.kwargs["view"]
    disabled = _disabled_by_custom_id(view)
    assert disabled["live_arena:match:report_result"] is True
    assert disabled["live_arena:match:dispute_result"] is False
    assert disabled["live_arena:match:report_scheduling_problem"] is True
    assert disabled["live_arena:availability:review_update"] is False


def test_disputed_result_disables_both_result_actions(monkeypatch):
    starter = SimpleNamespace(edit=AsyncMock())
    thread = SimpleNamespace(id=777, get_partial_message=Mock(return_value=starter))

    class FakeCompetitionResolutionService:
        def __init__(self, _sheet_id):
            pass

        async def initialize(self):
            return None

        async def match_for_thread(self, _thread_id):
            return {"status": "disputed"}

    monkeypatch.setattr(
        refresh,
        "CompetitionResolutionService",
        FakeCompetitionResolutionService,
    )

    run(refresh._refresh_channel_controls(thread, "sheet-live"))

    view = starter.edit.await_args.kwargs["view"]
    disabled = _disabled_by_custom_id(view)
    assert disabled["live_arena:match:report_result"] is True
    assert disabled["live_arena:match:dispute_result"] is True
    assert disabled["live_arena:match:report_scheduling_problem"] is True


def test_targeted_thread_refresh_runs_before_broad_reconciliation(monkeypatch):
    events = []
    thread = SimpleNamespace(id=1538130704028672043)

    async def fake_refresh(channel, sheet_id):
        assert channel is thread
        assert sheet_id == "sheet-live"
        events.append("targeted")

    async def fake_broad(sheet_id):
        assert sheet_id == "sheet-live"
        events.append("broad")

    monkeypatch.setattr(refresh, "_refresh_channel_controls", fake_refresh)
    monkeypatch.setattr(refresh, "_original_post_mutation_sync", fake_broad)

    async def scenario():
        refresh._mutation_channel.set(thread)
        await refresh._sync_with_targeted_control_refresh("sheet-live")
        assert refresh._mutation_channel.get() is None

    run(scenario())

    assert events == ["targeted", "broad"]


def test_thread_notice_captures_exact_mutated_thread_for_next_sync():
    thread = SimpleNamespace(id=1538130704028672043)
    notice = AsyncMock()
    wrapped = refresh._wrap_notice(notice)

    async def scenario():
        await wrapped(thread, "Result reported", title="Result reported by organizer")
        assert refresh._mutation_channel.get() is thread

    run(scenario())

    notice.assert_awaited_once_with(
        thread,
        "Result reported",
        title="Result reported by organizer",
    )


def test_open_match_keeps_report_enabled_and_dispute_disabled(monkeypatch):
    starter = SimpleNamespace(edit=AsyncMock())
    thread = SimpleNamespace(id=888, get_partial_message=Mock(return_value=starter))

    class FakeCompetitionResolutionService:
        def __init__(self, _sheet_id):
            pass

        async def initialize(self):
            return None

        async def match_for_thread(self, _thread_id):
            return {"status": "published"}

    monkeypatch.setattr(
        refresh,
        "CompetitionResolutionService",
        FakeCompetitionResolutionService,
    )

    run(refresh._refresh_channel_controls(thread, "sheet-live"))

    view = starter.edit.await_args.kwargs["view"]
    disabled = _disabled_by_custom_id(view)
    assert disabled["live_arena:match:report_result"] is False
    assert disabled["live_arena:match:dispute_result"] is True
    assert disabled["live_arena:match:report_scheduling_problem"] is False
