import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from modules.onboarding import watcher_promo
from shared.sheets import onboarding as onboarding_sheets


_CONFIG_HEADERS = {
    "PROMO_SOURCE_CLAN_TAG_HEADER": "source_clan_tag",
    "PROMO_FINALIZATION_STATUS_HEADER": "finalization_status",
    "PROMO_RESERVATION_STATUS_HEADER": "reservation_status",
    "PROMO_CLAN_UPDATE_STATUS_HEADER": "clan_update_status",
    "PROMO_FINALIZATION_NOTE_HEADER": "finalization_note",
}


class FakeWorksheet:
    id = 1

    def __init__(self):
        self.rows: list[list[str]] = []

    def row_values(self, _idx):
        return self.rows[0] if self.rows else []

    def get_all_values(self):
        return self.rows

    def update(self, cell_range, values):
        if cell_range == "A1":
            self.rows[:1] = [list(values[0])]
            return
        row_label = cell_range.split(":", 1)[0]
        row_number = int("".join(ch for ch in row_label if ch.isdigit()) or "1")
        while len(self.rows) < row_number:
            self.rows.append([])
        self.rows[row_number - 1] = list(values[0])

    def append_row(self, row, value_input_option=None):
        self.rows.append(list(row))


@pytest.fixture
def promo_config(monkeypatch):
    monkeypatch.setattr(
        onboarding_sheets,
        "_config_lookup",
        lambda key, default=None: _CONFIG_HEADERS.get(str(key).upper(), default),
    )


def _install_fake_sheet(monkeypatch):
    worksheet = FakeWorksheet()
    monkeypatch.setattr(
        onboarding_sheets.core,
        "call_with_backoff",
        lambda func, *args, **kwargs: func(*args, **kwargs),
    )
    monkeypatch.setattr(onboarding_sheets, "_resolve_onboarding_and_promo_tab", lambda: ("sheet", "Promo"))
    monkeypatch.setattr(onboarding_sheets, "_worksheet", lambda tab: worksheet)
    monkeypatch.setattr(onboarding_sheets.core, "get_worksheet", lambda sheet_id, tab: worksheet)
    worksheet.rows.append(onboarding_sheets.get_promo_headers())
    return worksheet


def test_promo_source_header_resolves_from_config(monkeypatch):
    monkeypatch.setattr(
        onboarding_sheets,
        "_config_lookup",
        lambda key, default=None: ({**_CONFIG_HEADERS, "PROMO_SOURCE_CLAN_TAG_HEADER": "previous_clan_tag"}).get(str(key).upper(), default),
    )

    assert onboarding_sheets.get_promo_source_clan_tag_header() == "previous_clan_tag"
    assert onboarding_sheets.get_promo_headers()[3] == "previous_clan_tag"


def test_missing_promo_source_header_config_fails_clearly(monkeypatch):
    monkeypatch.setattr(onboarding_sheets, "_config_lookup", lambda key, default=None: default)

    with pytest.raises(RuntimeError, match="PROMO_SOURCE_CLAN_TAG_HEADER"):
        onboarding_sheets.get_promo_headers()


def test_missing_resolved_promo_source_header_fails_clearly(monkeypatch, promo_config):
    headers = list(onboarding_sheets.PROMO_HEADERS)
    headers[3] = "wrong_source_column"

    with pytest.raises(RuntimeError, match="source clan header"):
        onboarding_sheets.require_promo_source_clan_header(headers)


def test_promo_open_row_alignment(monkeypatch, promo_config):
    worksheet = _install_fake_sheet(monkeypatch)
    created = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)

    onboarding_sheets.append_promo_ticket_row(
        "M0352",
        "Lucifer",
        "F-IT",
        "C1CV",
        "player move request",
        "2026-06-08 00:00:00",
        "2026",
        "June",
        "",
        "",
        "",
        thread_name="M0352-Lucifer",
        user_id=12345,
        thread_id=999,
        panel_message_id=444,
        status="open",
        created_at=created,
        updated_at=created,
    )

    header, row = worksheet.rows[0], worksheet.rows[1]
    assert header[3] == "source_clan_tag"
    assert row[0] == "M0352"
    assert row[2] == "F-IT"
    assert row[3] == "C1CV"
    assert row[4] == ""
    assert row[5] == "player move request"
    assert row[6] == "2026-06-08 00:00:00"
    assert row[17] == ""
    assert row[18] == created.isoformat()
    assert row[19] == created.isoformat()


