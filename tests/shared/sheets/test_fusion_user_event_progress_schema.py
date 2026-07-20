import asyncio
import datetime as dt
from unittest.mock import AsyncMock

import pytest

import shared.config as config_module
from shared.sheets import fusion as fusion_sheets


class _Worksheet:
    def __init__(self) -> None:
        self.updated: list[tuple[str, list[list[str]]]] = []
        self.appended: list[list[str]] = []

    def update(
        self, cell: str, values: list[list[str]], value_input_option: str = "RAW"
    ) -> None:
        self.updated.append((cell, values))

    def append_row(self, values: list[str], value_input_option: str = "RAW") -> None:
        self.appended.append(values)


def _install_config(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> None:
    for key, value in mapping.items():
        monkeypatch.setitem(config_module._CONFIG, key, value)
    monkeypatch.setattr(
        fusion_sheets,
        "_resolve_tab_name",
        AsyncMock(side_effect=lambda key: mapping[key]),
    )


def _install_progress_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_config(
        monkeypatch,
        {
            "FUSION_USER_EVENT_PROGRESS_TAB": "Progress Ledger",
        },
    )


def test_get_user_event_progress_uses_configured_tab_and_columns(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_progress_config(monkeypatch)
    monkeypatch.setattr(fusion_sheets, "_sheet_id", lambda: "sheet-1")

    async def _afetch_values(sheet_id: str, tab_name: str):
        assert sheet_id == "sheet-1"
        assert tab_name == "Progress Ledger"
        return [
            [
                "FusionKey",
                "UserKey",
                "EventKey",
                "Milestone Key",
                "Status",
                "partial_amount",
                "UpdatedAt",
            ],
            ["f-1", "42", "e-1", "", "done", "", "2026-01-01T00:00:00+00:00"],
            ["f-1", "42", "e-2", "", "done_bonus", "", "2026-01-01T00:00:00+00:00"],
            ["f-1", "42", "e-3", "", "unknown", "", "2026-01-01T00:00:00+00:00"],
            ["f-2", "42", "e-9", "", "done", "", "2026-01-01T00:00:00+00:00"],
        ]

    monkeypatch.setattr(fusion_sheets, "afetch_values", _afetch_values)

    progress = asyncio.run(fusion_sheets.get_user_event_progress("f-1", "42"))

    assert progress == {
        "progress": {"e-1": "done", "e-2": "done_bonus", "e-3": "not_started"},
        "partials": {"e-1": 0.0, "e-2": 0.0, "e-3": 0.0},
    }


def test_upsert_user_event_progress_updates_existing_row(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_progress_config(monkeypatch)
    worksheet = _Worksheet()
    fetch_count = 0
    monkeypatch.setattr(fusion_sheets, "_sheet_id", lambda: "sheet-1")

    async def _afetch_values(sheet_id: str, tab_name: str):
        nonlocal fetch_count
        fetch_count += 1
        assert sheet_id == "sheet-1"
        assert tab_name == "Progress Ledger"
        return [
            [
                "FusionKey",
                "UserKey",
                "EventKey",
                "Milestone Key",
                "Status",
                "partial_amount",
                "UpdatedAt",
            ],
            [
                "f-1",
                "42",
                "e-1",
                "",
                "done" if fetch_count > 1 else "not_started",
                "",
                "2026-01-01T12:00:00+00:00" if fetch_count > 1 else "",
            ],
        ]

    async def _aget_worksheet(_sheet_id: str, _tab_name: str):
        return worksheet

    async def _acall_with_backoff(fn, *args, **kwargs):
        fn(*args, **kwargs)

    monkeypatch.setattr(fusion_sheets, "afetch_values", _afetch_values)
    monkeypatch.setattr(fusion_sheets, "aget_worksheet", _aget_worksheet)
    monkeypatch.setattr(fusion_sheets, "acall_with_backoff", _acall_with_backoff)

    asyncio.run(
        fusion_sheets.upsert_user_event_progress(
            "f-1",
            "42",
            "e-1",
            "done",
            dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc),
        )
    )

    assert worksheet.updated
    assert not worksheet.appended
    assert [cell for cell, _values in worksheet.updated] == ["E2", "G2", "F2"]


def test_upsert_user_event_progress_appends_when_missing(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_progress_config(monkeypatch)
    worksheet = _Worksheet()
    fetch_count = 0
    monkeypatch.setattr(fusion_sheets, "_sheet_id", lambda: "sheet-1")

    async def _afetch_values(sheet_id: str, tab_name: str):
        nonlocal fetch_count
        fetch_count += 1
        assert sheet_id == "sheet-1"
        assert tab_name == "Progress Ledger"
        rows = [
            [
                "FusionKey",
                "UserKey",
                "EventKey",
                "Milestone Key",
                "Status",
                "partial_amount",
                "UpdatedAt",
            ]
        ]
        if fetch_count > 1:
            rows.append(
                ["f-1", "42", "e-2", "", "done_bonus", "", "2026-01-01T12:00:00+00:00"]
            )
        return rows

    async def _aget_worksheet(_sheet_id: str, _tab_name: str):
        return worksheet

    async def _acall_with_backoff(fn, *args, **kwargs):
        fn(*args, **kwargs)

    monkeypatch.setattr(fusion_sheets, "afetch_values", _afetch_values)
    monkeypatch.setattr(fusion_sheets, "aget_worksheet", _aget_worksheet)
    monkeypatch.setattr(fusion_sheets, "acall_with_backoff", _acall_with_backoff)

    asyncio.run(
        fusion_sheets.upsert_user_event_progress(
            "f-1",
            "42",
            "e-2",
            "done_bonus",
            dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc),
        )
    )

    assert not worksheet.updated
    assert worksheet.appended
    assert worksheet.appended[0][0:5] == ["f-1", "42", "e-2", "", "done_bonus"]


def test_get_user_event_progress_requires_expected_headers(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_progress_config(monkeypatch)
    monkeypatch.setattr(fusion_sheets, "_sheet_id", lambda: "sheet-1")

    async def _afetch_values(_sheet_id: str, _tab_name: str):
        return [
            [
                "BadFusion",
                "UserKey",
                "EventKey",
                "Milestone Key",
                "Status",
                "partial_amount",
                "UpdatedAt",
            ],
            ["f-1", "42", "e-1", "", "done", "", "2026-01-01T00:00:00+00:00"],
        ]

    monkeypatch.setattr(fusion_sheets, "afetch_values", _afetch_values)

    with pytest.raises(RuntimeError, match="missing required header"):
        asyncio.run(fusion_sheets.get_user_event_progress("f-1", "42"))


def test_upsert_user_event_progress_rejects_non_canonical_status(
    monkeypatch: pytest.MonkeyPatch,
):
    _install_progress_config(monkeypatch)

    with pytest.raises(ValueError, match="Invalid fusion progress status"):
        asyncio.run(
            fusion_sheets.upsert_user_event_progress(
                "f-1",
                "42",
                "e-1",
                "3",
                dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc),
            )
        )
