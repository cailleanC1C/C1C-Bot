from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord
import pytest

from modules.community.live_arena import result_control_refresh as refresh


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture(autouse=True)
def reset_refresh_state():
    refresh._mutation_channel.set(None)
    refresh._mutation_match.set(None)
    refresh._broad_sync_tasks.clear()
    refresh._broad_sync_dirty.clear()
    yield
    refresh._mutation_channel.set(None)
    refresh._mutation_match.set(None)
    refresh._broad_sync_tasks.clear()
    refresh._broad_sync_dirty.clear()


def _disabled_by_custom_id(view):
    return {
        getattr(item, "custom_id", ""): bool(getattr(item, "disabled", False))
        for item in view.children
        if getattr(item, "custom_id", "")
    }


def _pending_match():
    return {
        "match_id": "LA-2026-TRIAL-01-Q2-M03",
        "status": "pending_confirmation",
        "reported_by_discord_user_id": "1050998068943781928",
        "player_a_discord_user_id": "1050998068943781928",
        "player_b_discord_user_id": "728674284603572327",
    }


def test_pending_confirmation_uses_returned_match_without_another_sheet_read(monkeypatch):
    starter = SimpleNamespace(edit=AsyncMock())
    thread = SimpleNamespace(
        id=1538130704028672043,
        get_partial_message=Mock(return_value=starter),
    )

    class MustNotReadService:
        def __init__(self, _sheet_id):
            raise AssertionError("immediate control refresh must not read Sheets")

    monkeypatch.setattr(refresh, "CompetitionResolutionService", MustNotReadService)

    run(
        refresh._refresh_channel_controls(
            thread,
            "sheet-live",
            _pending_match(),
        )
    )

    starter.edit.assert_awaited_once()
    view = starter.edit.await_args.kwargs["view"]
    disabled = _disabled_by_custom_id(view)
    assert disabled["live_arena:match:report_result"] is True
    assert disabled["live_arena:match:dispute_result"] is False
    assert disabled["live_arena:match:report_scheduling_problem"] is True
    assert disabled["live_arena:availability:review_update"] is False


def test_disputed_result_disables_both_result_actions_without_reading(monkeypatch):
    starter = SimpleNamespace(edit=AsyncMock())
    thread = SimpleNamespace(id=777, get_partial_message=Mock(return_value=starter))

    class MustNotReadService:
        def __init__(self, _sheet_id):
            raise AssertionError("immediate control refresh must not read Sheets")

    monkeypatch.setattr(refresh, "CompetitionResolutionService", MustNotReadService)

    run(
        refresh._refresh_channel_controls(
            thread,
            "sheet-live",
            {"status": "disputed"},
        )
    )

    view = starter.edit.await_args.kwargs["view"]
    disabled = _disabled_by_custom_id(view)
    assert disabled["live_arena:match:report_result"] is True
    assert disabled["live_arena:match:dispute_result"] is True
    assert disabled["live_arena:match:report_scheduling_problem"] is True


def test_open_match_keeps_report_enabled_and_dispute_disabled_without_reading(monkeypatch):
    starter = SimpleNamespace(edit=AsyncMock())
    thread = SimpleNamespace(id=888, get_partial_message=Mock(return_value=starter))

    class MustNotReadService:
        def __init__(self, _sheet_id):
            raise AssertionError("immediate control refresh must not read Sheets")

    monkeypatch.setattr(refresh, "CompetitionResolutionService", MustNotReadService)

    run(
        refresh._refresh_channel_controls(
            thread,
            "sheet-live",
            {"status": "published"},
        )
    )

    view = starter.edit.await_args.kwargs["view"]
    disabled = _disabled_by_custom_id(view)
    assert disabled["live_arena:match:report_result"] is False
    assert disabled["live_arena:match:dispute_result"] is True
    assert disabled["live_arena:match:report_scheduling_problem"] is False


def test_service_mutation_wrapper_captures_authoritative_updated_row():
    async def original(_self, *_args, **_kwargs):
        return _pending_match()

    wrapped = refresh._wrap_service_mutation(original)

    async def scenario():
        refresh._mutation_match.set(None)
        result = await wrapped(object(), "x")
        assert result == _pending_match()
        assert refresh._mutation_match.get() == _pending_match()

    run(scenario())