def test_promo_close_row_alignment(monkeypatch, promo_config):
    worksheet = _install_fake_sheet(monkeypatch)
    headers = onboarding_sheets.get_promo_headers()
    row_values = [
        "M0352",
        "Lucifer",
        "F-IT",
        "C1CV",
        "2026-06-08",
        "player move request",
        "2026-06-08 00:00:00",
        "2026",
        "June",
        "",
        "",
        "",
    ]

    onboarding_sheets.upsert_promo(row_values, headers)

    row = worksheet.rows[1]
    assert row[0] == "M0352"
    assert row[3] == "C1CV"
    assert row[4] == "2026-06-08"
    assert row[5] == "player move request"
    assert row[6] == "2026-06-08 00:00:00"


def test_promo_reminder_touch_fallback_alignment(monkeypatch, promo_config):
    captured: dict[str, list[str]] = {}

    async def fake_log_sheet_write(**kwargs):
        return await kwargs["write_coro"]()

    def fake_patch(**kwargs):
        captured["metadata"] = dict(kwargs)
        return "updated"

    monkeypatch.setattr(watcher_promo, "log_sheet_write", fake_log_sheet_write)
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "find_promo_row", lambda ticket: None)
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "patch_promo_ticket_metadata", fake_patch)
    watcher = watcher_promo.PromoTicketWatcher(bot=SimpleNamespace())
    context = watcher_promo.PromoTicketContext(
        thread_id=999,
        ticket_number="M0352",
        username="Lucifer",
        promo_type="player move request",
        thread_created="2026-06-08 00:00:00",
        year="2026",
        month="June",
        clan_tag="F-IT",
        source_clan_tag="C1CV",
        state="open",
        user_id=12345,
    )
    thread = SimpleNamespace(id=999, name="M0352-Lucifer")
    created = datetime(2026, 6, 8, 12, 0, tzinfo=timezone.utc)

    asyncio.run(
        watcher._touch_promo_sheet_for_reminder(
            phase="reminder",
            thread=thread,
            context=context,
            created_at=created,
            user_ref="Lucifer",
        )
    )

    metadata = captured["metadata"]
    assert metadata["ticket"] == "M0352"
    assert metadata["thread_id"] == 999
    assert metadata["thread_name"] == "M0352-Lucifer"
    assert metadata["user_id"] == 12345
    assert metadata["status"] == "open"
    assert metadata["created_at"] == created


def test_existing_promo_sheet_missing_source_header_fails_safely(monkeypatch, promo_config):
    worksheet = _install_fake_sheet(monkeypatch)
    worksheet.rows = [[
        "ticket number",
        "username",
        "clantag",
        "date closed",
        "type",
        "thread created",
    ]]

    with pytest.raises(RuntimeError, match="missing configured source clan header"):
        onboarding_sheets.append_promo_ticket_row(
            "M0352",
            "Lucifer",
            "F-IT",
            "C1CV",
            "player move request",
            "2026-06-08 00:00:00",
            "2026",
            "June",
            "",
            "",
            "",
        )


def test_existing_promo_row_with_blank_source_keeps_later_columns_aligned(monkeypatch, promo_config):
    worksheet = _install_fake_sheet(monkeypatch)
    headers = onboarding_sheets.get_promo_headers()
    worksheet.rows = [headers]
    worksheet.rows.append([
        "M0352",
        "Lucifer",
        "F-IT",
        "",
        "2026-06-08",
        "player move request",
        "2026-06-08 00:00:00",
        "2026",
        "June",
    ])

    found = onboarding_sheets.find_promo_row("M0352")

    assert found is not None
    _row_idx, mapping = found
    assert mapping["source_clan_tag"] == ""
    assert mapping["date closed"] == "2026-06-08"
    assert mapping["type"] == "player move request"
    assert mapping["thread created"] == "2026-06-08 00:00:00"
