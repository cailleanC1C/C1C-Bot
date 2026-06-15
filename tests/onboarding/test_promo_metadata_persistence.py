import datetime as dt
from types import SimpleNamespace

import pytest

from modules.onboarding import watcher_promo
from shared.sheets import onboarding as onboarding_sheets

LIVE_PROMO_HEADERS = [
    "ticket number",
    "username",
    "clantag",
    "source_clan_tag",
    "date closed",
    "type",
    "thread created",
    "thread_name",
    "user_id",
    "thread_id",
    "panel_message_id",
    "status",
    "review_reason",
    "created_at",
    "updated_at",
    "finalization_status",
    "reservation_status",
    "clan_update_status",
    "finalization_note",
    "year",
    "month",
    "join_month",
    "clan name",
    "progression",
]


class FakeWorksheet:
    id = 1

    def __init__(self, headers=None):
        self.rows = [list(headers or LIVE_PROMO_HEADERS)]
        self.header_updates = []

    def row_values(self, idx):
        return self.rows[idx - 1] if len(self.rows) >= idx else []

    def get_all_values(self):
        return self.rows

    def update(self, cell_range, values):
        if cell_range == "A1":
            self.header_updates.append(list(values[0]))
        row_label = cell_range.split(":", 1)[0]
        row_number = int("".join(ch for ch in row_label if ch.isdigit()) or "1")
        while len(self.rows) < row_number:
            self.rows.append([])
        self.rows[row_number - 1] = list(values[0])

    def append_row(self, row, value_input_option=None):
        self.rows.append(list(row))


@pytest.fixture
def promo_sheet(monkeypatch):
    worksheet = FakeWorksheet()
    monkeypatch.setattr(onboarding_sheets.core, "call_with_backoff", lambda func, *args, **kwargs: func(*args, **kwargs))
    monkeypatch.setattr(onboarding_sheets, "_resolve_onboarding_and_promo_tab", lambda: ("sheet", "PromoFromConfig"))
    monkeypatch.setattr(onboarding_sheets, "_worksheet", lambda tab: worksheet)
    monkeypatch.setattr(onboarding_sheets, "_promo_tab", lambda: "PromoFromConfig")
    monkeypatch.setattr(onboarding_sheets.core, "get_worksheet", lambda sheet_id, tab: worksheet)
    monkeypatch.setattr(onboarding_sheets, "get_promo_source_clan_tag_header", lambda **kwargs: "source_clan_tag")
    monkeypatch.setattr(onboarding_sheets, "get_finalization_headers", lambda flow, **kwargs: {
        "finalization_status": "finalization_status",
        "reservation_status": "reservation_status",
        "clan_update_status": "clan_update_status",
        "finalization_note": "finalization_note",
    })
    return worksheet


def _row_map(ws, row_idx=1):
    return dict(zip(ws.rows[0], ws.rows[row_idx]))


def test_patch_promo_metadata_writes_existing_headers_and_preserves_order(promo_sheet):
    created = dt.datetime(2026, 6, 14, 12, 0, tzinfo=dt.timezone.utc)

    result = onboarding_sheets.patch_promo_ticket_metadata(
        ticket="M0370",
        username="Pastor Coco",
        thread_name="M0370-Pastor-Coco",
        user_id=123456789012345678,
        thread_id=987654321098765432,
        status="open",
        created_at=created,
        updated_at=created,
    )

    assert result == "inserted"
    assert promo_sheet.rows[0] == LIVE_PROMO_HEADERS
    assert promo_sheet.header_updates == []
    row = _row_map(promo_sheet)
    assert row["ticket number"] == "M0370"
    assert row["thread_name"] == "M0370-Pastor-Coco"
    assert row["user_id"] == "123456789012345678"
    assert row["thread_id"] == "987654321098765432"
    assert row["status"] == "open"
    assert row["created_at"] == created.isoformat()
    assert row["updated_at"] == created.isoformat()


