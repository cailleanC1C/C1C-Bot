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
                "champion_image_url": "https://cdn.discordapp.com/champion.png",
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
    assert rows[0].champion_image_url == "https://cdn.discordapp.com/champion.png"
    assert rows[0].start_at_utc == dt.datetime(2026, 4, 8, tzinfo=dt.timezone.utc)




def test_load_fusions_reads_needed_total_alias_for_fragment(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_fetch_records(_sheet_id: str, _tab_name: str):
        return [
            {
                "fusion_id": "f-frag",
                "fusion_name": "Mashiro",
                "champion": "Mashiro",
                "champion_image_url": "https://cdn.discordapp.com/mashiro.png",
                "fusion_type": "fragment",
                "fusion_structure": "",
                "reward_type": "fragment",
                "needed_total": "100",
                "available": "135",
                "start_at_utc": "2026-04-08T00:00:00Z",
                "end_at_utc": "2026-04-22T00:00:00Z",
                "status": "active",
            }
        ]

    monkeypatch.setattr(fusion, "afetch_records", _fake_fetch_records)
    monkeypatch.setattr(fusion, "_resolve_tab_name", lambda _key: "Fusion")
    monkeypatch.setattr(fusion, "_sheet_id", lambda: "sheet-id")

    rows = asyncio.run(fusion._load_fusions())

    assert len(rows) == 1
    assert rows[0].fusion_type == "fragment"
    assert rows[0].needed == 100
    assert rows[0].available == 135

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


def test_get_fusion_events_matches_fusion_id_casefold_and_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = (
        fusion.FusionEventRow(
            fusion_id=" Anomalus_Titan_Event_2026_04 ",
            event_id="PH_1",
            event_name="Path Event",
            event_type="tournament",
            category="",
            start_at_utc=dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc),
            end_at_utc=dt.datetime(2026, 4, 2, tzinfo=dt.timezone.utc),
            reward_amount=100.0,
            bonus=None,
            reward_type="Titan Event Points",
            points_needed=1000,
            is_estimated=True,
            sort_order=1,
        ),
    )
    monkeypatch.setattr(fusion, "register_cache_buckets", lambda: ("fusion", "fusion_events"))
    monkeypatch.setattr(fusion, "_cached_rows", AsyncMock(return_value=rows))

    result = asyncio.run(fusion.get_fusion_events("anomalus_titan_event_2026_04"))

    assert [row.event_id for row in result] == ["PH_1"]


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


def test_get_upcoming_events_uses_validated_timestamps_for_sorting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    events = [
        _event(
            event_id="aware",
            start_at_utc=dt.datetime(2026, 4, 10, 13, 0, tzinfo=dt.timezone.utc),
            end_at_utc=dt.datetime(2026, 4, 10, 15, 0, tzinfo=dt.timezone.utc),
        ),
        _event(
            event_id="naive-earlier",
            start_at_utc=dt.datetime(2026, 4, 10, 12, 30),
            end_at_utc=dt.datetime(2026, 4, 10, 14, 0),
        ),
    ]
    monkeypatch.setattr(fusion, "get_fusion_events", AsyncMock(return_value=events))

    result = asyncio.run(fusion.get_upcoming_events("f-1", now=now))

    assert [row.event_id for row in result] == ["naive-earlier", "aware"]


