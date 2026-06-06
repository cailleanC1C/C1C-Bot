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


def test_get_last_reminder_sent_at_reads_grouped_marker(monkeypatch: pytest.MonkeyPatch):
    _install_config(
        monkeypatch,
        {
            "FUSION_REMINDER_TAB": "FusionReminders",
        },
    )
    monkeypatch.setattr(fusion_sheets, "_sheet_id", lambda: "sheet-1")

    async def _afetch_values(sheet_id: str, tab_name: str):
        assert sheet_id == "sheet-1"
        assert tab_name == "FusionReminders"
        return [
            ["fusion_id", "event_id", "reminder_type", "sent_at_utc"],
            ["f-1", "e-old", "start", "2026-04-09T01:00:00+00:00"],
            ["f-1", "grouped_daily:2026-04-09", "grouped_daily", "2026-04-09T12:00:00+00:00"],
            ["f-1", "grouped_daily:2026-04-10", "grouped_daily", "2026-04-10T12:00:00+00:00"],
            ["f-2", "grouped_daily:2026-04-11", "grouped_daily", "2026-04-11T12:00:00+00:00"],
        ]

    monkeypatch.setattr(fusion_sheets, "afetch_values", _afetch_values)

    sent_at = asyncio.run(
        fusion_sheets.get_last_reminder_sent_at("f-1", reminder_type="grouped_daily")
    )

    assert sent_at == dt.datetime(2026, 4, 10, 12, tzinfo=dt.timezone.utc)


def test_get_fusion_reminder_settings_reads_configured_tab(monkeypatch: pytest.MonkeyPatch):
    _install_config(
        monkeypatch,
        {
            "FUSION_REMINDER_SETTINGS_TAB": "FusionReminderSettings",
        },
    )
    monkeypatch.setattr(fusion_sheets, "_sheet_id", lambda: "sheet-1")

    async def _afetch_records(sheet_id: str, tab_name: str):
        assert sheet_id == "sheet-1"
        assert tab_name == "FusionReminderSettings"
        return [
            {"setting_key": "group_events", "setting_value": "TRUE"},
            {"key": "grouped_post_time_utc", "value": "12:30"},
            {"key": "upcoming_window_days", "value": "3"},
            {"key": "include_upcoming_events", "value": "TRUE"},
            {"key": "grouped_embed_title", "value": "Title {fusion_title}"},
            {"key": "grouped_embed_description", "value": "Desc {jump_link}"},
            {"key": "grouped_live_label", "value": "Live"},
            {"key": "grouped_upcoming_label", "value": "Up"},
            {"key": "grouped_ending_label", "value": "End"},
            {"key": "grouped_empty_value", "value": "None"},
            {"key": "grouped_jump_label", "value": "Open"},
        ]

    monkeypatch.setattr(fusion_sheets, "afetch_records", _afetch_records)
    settings = asyncio.run(fusion_sheets.get_fusion_reminder_settings())
    assert settings.group_events is True
    assert settings.group_events_source.tab_name == "FusionReminderSettings"
    assert settings.group_events_source.key_header == "setting_key"
    assert settings.group_events_source.value_header == "setting_value"
    assert settings.group_events_source.raw_value == "TRUE"
    assert settings.grouped_post_time_utc == "12:30"
    assert settings.upcoming_window_days == 3
    assert settings.include_upcoming_events is True
    assert settings.grouped_embed_title == "Title {fusion_title}"
    assert settings.grouped_empty_value == "None"
    assert settings.grouped_jump_label == "Open"


def test_fusion_reminder_bool_parser_accepts_sheet_true_false_values():
    assert fusion_sheets._parse_bool(True) is True
    assert fusion_sheets._parse_bool(False) is False
    assert fusion_sheets._parse_bool("TRUE") is True
    assert fusion_sheets._parse_bool("FALSE") is False
    assert fusion_sheets._parse_bool("yes") is True
    assert fusion_sheets._parse_bool("1") is True
    assert fusion_sheets._parse_bool("0") is False
    assert fusion_sheets._parse_bool("") is False


def test_load_fusion_events_reads_optional_embed_copy_columns(monkeypatch: pytest.MonkeyPatch):
    _install_config(monkeypatch, {"FUSION_EVENT_TAB": "FusionEvents"})
    async def _afetch_records(_sheet_id: str, tab_name: str):
        assert tab_name == "FusionEvents"
        return [{
            "fusion_id": "f-1",
            "event_id": "e-1",
            "event_name": "Event 1",
            "event_type": "tournament",
            "category": "arena",
            "start_at_utc": "2026-01-01T00:00:00+00:00",
            "end_at_utc": "2026-01-01T01:00:00+00:00",
            "reward_amount": "10",
            "reward_type": "fragments",
            "embed_title": "{event_label}",
            "embed_description": "Begins {starts_in}",
            "embed_footer": "Footer",
        }]
    monkeypatch.setattr(fusion_sheets, "_sheet_id", lambda: "sheet-1")
    monkeypatch.setattr(fusion_sheets, "afetch_records", _afetch_records)
    rows = asyncio.run(fusion_sheets._load_fusion_events())
    assert rows[0].embed_title == "{event_label}"
    assert rows[0].embed_description == "Begins {starts_in}"
    assert rows[0].embed_footer == "Footer"