def test_patch_promo_metadata_panel_id_no_header_change(promo_sheet):
    promo_sheet.rows.append(["M0370", "Pastor", "C1CE", "C1CV", "", "player move request", "2026", "M0370-Pastor", "", "777", "", "open", "", "old", "old", "pending", "none", "pending", "", "2026", "June", "", "", ""])

    result = onboarding_sheets.patch_promo_ticket_metadata(
        ticket="M0370",
        thread_id=777,
        panel_message_id=444,
        updated_at=dt.datetime(2026, 6, 14, 13, 0, tzinfo=dt.timezone.utc),
    )

    assert result == "updated"
    assert promo_sheet.rows[0] == LIVE_PROMO_HEADERS
    row = _row_map(promo_sheet)
    assert row["panel_message_id"] == "444"
    assert row["clantag"] == "C1CE"
    assert row["finalization_status"] == "pending"


def test_patch_promo_metadata_finalization_preserves_business_and_no_blank_overwrite(promo_sheet):
    promo_sheet.rows.append(["M0370", "Pastor", "C1CE", "C1CV", "2026-06-14", "player move request", "2026-06-14 12:00", "M0370-Pastor", "111", "777", "333", "open", "", "created", "updated", "done", "released", "done", "final", "2026", "June", "", "Clan", "TH15"])

    result = onboarding_sheets.patch_promo_ticket_metadata(
        ticket="M0370",
        thread_id=777,
        thread_name="",
        user_id=None,
        panel_message_id=None,
        status="closed",
        updated_at=dt.datetime(2026, 6, 14, 14, 0, tzinfo=dt.timezone.utc),
    )

    assert result == "updated"
    row = _row_map(promo_sheet)
    assert row["thread_name"] == "M0370-Pastor"
    assert row["user_id"] == "111"
    assert row["panel_message_id"] == "333"
    assert row["status"] == "closed"
    assert row["clantag"] == "C1CE"
    assert row["source_clan_tag"] == "C1CV"
    assert row["finalization_status"] == "done"
    assert row["finalization_note"] == "final"
    assert row["progression"] == "TH15"


def test_patch_promo_metadata_missing_user_id_sets_review_reason(promo_sheet):
    result = onboarding_sheets.patch_promo_ticket_metadata(
        ticket="M0371",
        username="No Mention",
        thread_name="M0371-No-Mention",
        thread_id=778,
        status="open",
        review_reason="user_id unresolved from Ticket Tool intro message",
    )

    assert result == "inserted"
    row = _row_map(promo_sheet)
    assert row["thread_name"] == "M0371-No-Mention"
    assert row["thread_id"] == "778"
    assert row["user_id"] == ""
    assert row["review_reason"] == "user_id unresolved from Ticket Tool intro message"


def test_patch_promo_metadata_missing_required_header_skips_without_header_rewrite(monkeypatch):
    worksheet = FakeWorksheet(headers=[h for h in LIVE_PROMO_HEADERS if h != "thread_id"])
    monkeypatch.setattr(onboarding_sheets.core, "call_with_backoff", lambda func, *args, **kwargs: func(*args, **kwargs))
    monkeypatch.setattr(onboarding_sheets, "_resolve_onboarding_and_promo_tab", lambda: ("sheet", "PromoFromConfig"))
    monkeypatch.setattr(onboarding_sheets.core, "get_worksheet", lambda sheet_id, tab: worksheet)
    monkeypatch.setattr(onboarding_sheets, "get_promo_source_clan_tag_header", lambda **kwargs: "source_clan_tag")

    result = onboarding_sheets.patch_promo_ticket_metadata(ticket="M0372", thread_id=779)

    assert result == "skipped_missing_header"
    assert len(worksheet.rows) == 1
    assert worksheet.header_updates == []


def test_promo_close_backfill_runs_on_startup_with_bounded_runner(monkeypatch):
    calls = []
    monkeypatch.setattr(watcher_promo, "get_promo_channel_id", lambda: 1)
    monkeypatch.setattr(watcher_promo, "get_ticket_tool_bot_id", lambda: 2)
    monkeypatch.setattr(watcher_promo.feature_flags, "is_enabled", lambda name: True)
    monkeypatch.setattr(watcher_promo, "_channel_readable_label", lambda bot, cid: "promo")

    async def fake_backfill(self):
        calls.append("backfill")
        return {"scanned": 0}

    monkeypatch.setattr(watcher_promo.PromoTicketWatcher, "run_close_backfill", fake_backfill)
    watcher = watcher_promo.PromoTicketWatcher(bot=SimpleNamespace())

    import asyncio
    asyncio.run(watcher.on_ready())

    assert calls == ["backfill"]


