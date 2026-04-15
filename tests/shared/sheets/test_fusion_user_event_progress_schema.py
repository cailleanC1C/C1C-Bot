import asyncio
import datetime as dt

import pytest

import shared.config as config_module
from shared.sheets import fusion as fusion_sheets


class _Worksheet:
    def __init__(self) -> None:
        self.updated: list[tuple[str, list[list[str]]]] = []
        self.appended: list[list[str]] = []

    def update(self, cell: str, values: list[list[str]], value_input_option: str = "RAW") -> None:
        self.updated.append((cell, values))

    def append_row(self, values: list[str], value_input_option: str = "RAW") -> None:
        self.appended.append(values)


def _install_config(monkeypatch: pytest.MonkeyPatch, mapping: dict[str, str]) -> None:
    for key, value in mapping.items():
        monkeypatch.setitem(config_module._CONFIG, key, value)


def _install_progress_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_config(
        monkeypatch,
        {
            "FUSION_USER_EVENT_PROGRESS_TAB": "Progress Ledger",
            "FUSION_USER_EVENT_PROGRESS_COL_FUSION_ID": "FusionKey",
            "FUSION_USER_EVENT_PROGRESS_COL_USER_ID": "UserKey",
            "FUSION_USER_EVENT_PROGRESS_COL_EVENT_ID": "EventKey",
            "FUSION_USER_EVENT_PROGRESS_COL_STATUS": "Status",
            "FUSION_USER_EVENT_PROGRESS_COL_UPDATED_AT_UTC": "UpdatedAt",
        },
    )


def test_get_user_event_progress_uses_configured_tab_and_columns(monkeypatch: pytest.MonkeyPatch):
    _install_progress_config(monkeypatch)
    monkeypatch.setattr(fusion_sheets, "_sheet_id", lambda: "sheet-1")

    async def _afetch_values(sheet_id: str, tab_name: str):
        assert sheet_id == "sheet-1"
        assert tab_name == "Progress Ledger"
        return [
            ["FusionKey", "UserKey", "EventKey", "Status", "UpdatedAt"],
            ["f-1", "42", "e-1", "done", "2026-01-01T00:00:00+00:00"],
            ["f-1", "42", "e-2", "unknown", "2026-01-01T00:00:00+00:00"],
            ["f-2", "42", "e-9", "done", "2026-01-01T00:00:00+00:00"],
        ]

    monkeypatch.setattr(fusion_sheets, "afetch_values", _afetch_values)

    progress = asyncio.run(fusion_sheets.get_user_event_progress("f-1", "42"))

    assert progress == {"e-1": "done", "e-2": "not_started"}


def test_upsert_user_event_progress_updates_existing_row(monkeypatch: pytest.MonkeyPatch):
    _install_progress_config(monkeypatch)
    worksheet = _Worksheet()
    monkeypatch.setattr(fusion_sheets, "_sheet_id", lambda: "sheet-1")

    async def _afetch_values(sheet_id: str, tab_name: str):
        assert sheet_id == "sheet-1"
        assert tab_name == "Progress Ledger"
        return [
            ["FusionKey", "UserKey", "EventKey", "Status", "UpdatedAt"],
            ["f-1", "42", "e-1", "not_started", ""],
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
    assert [cell for cell, _values in worksheet.updated] == ["D2", "E2"]


def test_upsert_user_event_progress_appends_when_missing(monkeypatch: pytest.MonkeyPatch):
    _install_progress_config(monkeypatch)
    worksheet = _Worksheet()
    monkeypatch.setattr(fusion_sheets, "_sheet_id", lambda: "sheet-1")

    async def _afetch_values(sheet_id: str, tab_name: str):
        assert sheet_id == "sheet-1"
        assert tab_name == "Progress Ledger"
        return [["FusionKey", "UserKey", "EventKey", "Status", "UpdatedAt"]]

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
            "in_progress",
            dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc),
        )
    )

    assert not worksheet.updated
    assert worksheet.appended
    assert worksheet.appended[0][0:4] == ["f-1", "42", "e-2", "in_progress"]


def test_get_user_event_progress_requires_configured_column_keys(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delitem(
        config_module._CONFIG,
        "FUSION_USER_EVENT_PROGRESS_COL_FUSION_ID",
        raising=False,
    )
    _install_config(
        monkeypatch,
        {
            "FUSION_USER_EVENT_PROGRESS_TAB": "Progress Ledger",
            "FUSION_USER_EVENT_PROGRESS_COL_USER_ID": "UserKey",
            "FUSION_USER_EVENT_PROGRESS_COL_EVENT_ID": "EventKey",
            "FUSION_USER_EVENT_PROGRESS_COL_STATUS": "Status",
            "FUSION_USER_EVENT_PROGRESS_COL_UPDATED_AT_UTC": "UpdatedAt",
        },
    )

    with pytest.raises(RuntimeError, match="FUSION_USER_EVENT_PROGRESS_COL_FUSION_ID"):
        asyncio.run(fusion_sheets.get_user_event_progress("f-1", "42"))
