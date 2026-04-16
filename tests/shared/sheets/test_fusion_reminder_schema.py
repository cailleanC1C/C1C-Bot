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


def test_get_sent_reminder_keys_uses_configured_tab_and_columns(monkeypatch: pytest.MonkeyPatch):
    _install_config(
        monkeypatch,
        {
            "FUSION_REMINDER_TAB": "Reminder Ledger",
        },
    )
    monkeypatch.setattr(fusion_sheets, "_sheet_id", lambda: "sheet-1")
    async def _afetch_values(sheet_id: str, tab_name: str):
        assert sheet_id == "sheet-1"
        assert tab_name == "Reminder Ledger"
        return [
            ["fusion_id", "event_id", "reminder_type", "sent_at_utc"],
            ["f-1", "e-1", "start", "2026-01-01T00:00:00+00:00"],
            ["f-2", "e-2", "start", "2026-01-01T00:00:00+00:00"],
        ]

    monkeypatch.setattr(fusion_sheets, "afetch_values", _afetch_values)

    sent = asyncio.run(fusion_sheets.get_sent_reminder_keys("f-1"))

    assert sent == {("e-1", "start")}


def test_mark_reminder_sent_updates_existing_row_with_configured_columns(monkeypatch: pytest.MonkeyPatch):
    _install_config(
        monkeypatch,
        {
            "FUSION_REMINDER_TAB": "Reminder Ledger",
        },
    )
    worksheet = _Worksheet()
    monkeypatch.setattr(fusion_sheets, "_sheet_id", lambda: "sheet-1")
    async def _afetch_values(sheet_id: str, tab_name: str):
        assert sheet_id == "sheet-1"
        assert tab_name == "Reminder Ledger"
        return [
            ["fusion_id", "event_id", "reminder_type", "sent_at_utc"],
            ["f-1", "e-1", "start", ""],
        ]

    async def _aget_worksheet(_sheet_id: str, _tab_name: str):
        return worksheet

    async def _acall_with_backoff(fn, *args, **kwargs):
        fn(*args, **kwargs)

    monkeypatch.setattr(fusion_sheets, "afetch_values", _afetch_values)
    monkeypatch.setattr(fusion_sheets, "aget_worksheet", _aget_worksheet)
    monkeypatch.setattr(fusion_sheets, "acall_with_backoff", _acall_with_backoff)

    asyncio.run(
        fusion_sheets.mark_reminder_sent(
            "f-1",
            event_id="e-1",
            reminder_type="start",
            sent_at=dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc),
        )
    )

    assert worksheet.updated
    assert not worksheet.appended
    assert worksheet.updated[0][0] == "D2"


def test_get_sent_reminder_keys_requires_required_headers(monkeypatch: pytest.MonkeyPatch):
    _install_config(
        monkeypatch,
        {
            "FUSION_REMINDER_TAB": "Reminder Ledger",
        },
    )
    monkeypatch.setattr(fusion_sheets, "_sheet_id", lambda: "sheet-1")

    async def _afetch_values(sheet_id: str, tab_name: str):
        assert sheet_id == "sheet-1"
        assert tab_name == "Reminder Ledger"
        return [["event_id", "reminder_type", "sent_at_utc"], ["e-1", "start", ""]]

    monkeypatch.setattr(fusion_sheets, "afetch_values", _afetch_values)

    with pytest.raises(RuntimeError, match="missing required header"):
        asyncio.run(fusion_sheets.get_sent_reminder_keys("f-1"))


def test_get_sent_reminder_keys_does_not_require_sent_at_header(monkeypatch: pytest.MonkeyPatch):
    _install_config(
        monkeypatch,
        {
            "FUSION_REMINDER_TAB": "Reminder Ledger",
        },
    )
    monkeypatch.setattr(fusion_sheets, "_sheet_id", lambda: "sheet-1")

    async def _afetch_values(sheet_id: str, tab_name: str):
        assert sheet_id == "sheet-1"
        assert tab_name == "Reminder Ledger"
        return [
            ["fusion_id", "event_id", "reminder_type"],
            ["f-1", "e-1", "start"],
        ]

    monkeypatch.setattr(fusion_sheets, "afetch_values", _afetch_values)

    sent = asyncio.run(fusion_sheets.get_sent_reminder_keys("f-1"))

    assert sent == {("e-1", "start")}