def test_targeted_refresh_finishes_before_debounced_broad_sync(monkeypatch):
    events = []
    thread = SimpleNamespace(id=1538130704028672043)
    updated = _pending_match()

    async def fake_refresh(channel, sheet_id, match=None):
        assert channel is thread
        assert sheet_id == "sheet-live"
        assert match == updated
        events.append("targeted")

    async def fake_broad(sheet_id):
        assert sheet_id == "sheet-live"
        events.append("broad")

    monkeypatch.setattr(refresh, "_refresh_channel_controls", fake_refresh)
    monkeypatch.setattr(refresh, "_original_post_mutation_sync", fake_broad)
    monkeypatch.setattr(refresh, "_BROAD_SYNC_DEBOUNCE_SECONDS", 0.01)

    async def scenario():
        refresh._mutation_channel.set(thread)
        refresh._mutation_match.set(updated)
        await refresh._sync_with_targeted_control_refresh("sheet-live")
        assert events == ["targeted"]
        assert refresh._mutation_channel.get() is None
        assert refresh._mutation_match.get() is None
        await asyncio.sleep(0.05)

    run(scenario())

    assert events == ["targeted", "broad"]


def test_rapid_mutations_coalesce_to_one_broad_reconciliation(monkeypatch):
    broad_calls = 0

    async def fake_broad(sheet_id):
        nonlocal broad_calls
        assert sheet_id == "sheet-live"
        broad_calls += 1

    monkeypatch.setattr(refresh, "_original_post_mutation_sync", fake_broad)
    monkeypatch.setattr(refresh, "_BROAD_SYNC_DEBOUNCE_SECONDS", 0.01)

    async def scenario():
        refresh._schedule_broad_sync("sheet-live")
        refresh._schedule_broad_sync("sheet-live")
        refresh._schedule_broad_sync("sheet-live")
        await asyncio.sleep(0.05)

    run(scenario())

    assert broad_calls == 1
    assert "sheet-live" not in refresh._broad_sync_tasks


def test_mutation_during_broad_sync_gets_one_trailing_pass(monkeypatch):
    calls = 0
    first_started = asyncio.Event()
    release_first = asyncio.Event()

    async def fake_broad(_sheet_id):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await release_first.wait()

    monkeypatch.setattr(refresh, "_original_post_mutation_sync", fake_broad)
    monkeypatch.setattr(refresh, "_BROAD_SYNC_DEBOUNCE_SECONDS", 0.01)

    async def scenario():
        refresh._schedule_broad_sync("sheet-live")
        await first_started.wait()
        refresh._schedule_broad_sync("sheet-live")
        release_first.set()
        await asyncio.sleep(0.06)

    run(scenario())

    assert calls == 2


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


def test_quota_error_embed_uses_preloaded_sheet_copy_and_hides_provider_payload(monkeypatch):
    raw = RuntimeError(
        "{'code': 429, 'message': 'Quota exceeded', 'status': 'RESOURCE_EXHAUSTED'}"
    )
    monkeypatch.setattr(
        refresh,
        "_base_error_embed",
        lambda message: discord.Embed(title="Base", description=str(message)),
    )
    refresh._quota_copy["sheet-live"] = refresh._QuotaCopy(
        "Tournament data is temporarily busy",
        "Woadkeeper couldn't read the tournament data just now. Please try again in a moment.",
        0x1A73E8,
    )
    monkeypatch.setattr(refresh, "_active_sheet_id", "sheet-live")

    embed = refresh._safe_error_embed(raw)

    assert embed.title == "Tournament data is temporarily busy"
    assert "try again in a moment" in embed.description
    assert "429" not in embed.description
    assert "RESOURCE_EXHAUSTED" not in embed.description


def test_quota_error_fallback_never_exposes_raw_payload_without_cache(monkeypatch):
    raw = RuntimeError("429 RESOURCE_EXHAUSTED ReadRequestsPerMinutePerUser")
    monkeypatch.setattr(
        refresh,
        "_base_error_embed",
        lambda message: discord.Embed(title="Base", description=str(message)),
    )
    refresh._quota_copy.clear()
    monkeypatch.setattr(refresh, "_active_sheet_id", "missing")

    embed = refresh._safe_error_embed(raw)

    assert embed.description == "Something went wrong. Please try again later."
    assert "429" not in embed.description


def test_quota_copy_loads_from_messages_without_placeholders(monkeypatch):
    async def fake_config(_sheet_id):
        return {"MESSAGES_TAB": "MESSAGES"}, []

    async def fake_values(_sheet_id, _tab):
        return [
            ["message_key", "title", "description", "color_hex", "active", "notes"],
            [
                "sheets_quota_retry",
                "Tournament data is temporarily busy",
                "Please try again in a moment.",
                "#1A73E8",
                "TRUE",
                "",
            ],
        ]

    monkeypatch.setattr(refresh, "load_pr5_config", fake_config)
    monkeypatch.setattr(refresh, "afetch_values", fake_values)
    refresh._quota_copy.clear()

    run(refresh._load_quota_copy("sheet-live"))

    assert refresh._quota_copy["sheet-live"].title == (
        "Tournament data is temporarily busy"
    )
    assert refresh._quota_copy["sheet-live"].description == (
        "Please try again in a moment."
    )
