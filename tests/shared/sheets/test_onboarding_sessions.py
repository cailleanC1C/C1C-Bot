from datetime import datetime, timezone

import pytest

import shared.sheets.onboarding_sessions as sheet_module


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


@pytest.fixture()
def headers():
    return list(sheet_module.CANONICAL_COLUMNS)


def _row(payload, headers):
    return sheet_module.build_row(payload, headers=headers)


def test_save_updates_existing_row(monkeypatch, headers):
    existing_payload = {
        "thread_id": "123456789012345678",
        "thread_name": "W0001-user",
        "user_id": "999",
        "step_index": 1,
        "answers": {"foo": "bar"},
    }
    existing_row = _row(existing_payload, headers)
    sheet = _RecordingSheet([headers, existing_row])

    monkeypatch.setattr(sheet_module, "_sheet", lambda: sheet)
    monkeypatch.setattr(sheet_module, "_now_iso", lambda: "2025-12-06T00:00:00Z")

    sheet_module.save({"thread_id": existing_payload["thread_id"], "step_index": 2})

    assert not sheet.appended
    assert len(sheet.updated) == 1
    updated_row = sheet.updated[0][1][0]
    record = sheet_module._record_from_row(updated_row, headers, sheet_module._header_index_map(headers))
    assert record["thread_id"] == existing_payload["thread_id"]
    assert record["user_id"] == existing_payload["user_id"]
    assert str(record["step_index"]) == "2"


def test_save_appends_when_missing(monkeypatch, headers):
    sheet = _RecordingSheet([headers])
    monkeypatch.setattr(sheet_module, "_sheet", lambda: sheet)
    monkeypatch.setattr(sheet_module, "_now_iso", lambda: "2025-12-06T00:00:00Z")

    payload = {
        "thread_id": "987654321098765432",
        "thread_name": "W0002-anon",
        "user_id": "",
        "updated_at": datetime(2025, 12, 6, tzinfo=timezone.utc),
    }

    sheet_module.save(payload)

    assert not sheet.updated
    assert len(sheet.appended) == 1
    appended_row = sheet.appended[0]
    record = sheet_module._record_from_row(appended_row, headers, sheet_module._header_index_map(headers))
    assert record["thread_id"] == payload["thread_id"]
    assert record["thread_name"] == payload["thread_name"]
    assert record["user_id"] == ""
    assert record["updated_at"].startswith("2025-12-06")


def test_thread_id_matching_uses_strings(monkeypatch, headers):
    sheet = _RecordingSheet([headers, _row({"thread_id": "1446838420428030000"}, headers)])
    monkeypatch.setattr(sheet_module, "_sheet", lambda: sheet)
    monkeypatch.setattr(sheet_module, "_now_iso", lambda: "2025-12-06T00:00:00Z")

    payload = {"thread_id": "1446838420428034159", "thread_name": "W9999-user"}
    sheet_module.save(payload)

    assert len(sheet.updated) == 0
    assert len(sheet.appended) == 1
    record = sheet_module._record_from_row(sheet.appended[0], headers, sheet_module._header_index_map(headers))
    assert record["thread_id"] == payload["thread_id"]


def test_completed_at_normalizes_completed_true_in_load(monkeypatch, headers):
    payload = {
        "thread_id": "555",
        "thread_name": "W0555-user",
        "user_id": "111",
        "completed": False,
        "completed_at": "2025-12-06T00:00:00Z",
        "updated_at": "2025-12-06T00:00:00Z",
    }
    sheet = _RecordingSheet([headers, _row(payload, headers)])
    monkeypatch.setattr(sheet_module, "_sheet", lambda: sheet)

    loaded = sheet_module.load(user_id=111, thread_id=555)

    assert loaded is not None
    assert loaded["completed"] is True
    assert loaded["completed_at"] == "2025-12-06T00:00:00Z"