def test_transition_fusion_to_ended_updates_status_once(monkeypatch: pytest.MonkeyPatch) -> None:
    worksheet = AsyncMock()

    async def _afetch_values(_sheet_id: str, _tab_name: str):
        return [
            ["fusion_id", "status"],
            ["f-1", "published"],
        ]

    async def _acall_with_backoff(fn, *args, **kwargs):
        return await fn(*args, **kwargs)

    monkeypatch.setattr(fusion, "_resolve_tab_name", lambda _key: "Fusion")
    monkeypatch.setattr(fusion, "_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(fusion, "afetch_values", _afetch_values)
    monkeypatch.setattr(fusion, "aget_worksheet", AsyncMock(return_value=worksheet))
    monkeypatch.setattr(fusion, "acall_with_backoff", _acall_with_backoff)
    monkeypatch.setattr(fusion, "register_cache_buckets", lambda: ("fusion", "fusion_events"))
    monkeypatch.setattr(fusion.cache, "refresh_now", AsyncMock())

    changed = asyncio.run(fusion.transition_fusion_to_ended("f-1"))

    assert changed is True
    worksheet.update.assert_awaited_once_with("B2", [["ended"]], value_input_option="RAW")
    fusion.cache.refresh_now.assert_awaited_once_with("fusion", actor="fusion_status_ended")


def test_transition_fusion_to_ended_is_noop_when_already_ended(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    worksheet = AsyncMock()

    async def _afetch_values(_sheet_id: str, _tab_name: str):
        return [
            ["fusion_id", "status"],
            ["f-1", "ended"],
        ]

    monkeypatch.setattr(fusion, "_resolve_tab_name", lambda _key: "Fusion")
    monkeypatch.setattr(fusion, "_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(fusion, "afetch_values", _afetch_values)
    monkeypatch.setattr(fusion, "aget_worksheet", AsyncMock(return_value=worksheet))
    monkeypatch.setattr(fusion, "register_cache_buckets", lambda: ("fusion", "fusion_events"))
    monkeypatch.setattr(fusion.cache, "refresh_now", AsyncMock())

    changed = asyncio.run(fusion.transition_fusion_to_ended("f-1"))

    assert changed is False
    worksheet.update.assert_not_awaited()
    fusion.cache.refresh_now.assert_not_awaited()


def _fusion_row(*, fusion_id: str, fusion_type: str, status: str) -> fusion.FusionRow:
    return fusion.FusionRow(
        fusion_id=fusion_id,
        fusion_name=fusion_id,
        champion="Champ",
        champion_image_url="",
        fusion_type=fusion_type,
        fusion_structure="",
        reward_type="fragments",
        needed=100,
        available=0,
        start_at_utc=dt.datetime(2026, 5, 1, tzinfo=dt.timezone.utc),
        end_at_utc=dt.datetime(2026, 5, 20, tzinfo=dt.timezone.utc),
        announcement_channel_id=None,
        opt_in_role_id=None,
        announcement_message_id=None,
        published_at=None,
        last_announcement_refresh_at=None,
        last_announcement_status_hash="",
        status=status,
    )


def test_get_publishable_fusion_includes_fragment_draft(monkeypatch: pytest.MonkeyPatch) -> None:
    rows = (
        _fusion_row(fusion_id="hybrid-active", fusion_type="hybrid", status="active"),
        _fusion_row(fusion_id="fragment-draft", fusion_type="fragment", status="draft"),
    )
    monkeypatch.setattr(fusion, "register_cache_buckets", lambda: ("fusion", "fusion_events"))
    monkeypatch.setattr(fusion, "_cached_rows", AsyncMock(return_value=rows))

    result = asyncio.run(
        fusion.get_publishable_fusion(include_draft=True, tracker_kind="fusion", prefer_draft=True)
    )

    assert result is not None
    assert result.fusion_id == "fragment-draft"


@pytest.mark.parametrize(
    ("fusion_type", "expected"),
    [
        ("hybrid", "fusion"),
        ("fragment", "fusion"),
        ("fusion", "fusion"),
        ("traditional", "fusion"),
        ("titan", "titan"),
        ("titan_event", "titan"),
        ("titan event", "titan"),
    ],
)
def test_tracker_kind_maps_supported_fusion_types(fusion_type: str, expected: str) -> None:
    row = _fusion_row(fusion_id="f-1", fusion_type=fusion_type, status="draft")
    assert fusion._tracker_kind(row) == expected


def test_load_fusions_normalizes_fragment_fusion_type_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_fetch_records(_sheet_id: str, _tab_name: str):
        return [
            {
                "fusion_id": "fragment-case",
                "fusion_name": "Mashiro",
                "champion": "Mashiro",
                "champion_image_url": "",
                "fusion_type": "FRAGMENT",
                "reward_type": "fragments",
                "needed": "100",
                "available": "0",
                "start_at_utc": "2026-05-01T00:00:00Z",
                "end_at_utc": "2026-05-20T00:00:00Z",
                "status": "draft",
            }
        ]

    monkeypatch.setattr(fusion, "afetch_records", _fake_fetch_records)
    monkeypatch.setattr(fusion, "_resolve_tab_name", lambda _key: "Fusion")
    monkeypatch.setattr(fusion, "_sheet_id", lambda: "sheet-id")

    rows = asyncio.run(fusion._load_fusions())

    assert len(rows) == 1
    assert rows[0].fusion_type == "fragment"


def test_upsert_user_event_progress_returns_insert_diagnostics(monkeypatch: pytest.MonkeyPatch) -> None:
    matrix = [["fusion_id", "user_id", "event_id", "milestone_key", "status", "updated_at_utc", "partial_amount"]]

    class _Worksheet:
        async def append_row(self, *_args, **_kwargs):
            return None

    worksheet = _Worksheet()
    appended: list[list[str]] = []

    async def _afetch_values(_sheet_id: str, tab_name: str):
        assert tab_name == "User Progress"
        return matrix

    async def _acall_with_backoff(func, *args, **kwargs):
        if getattr(func, "__name__", "") == "append_row":
            appended.append(args[0])
            matrix.append(args[0])
        return None

    monkeypatch.setattr(fusion, "_resolve_tab_name", lambda key: "User Progress")
    monkeypatch.setattr(fusion, "_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(fusion, "afetch_values", _afetch_values)
    monkeypatch.setattr(fusion, "aget_worksheet", AsyncMock(return_value=worksheet))
    monkeypatch.setattr(fusion, "acall_with_backoff", _acall_with_backoff)

    result = asyncio.run(
        fusion.upsert_user_event_progress(
            "f-1",
            "10",
            "e-1",
            "done",
            dt.datetime(2026, 4, 10, tzinfo=dt.timezone.utc),
        )
    )

    assert result.tab_name == "User Progress"
    assert result.headers == tuple(matrix[0])
    assert result.row_key == ("f-1", "10", "e-1", "")
    assert result.operation == "inserted"
    assert result.saved is True
    assert appended[0][4] == "done"


def test_get_user_traditional_progress_resolves_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _afetch_values(_sheet_id: str, tab_name: str):
        assert tab_name == "fusion_traditional_user_prog"
        return [
            ["user_id", "fusion_id", "epics_ascended", "rares_level_40", "rares_ascended", "epics_fused", "epics_level_50", "updated_at_utc"],
            ["42", "f-1", "3", "12", "8", "2", "2", "2026-07-01T00:00:00+00:00"],
        ]

    monkeypatch.setattr(fusion, "_resolve_tab_name", lambda key: "fusion_traditional_user_prog")
    monkeypatch.setattr(fusion, "_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(fusion, "afetch_values", _afetch_values)

    row = asyncio.run(fusion.get_user_traditional_progress("f-1", "42"))

    assert row.rares_level_40 == 12
    assert row.rares_ascended == 8
    assert row.epics_fused == 2
    assert row.epics_level_50 == 2
    assert row.epics_ascended == 3


def test_upsert_user_traditional_progress_updates_existing_row_by_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    worksheet = AsyncMock()

    async def _afetch_values(_sheet_id: str, _tab_name: str):
        return [
            ["user_id", "fusion_id", "epics_ascended", "rares_level_40", "rares_ascended", "epics_fused", "epics_level_50", "updated_at_utc"],
            ["42", "f-1", "1", "4", "4", "1", "1", "old"],
        ]

    async def _acall_with_backoff(fn, *args, **kwargs):
        return await fn(*args, **kwargs)

    monkeypatch.setattr(fusion, "_resolve_tab_name", lambda key: "fusion_traditional_user_prog")
    monkeypatch.setattr(fusion, "_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(fusion, "afetch_values", _afetch_values)
    monkeypatch.setattr(fusion, "aget_worksheet", AsyncMock(return_value=worksheet))
    monkeypatch.setattr(fusion, "acall_with_backoff", _acall_with_backoff)

    row = asyncio.run(fusion.upsert_user_traditional_progress(
        "f-1", "42", rares_level_40=8, rares_ascended=8, epics_fused=2,
        epics_level_50=2, epics_ascended=2, updated_at=dt.datetime(2026, 7, 1, tzinfo=dt.timezone.utc)
    ))

    assert row.epics_ascended == 2
    updated_cells = [call.args[0] for call in worksheet.update.await_args_list]
    assert "D2" in updated_cells
    assert "C2" in updated_cells
    worksheet.append_row.assert_not_awaited()
