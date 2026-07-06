from datetime import datetime, timezone

import pytest

import shared.sheets.onboarding_sessions as sheet_module


@pytest.fixture()
def headers():
    return list(sheet_module.CANONICAL_COLUMNS)


def test_onboarding_sessions_row_mapping(monkeypatch, headers):
    fixed_now = "2025-01-05T12:00:00Z"
    monkeypatch.setattr(sheet_module, "_now_iso", lambda: fixed_now)

    payload = {
        "user_id": "123",
        "thread_id": "456",
        "panel_message_id": "789",
        "step_index": 15,
        "answers": {"foo": "bar"},
        "completed": True,
        "completed_at": datetime(2025, 1, 1, 10, 30, tzinfo=timezone.utc).isoformat(),
        "first_reminder_at": None,
        "warning_sent_at": None,
        "auto_closed_at": None,
    }

    row = sheet_module.build_row(payload, headers=headers)

    assert len(row) == len(headers)
    assert row[headers.index("answers_json")] == "{\"foo\":\"bar\"}"
    assert row[headers.index("updated_at")] == fixed_now
    assert row[headers.index("first_reminder_at")] == ""
    assert row[headers.index("warning_sent_at")] == ""
    assert row[headers.index("auto_closed_at")] == ""


def test_onboarding_sessions_discord_ids_are_sheet_safe_text(headers):
    snowflake = "1523691234567890123"
    payload = {
        "thread_name": "R1234-smurf",
        "user_id": int(snowflake),
        "thread_id": snowflake,
        "panel_message_id": 1523691234567890456,
        "recruiter_summary_message_id": 1523691234567890789,
        "recruiter_summary_player_id": 1523691234567890111,
        "summary_screenshot_prompt_message_id": 1523691234567890222,
        "summary_screenshot_prompt_summary_message_id": 1523691234567890333,
    }

    row = sheet_module.build_row(payload, headers=headers)

    for column in (
        "user_id",
        "thread_id",
        "panel_message_id",
        "recruiter_summary_message_id",
        "recruiter_summary_player_id",
        "summary_screenshot_prompt_message_id",
        "summary_screenshot_prompt_summary_message_id",
    ):
        value = row[headers.index(column)]
        assert isinstance(value, str)
        assert value.isdecimal()
        assert "e" not in value.lower()

    assert row[headers.index("thread_name")] == "R1234-smurf"
    assert row[headers.index("thread_id")] == snowflake


class _FakeSheet:
    def __init__(self, rows):
        self._rows = rows

    def get_all_values(self):
        return self._rows


class _RecordingSheet:
    def __init__(self, rows):
        self._rows = rows
        self.updated: list[tuple[str, list[list[str]]]] = []
        self.appended: list[list[str]] = []

    def get_all_values(self):
        return self._rows

    def update(self, range_, values):
        self.updated.append((range_, values))

    def append_row(self, values):
        self.appended.append(values)


def test_onboarding_sessions_load_mapping(monkeypatch, headers):
    fixed_now = "2025-02-01T09:00:00Z"
    payload = {
        "user_id": 42,
        "thread_id": 84,
        "panel_message_id": 21,
        "step_index": 3,
        "answers": {"alpha": 1},
        "completed": False,
        "completed_at": None,
        "first_reminder_at": datetime(2025, 2, 1, 8, 0, tzinfo=timezone.utc).isoformat(),
        "warning_sent_at": "",
        "auto_closed_at": None,
    }

    monkeypatch.setattr(sheet_module, "_now_iso", lambda: fixed_now)

    row = sheet_module.build_row(payload, headers=headers)
    fake_sheet = _FakeSheet([headers, row])
    monkeypatch.setattr(sheet_module, "_sheet", lambda: fake_sheet)

    result = sheet_module.load(payload["user_id"], payload["thread_id"])

    assert result is not None
    assert result.get("answers", {}).get("alpha") == 1
    assert result.get("first_reminder_at")
    assert result.get("warning_sent_at") == ""
    assert result.get("auto_closed_at") == ""
    assert result.get("updated_at") == fixed_now


def test_onboarding_sessions_header_mismatch_load_noop(monkeypatch):
    bad_headers = ["user_id", "thread_id", "legacy_col"]
    fake_sheet = _FakeSheet([bad_headers, ["1", "2", "legacy"]])
    monkeypatch.setattr(sheet_module, "_sheet", lambda: fake_sheet)

    result = sheet_module.load(1, 2)

    assert result is None


def test_onboarding_sessions_header_mismatch_save_noop(monkeypatch):
    bad_headers = ["user_id", "thread_id", "panel_message_id", "step_index", "completed", "completed_at", "answers_json", "updated_at", "first_reminder_at", "warning_sent_at", "auto_closed_at", "legacy_col"]
    fake_sheet = _RecordingSheet([bad_headers])
    monkeypatch.setattr(sheet_module, "_sheet", lambda: fake_sheet)

    payload = {
        "user_id": 123,
        "thread_id": 456,
        "panel_message_id": 789,
        "step_index": 1,
        "answers": {"foo": "bar"},
        "completed": False,
    }

    sheet_module.save(payload)

    assert not fake_sheet.updated
    assert not fake_sheet.appended


def test_onboarding_sessions_load_all(monkeypatch, headers):
    fixed_now = "2025-03-01T10:00:00Z"
    monkeypatch.setattr(sheet_module, "_now_iso", lambda: fixed_now)

    payload = {
        "user_id": 55,
        "thread_id": 66,
        "panel_message_id": 77,
        "step_index": 2,
        "answers": {"key": "value"},
        "completed": False,
        "completed_at": None,
        "first_reminder_at": None,
        "warning_sent_at": None,
        "auto_closed_at": None,
    }

    row = sheet_module.build_row(payload, headers=headers)
    fake_sheet = _FakeSheet([headers, row])
    monkeypatch.setattr(sheet_module, "_sheet", lambda: fake_sheet)

    rows = sheet_module.load_all()

    assert len(rows) == 1
    assert rows[0]["user_id"] == str(payload["user_id"])
    assert rows[0]["thread_id"] == str(payload["thread_id"])
    assert rows[0]["answers"].get("key") == "value"