def test_startup_backfill_includes_recent_unresolved_closed_promo(monkeypatch):
    now = dt.datetime.now(dt.timezone.utc)
    updates = []
    logs = []
    row = {
        "ticket number": "M0401",
        "username": "Recent",
        "status": "closed",
        "thread_id": "444401",
        "updated_at": (now - dt.timedelta(hours=1)).isoformat(),
        "finalization_status": "pending",
        "thread_name": "Closed-M0401-Recent-C1CE",
    }
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "list_ticket_rows_for_finalization_backfill", lambda flow: [(2, row)])
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "update_ticket_finalization_state", lambda *args, **kwargs: updates.append(kwargs) or "updated")

    async def fake_log(**kwargs):
        logs.append(kwargs)

    monkeypatch.setattr(watcher_promo, "_send_placement_log_line", fake_log)
    bot = SimpleNamespace(get_channel=lambda tid: None)
    watcher = watcher_promo.PromoTicketWatcher(bot=bot)

    import asyncio
    summary = asyncio.run(watcher.run_close_backfill())

    assert summary["scanned"] == 1
    assert summary["unresolved"] == 1
    assert updates and updates[0]["finalization_status"] == "skipped_unresolved"
    assert logs and logs[0]["trigger"] == "startup_backfill"


def test_startup_backfill_skips_closed_promo_older_than_48_hours(monkeypatch):
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=49)
    updates = []
    row = {
        "ticket number": "M0402",
        "username": "Old",
        "status": "closed",
        "thread_id": "444402",
        "date closed": old.isoformat(),
        "finalization_status": "pending",
    }
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "list_ticket_rows_for_finalization_backfill", lambda flow: [(2, row)])
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "update_ticket_finalization_state", lambda *args, **kwargs: updates.append(kwargs) or "updated")
    watcher = watcher_promo.PromoTicketWatcher(bot=SimpleNamespace(get_channel=lambda tid: None))

    import asyncio
    summary = asyncio.run(watcher.run_close_backfill())

    assert summary["scanned"] == 0
    assert summary["skipped_old"] == 1
    assert updates == []


def test_startup_backfill_skips_done_inside_48_hours(monkeypatch):
    row = {
        "ticket number": "M0403",
        "username": "Done",
        "status": "closed",
        "thread_id": "444403",
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "finalization_status": "done",
    }
    updates = []
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "list_ticket_rows_for_finalization_backfill", lambda flow: [(2, row)])
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "update_ticket_finalization_state", lambda *args, **kwargs: updates.append(kwargs) or "updated")
    watcher = watcher_promo.PromoTicketWatcher(bot=SimpleNamespace(get_channel=lambda tid: None))

    import asyncio
    summary = asyncio.run(watcher.run_close_backfill())

    assert summary["already_done"] == 1
    assert summary["scanned"] == 0
    assert updates == []


def test_startup_backfill_skips_closed_promo_with_no_usable_timestamp(monkeypatch):
    row = {
        "ticket number": "M0404",
        "username": "NoTime",
        "status": "closed",
        "thread_id": "444404",
        "finalization_status": "pending",
    }
    updates = []
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "list_ticket_rows_for_finalization_backfill", lambda flow: [(2, row)])
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "update_ticket_finalization_state", lambda *args, **kwargs: updates.append(kwargs) or "updated")
    watcher = watcher_promo.PromoTicketWatcher(bot=SimpleNamespace(get_channel=lambda tid: None))

    import asyncio
    summary = asyncio.run(watcher.run_close_backfill())

    assert summary["skipped_no_timestamp"] == 1
    assert summary["scanned"] == 0
    assert updates == []


