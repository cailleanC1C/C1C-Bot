import asyncio
import datetime as dt
import logging
from unittest.mock import AsyncMock

import pytest

from shared.sheets import fusion


def test_load_fusions_reads_fusion_prefixed_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_fetch_records(_sheet_id: str, _tab_name: str):
        return [
            {
                "fusion_id": "f-1",
                "fusion_name": "Mavara",
                "champion": "Mavara",
                "fusion_type": "traditional",
                "fusion_structure": "",
                "reward_type": "fragments",
                "fusion.needed": "400",
                "fusion.available": "450",
                "start_at_utc": "2026-04-08T00:00:00Z",
                "end_at_utc": "2026-04-22T00:00:00Z",
                "status": "draft",
            }
        ]

    monkeypatch.setattr(fusion, "afetch_records", _fake_fetch_records)
    monkeypatch.setattr(fusion, "_resolve_tab_name", lambda _key: "Fusion")
    monkeypatch.setattr(fusion, "_sheet_id", lambda: "sheet-id")

    rows = asyncio.run(fusion._load_fusions())

    assert len(rows) == 1
    assert rows[0].needed == 400
    assert rows[0].available == 450
    assert rows[0].start_at_utc == dt.datetime(2026, 4, 8, tzinfo=dt.timezone.utc)


def _event(
    *,
    event_id: str,
    start_at_utc: dt.datetime | object,
    end_at_utc: dt.datetime | object | None,
    sort_order: int = 0,
) -> fusion.FusionEventRow:
    return fusion.FusionEventRow(
        fusion_id="f-1",
        event_id=event_id,
        event_name=f"Event {event_id}",
        event_type="tournament",
        category="",
        start_at_utc=start_at_utc,  # type: ignore[arg-type]
        end_at_utc=end_at_utc,  # type: ignore[arg-type]
        reward_amount=100.0,
        bonus=None,
        reward_type="fragments",
        points_needed=None,
        is_estimated=False,
        sort_order=sort_order,
    )


def test_load_fusion_events_accepts_legacy_start_end_column_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_fetch_records(_sheet_id: str, _tab_name: str):
        return [
            {
                "fusion_id": "f-1",
                "event_id": "e-1",
                "event_name": "Legacy Column Event",
                "event_type": "tournament",
                "category": "arena",
                "start_time_utc": "2026-04-09T00:00:00Z",
                "end_time_utc": "2026-04-10T00:00:00Z",
                "reward_amount": "150",
                "sort_order": "2",
            }
        ]

    monkeypatch.setattr(fusion, "afetch_records", _fake_fetch_records)
    monkeypatch.setattr(fusion, "_resolve_tab_name", lambda _key: "Fusion Events")
    monkeypatch.setattr(fusion, "_sheet_id", lambda: "sheet-id")

    rows = asyncio.run(fusion._load_fusion_events())

    assert len(rows) == 1
    assert rows[0].start_at_utc == dt.datetime(2026, 4, 9, tzinfo=dt.timezone.utc)
    assert rows[0].end_at_utc == dt.datetime(2026, 4, 10, tzinfo=dt.timezone.utc)


def test_get_upcoming_events_returns_only_future_events_sorted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    events = [
        _event(
            event_id="future-later",
            start_at_utc=now + dt.timedelta(days=2),
            end_at_utc=now + dt.timedelta(days=3),
        ),
        _event(
            event_id="past",
            start_at_utc=now - dt.timedelta(days=2),
            end_at_utc=now - dt.timedelta(days=1),
        ),
        _event(
            event_id="future-soon",
            start_at_utc=now + dt.timedelta(hours=1),
            end_at_utc=now + dt.timedelta(days=1),
        ),
    ]
    monkeypatch.setattr(fusion, "get_fusion_events", AsyncMock(return_value=events))

    result = asyncio.run(fusion.get_upcoming_events("f-1", now=now))

    assert [row.event_id for row in result] == ["future-soon", "future-later"]


def test_get_active_events_returns_only_currently_active_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    events = [
        _event(
            event_id="active-window",
            start_at_utc=now - dt.timedelta(hours=2),
            end_at_utc=now + dt.timedelta(hours=2),
            sort_order=2,
        ),
        _event(
            event_id="active-open-ended",
            start_at_utc=now - dt.timedelta(hours=1),
            end_at_utc=None,
            sort_order=1,
        ),
        _event(
            event_id="future",
            start_at_utc=now + dt.timedelta(minutes=1),
            end_at_utc=now + dt.timedelta(hours=4),
        ),
    ]
    monkeypatch.setattr(fusion, "get_fusion_events", AsyncMock(return_value=events))

    result = asyncio.run(fusion.get_active_events("f-1", now=now))

    assert [row.event_id for row in result] == ["active-window", "active-open-ended"]


def test_get_active_events_excludes_ended_events(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    events = [
        _event(
            event_id="ended",
            start_at_utc=now - dt.timedelta(days=2),
            end_at_utc=now - dt.timedelta(seconds=1),
        ),
        _event(
            event_id="active",
            start_at_utc=now - dt.timedelta(days=1),
            end_at_utc=now + dt.timedelta(days=1),
        ),
    ]
    monkeypatch.setattr(fusion, "get_fusion_events", AsyncMock(return_value=events))

    result = asyncio.run(fusion.get_active_events("f-1", now=now))

    assert [row.event_id for row in result] == ["active"]


def test_event_helpers_skip_invalid_timestamps_without_failing(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    events = [
        _event(
            event_id="invalid-start",
            start_at_utc="not-a-datetime",
            end_at_utc=now + dt.timedelta(hours=1),
        ),
        _event(
            event_id="invalid-end",
            start_at_utc=now - dt.timedelta(hours=1),
            end_at_utc="not-a-datetime",
        ),
        _event(
            event_id="valid-future",
            start_at_utc=now + dt.timedelta(hours=1),
            end_at_utc=now + dt.timedelta(hours=2),
        ),
        _event(
            event_id="valid-active",
            start_at_utc=now - dt.timedelta(hours=1),
            end_at_utc=now + dt.timedelta(hours=2),
        ),
    ]
    monkeypatch.setattr(fusion, "get_fusion_events", AsyncMock(return_value=events))

    with caplog.at_level(logging.WARNING, logger="c1c.sheets.fusion"):
        upcoming = asyncio.run(fusion.get_upcoming_events("f-1", now=now))
        active = asyncio.run(fusion.get_active_events("f-1", now=now))

    assert [row.event_id for row in upcoming] == ["valid-future"]
    assert [row.event_id for row in active] == ["valid-active"]
    assert "invalid start_at_utc" in caplog.text
    assert "invalid end_at_utc" in caplog.text