def test_append_promo_ticket_row_preserves_header_order_and_places_values(promo_sheet):
    created = dt.datetime(2026, 6, 14, 12, 0, tzinfo=dt.timezone.utc)
    result = onboarding_sheets.append_promo_ticket_row(
        "M0500",
        "Player One",
        "C1CE",
        "C1CV",
        "move",
        "2026-06-14 12:00",
        "2026",
        "June",
        "June",
        "Clan One",
        "TH16",
        thread_name="M0500-Player-One",
        user_id=111,
        thread_id=222,
        panel_message_id=333,
        status="open",
        created_at=created,
        updated_at=created,
    )

    assert result == "inserted"
    assert promo_sheet.rows[0] == LIVE_PROMO_HEADERS
    assert promo_sheet.header_updates == []
    row = _row_map(promo_sheet)
    expected = {
        "ticket number": "M0500",
        "username": "Player One",
        "clantag": "C1CE",
        "source_clan_tag": "C1CV",
        "date closed": "",
        "type": "move",
        "thread created": "2026-06-14 12:00",
        "thread_name": "M0500-Player-One",
        "user_id": "111",
        "thread_id": "222",
        "panel_message_id": "333",
        "status": "open",
        "review_reason": "",
        "created_at": created.isoformat(),
        "updated_at": created.isoformat(),
        "finalization_status": "pending",
        "reservation_status": "pending",
        "clan_update_status": "pending",
        "finalization_note": "",
        "year": "2026",
        "month": "June",
        "join_month": "June",
        "clan name": "Clan One",
        "progression": "TH16",
    }
    assert row == expected


def test_upsert_promo_preserves_header_and_existing_metadata(promo_sheet):
    promo_sheet.rows.append(["M0501", "Old", "C1CA", "C1CB", "", "move", "old thread", "thread-name", "111", "222", "333", "open", "keep", "created", "updated", "pending", "pending", "pending", "", "2026", "May", "May", "Clan", "TH15"])
    incoming = [""] * len(LIVE_PROMO_HEADERS)
    values = dict(zip(LIVE_PROMO_HEADERS, incoming))
    values.update({
        "ticket number": "M0501",
        "username": "New",
        "clantag": "C1CE",
        "source_clan_tag": "C1CV",
        "type": "move",
        "year": "2026",
        "month": "June",
        "clan name": "Clan New",
    })
    row_values = [values[h] for h in LIVE_PROMO_HEADERS]

    result = onboarding_sheets.upsert_promo(row_values, LIVE_PROMO_HEADERS)

    assert result == "updated"
    assert promo_sheet.rows[0] == LIVE_PROMO_HEADERS
    assert promo_sheet.header_updates == []
    row = _row_map(promo_sheet)
    assert row["thread_name"] == "thread-name"
    assert row["user_id"] == "111"
    assert row["panel_message_id"] == "333"
    assert row["finalization_status"] == "pending"
    assert row["username"] == "New"
    assert row["clantag"] == "C1CE"


def test_update_ticket_finalization_state_promo_preserves_header_and_metadata(promo_sheet):
    promo_sheet.rows.append(["M0502", "Player", "C1CE", "C1CV", "", "move", "created thread", "thread-name", "111", "222", "333", "closed", "reason", "created", "updated", "pending", "pending", "pending", "", "2026", "June", "June", "Clan", "TH16"])

    result = onboarding_sheets.update_ticket_finalization_state(
        "promo",
        ticket="M0502",
        thread_id=222,
        finalization_status="done",
        reservation_status="released",
        clan_update_status="done",
        finalization_note="finalized",
    )

    assert result == "updated"
    assert promo_sheet.rows[0] == LIVE_PROMO_HEADERS
    assert promo_sheet.header_updates == []
    row = _row_map(promo_sheet)
    assert row["thread_name"] == "thread-name"
    assert row["user_id"] == "111"
    assert row["panel_message_id"] == "333"
    assert row["finalization_status"] == "done"
    assert row["reservation_status"] == "released"
    assert row["clan_update_status"] == "done"
    assert row["finalization_note"] == "finalized"


def test_list_ticket_rows_for_promo_backfill_does_not_mutate_header(promo_sheet):
    promo_sheet.rows.append(["M0503", "Player", "", "", "", "", "", "", "", "222", "", "closed", "", "", dt.datetime.now(dt.timezone.utc).isoformat(), "pending", "", "", "", "", "", "", "", ""])

    rows = onboarding_sheets.list_ticket_rows_for_finalization_backfill("promo")

    assert rows[0][1]["ticket number"] == "M0503"
    assert promo_sheet.rows[0] == LIVE_PROMO_HEADERS
    assert promo_sheet.header_updates == []
