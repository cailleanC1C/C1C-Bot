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


def test_patch_promo_metadata_missing_user_id_sets_precise_review_reason(promo_sheet):
    result = onboarding_sheets.patch_promo_ticket_metadata(
        ticket="M0371",
        username="No Mention",
        thread_name="M0371-No-Mention",
        thread_id=778,
        status="open",
        review_reason="user_id_unresolved",
    )

    assert result == "inserted"
    row = _row_map(promo_sheet)
    assert row["thread_name"] == "M0371-No-Mention"
    assert row["thread_id"] == "778"
    assert row["user_id"] == ""
    assert row["review_reason"] == "missing_user_id"


def test_patch_promo_metadata_valid_user_id_gets_no_unresolved_review_reason(promo_sheet):
    result = onboarding_sheets.patch_promo_ticket_metadata(
        ticket="M0373",
        username="Has User",
        thread_name="M0373-Has-User",
        user_id=384926516792131584,
        thread_id=780,
        status="open",
    )

    assert result == "inserted"
    row = _row_map(promo_sheet)
    assert row["user_id"] == "384926516792131584"
    assert row["review_reason"] == ""


def test_patch_promo_metadata_discord_lookup_failure_uses_lookup_reason(promo_sheet):
    result = onboarding_sheets.patch_promo_ticket_metadata(
        ticket="M0374",
        username="Lookup Failed",
        user_id=384926516792131584,
        thread_id=781,
        status="open",
        review_reason="discord_user_lookup_failed",
    )

    assert result == "inserted"
    row = _row_map(promo_sheet)
    assert row["user_id"] == "384926516792131584"
    assert row["review_reason"] == "discord_user_lookup_failed"


def test_patch_promo_metadata_malformed_user_id_sets_invalid_reason(promo_sheet):
    result = onboarding_sheets.patch_promo_ticket_metadata(
        ticket="M0375",
        username="Bad User",
        user_id="not-a-snowflake",
        thread_id=782,
        status="open",
    )

    assert result == "inserted"
    row = _row_map(promo_sheet)
    assert row["user_id"] == "not-a-snowflake"
    assert row["review_reason"] == "invalid_user_id"


def test_patch_promo_metadata_clears_stale_user_id_unresolved_for_valid_user_id(promo_sheet):
    promo_sheet.rows.append([
        "L0059", "Caillean", "", "", "", "clan lead move request", "", "L0059-Caillean",
        "384926516792131584", "783", "", "open", "user_id_unresolved", "", "", "pending",
        "pending", "pending", "", "2026", "July", "", "", "",
    ])

    result = onboarding_sheets.patch_promo_ticket_metadata(
        ticket="L0059",
        thread_id=783,
        status="open",
    )

    assert result == "updated"
    row = _row_map(promo_sheet)
    assert row["user_id"] == "384926516792131584"
    assert row["review_reason"] == ""


@pytest.mark.parametrize(
    "incoming_reason,existing_reason",
    [
        ("missing_user_id", ""),
        ("", "missing_user_id"),
        ("", "invalid_user_id"),
        ("", "discord_user_lookup_failed"),
    ],
)
def test_patch_promo_metadata_clears_user_id_reasons_when_existing_user_id_is_valid(
    promo_sheet,
    incoming_reason,
    existing_reason,
):
    promo_sheet.rows.append([
        "M0376", "Existing User", "", "", "", "player move request", "", "M0376-Existing-User",
        "384926516792131584", "785", "", "open", existing_reason, "", "", "pending",
        "pending", "pending", "", "2026", "July", "", "", "",
    ])

    result = onboarding_sheets.patch_promo_ticket_metadata(
        ticket="M0376",
        thread_id=785,
        status="open",
        review_reason=incoming_reason,
    )

    assert result == "updated"
    row = _row_map(promo_sheet)
    assert row["user_id"] == "384926516792131584"
    assert row["review_reason"] == ""


def test_patch_promo_metadata_preserves_explicit_new_lookup_failure_for_valid_user_id(promo_sheet):
    promo_sheet.rows.append([
        "M0377", "Lookup Failed", "", "", "", "player move request", "", "M0377-Lookup-Failed",
        "384926516792131584", "786", "", "open", "", "", "", "pending",
        "pending", "pending", "", "2026", "July", "", "", "",
    ])

    result = onboarding_sheets.patch_promo_ticket_metadata(
        ticket="M0377",
        thread_id=786,
        status="open",
        review_reason="discord_user_lookup_failed",
    )

    assert result == "updated"
    row = _row_map(promo_sheet)
    assert row["user_id"] == "384926516792131584"
    assert row["review_reason"] == "discord_user_lookup_failed"


def test_patch_promo_metadata_blank_existing_user_id_legacy_reason_becomes_missing(promo_sheet):
    promo_sheet.rows.append([
        "M0378", "Missing User", "", "", "", "player move request", "", "M0378-Missing-User",
        "", "787", "", "open", "", "", "", "pending",
        "pending", "pending", "", "2026", "July", "", "", "",
    ])

    result = onboarding_sheets.patch_promo_ticket_metadata(
        ticket="M0378",
        thread_id=787,
        status="open",
        review_reason="user_id_unresolved",
    )

    assert result == "updated"
    row = _row_map(promo_sheet)
    assert row["user_id"] == ""
    assert row["review_reason"] == "missing_user_id"


def test_patch_promo_metadata_existing_malformed_user_id_sets_invalid_reason(promo_sheet):
    promo_sheet.rows.append([
        "M0379", "Bad User", "", "", "", "player move request", "", "M0379-Bad-User",
        "not-a-snowflake", "788", "", "open", "", "", "", "pending",
        "pending", "pending", "", "2026", "July", "", "", "",
    ])

    result = onboarding_sheets.patch_promo_ticket_metadata(
        ticket="M0379",
        thread_id=788,
        status="open",
    )

    assert result == "updated"
    row = _row_map(promo_sheet)
    assert row["user_id"] == "not-a-snowflake"
    assert row["review_reason"] == "invalid_user_id"


def test_patch_promo_metadata_preserves_unrelated_review_reason(promo_sheet):
    promo_sheet.rows.append([
        "M0380", "Other Review", "", "", "", "player move request", "", "M0380-Other-Review",
        "384926516792131584", "789", "", "open", "manual_review_needed", "", "", "pending",
        "pending", "pending", "", "2026", "July", "", "", "",
    ])

    result = onboarding_sheets.patch_promo_ticket_metadata(
        ticket="M0380",
        thread_id=789,
        status="open",
    )

    assert result == "updated"
    row = _row_map(promo_sheet)
    assert row["user_id"] == "384926516792131584"
    assert row["review_reason"] == "manual_review_needed"


def test_patch_promo_metadata_clan_lead_move_uses_ticket_creator_user_id(promo_sheet):
    result = onboarding_sheets.patch_promo_ticket_metadata(
        ticket="L0060",
        username="Lead",
        thread_name="L0060-Lead",
        user_id=384926516792131584,
        thread_id=784,
        status="open",
    )

    assert result == "inserted"
    row = _row_map(promo_sheet)
    assert row["type"] == ""
    assert row["user_id"] == "384926516792131584"
    assert row["review_reason"] != "user_id_unresolved"
    assert row["review_reason"] == ""


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


def test_promo_close_backfill_is_not_scheduled_on_startup(monkeypatch):
    calls = []
    created = []
    monkeypatch.setattr(watcher_promo, "get_promo_channel_id", lambda: 1)
    monkeypatch.setattr(watcher_promo, "get_ticket_tool_bot_id", lambda: 2)
    monkeypatch.setattr(watcher_promo.feature_flags, "is_enabled", lambda name: True)
    monkeypatch.setattr(watcher_promo, "_channel_readable_label", lambda bot, cid: "promo")

    async def fake_backfill(self):
        calls.append("backfill")
        return {"scanned": 0}

    monkeypatch.setattr(watcher_promo.PromoTicketWatcher, "run_close_backfill", fake_backfill)
    def fake_create_task(coro, *, name=None):
        created.append(name)
        coro.close()
        return SimpleNamespace(done=lambda: False)

    monkeypatch.setattr(watcher_promo.asyncio, "create_task", fake_create_task)
    watcher = watcher_promo.PromoTicketWatcher(bot=SimpleNamespace())

    import asyncio
    asyncio.run(watcher.on_ready())

    assert calls == []
    assert created == []


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
    assert logs == []


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


def test_promo_backfill_loader_uses_live_headers_without_source_config(promo_sheet, monkeypatch):
    monkeypatch.setattr(onboarding_sheets, "get_promo_source_clan_tag_header", lambda **kwargs: "wrong_source_header")
    promo_sheet.rows.append(["M0600", "Player", "C1CE", "C1CV", "", "move", "", "thread", "111", "222", "", "closed", "", "", dt.datetime.now(dt.timezone.utc).isoformat(), "pending", "", "", "", "2026", "June", "June", "Clan", "TH16"])

    rows = onboarding_sheets.list_ticket_rows_for_finalization_backfill("promo")

    assert rows == [(2, dict(zip(LIVE_PROMO_HEADERS, promo_sheet.rows[1])))]
    assert promo_sheet.rows[0] == LIVE_PROMO_HEADERS
    assert promo_sheet.header_updates == []


def test_promo_backfill_loader_missing_required_header_clear_reason(monkeypatch):
    worksheet = FakeWorksheet(headers=[h for h in LIVE_PROMO_HEADERS if h != "finalization_status"])
    monkeypatch.setattr(onboarding_sheets.core, "call_with_backoff", lambda func, *args, **kwargs: func(*args, **kwargs))
    monkeypatch.setattr(onboarding_sheets, "_promo_tab", lambda: "PromoFromConfig")
    monkeypatch.setattr(onboarding_sheets, "_worksheet", lambda tab: worksheet)

    with pytest.raises(RuntimeError, match=r"Promo backfill missing required header\(s\): finalization_status"):
        onboarding_sheets.list_ticket_rows_for_finalization_backfill("promo")

    assert worksheet.rows[0] == [h for h in LIVE_PROMO_HEADERS if h != "finalization_status"]
    assert worksheet.header_updates == []


def test_promo_startup_backfill_empty_loader_clean_summary_and_header_unchanged(promo_sheet, caplog):
    watcher = watcher_promo.PromoTicketWatcher(bot=SimpleNamespace(get_channel=lambda tid: None))

    import asyncio
    summary = asyncio.run(watcher.run_close_backfill())

    assert summary == {
        "scanned": 0,
        "finalized": 0,
        "prompt_required": 0,
        "already_done": 0,
        "unresolved": 0,
        "error": 0,
        "skipped_old": 0,
        "skipped_no_timestamp": 0,
    }
    assert promo_sheet.rows[0] == LIVE_PROMO_HEADERS
    assert promo_sheet.header_updates == []
    assert not [record for record in caplog.records if record.levelname == "ERROR"]


def test_promo_startup_backfill_loader_error_logs_reason(monkeypatch, caplog):
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "list_ticket_rows_for_finalization_backfill", lambda flow: (_ for _ in ()).throw(RuntimeError("Promo backfill missing required header(s): finalization_status")))
    watcher = watcher_promo.PromoTicketWatcher(bot=SimpleNamespace(get_channel=lambda tid: None))

    import asyncio
    summary = asyncio.run(watcher.run_close_backfill())

    assert summary["scanned"] == 0
    assert summary["error"] == 1
    errors = [record for record in caplog.records if record.name == "c1c.onboarding.promo_watcher" and record.levelname == "ERROR"]
    assert errors
    assert "RuntimeError: Promo backfill missing required header(s): finalization_status" == errors[-1].reason

def test_patch_promo_final_close_preserves_unrelated_metadata(promo_sheet):
    original = [
        "M0392", "M0392-J_Turbo", "", "C1C2", "", "move", "2026-07-01 12:00:00",
        "Closed-M0392-J_Turbo", "111", "222", "333", "prompt_required", "keep-review",
        "created-value", "updated-value", "in_progress", "pending", "pending",
        "finalization started by ticket_tool", "1899", "", "", "", "",
    ]
    promo_sheet.rows.append(original)

    result = onboarding_sheets.patch_promo_final_close(
        ticket="M0392",
        thread_id=222,
        clan_tag="C1CV",
        source_clan_tag="C1C2",
        date_closed="2026-07-07",
        clan_name="Vindicators",
        progression="TH16",
        year="",
        month="",
        join_month="",
    )

    assert result == "updated"
    row = _row_map(promo_sheet)
    assert row["clantag"] == "C1CV"
    assert row["source_clan_tag"] == "C1C2"
    assert row["date closed"] == "2026-07-07"
    assert row["clan name"] == "Vindicators"
    assert row["progression"] == "TH16"
    assert row["review_reason"] == "keep-review"
    assert row["created_at"] == "created-value"
    assert row["user_id"] == "111"
    assert row["thread_id"] == "222"
    assert row["panel_message_id"] == "333"
    assert row["status"] == "prompt_required"
    assert row["finalization_status"] == "in_progress"
    assert row["reservation_status"] == "pending"
    assert row["clan_update_status"] == "pending"
    assert row["finalization_note"] == "finalization started by ticket_tool"
    assert row["year"] == "1899"
    assert row["month"] == ""
    assert row["join_month"] == ""
    assert row["updated_at"] != "updated-value"


def test_patch_promo_final_close_missing_row_fails(promo_sheet):
    with pytest.raises(RuntimeError, match="promo final close row not found"):
        onboarding_sheets.patch_promo_final_close(
            ticket="M404",
            thread_id=404,
            clan_tag="C1CV",
            source_clan_tag="C1C2",
            date_closed="2026-07-07",
            clan_name="Vindicators",
        )

@pytest.mark.parametrize(
    ("missing_header", "normalized_field"),
    [
        ("clantag", "clantag"),
        ("source_clan_tag", "sourceclantag"),
        ("date closed", "dateclosed"),
    ],
)
def test_patch_promo_final_close_missing_required_headers_fail(monkeypatch, missing_header, normalized_field):
    headers = [h for h in LIVE_PROMO_HEADERS if h != missing_header]
    worksheet = FakeWorksheet(headers=headers)
    worksheet.rows.append(["M0392" if h == "ticket number" else "222" if h == "thread_id" else "keep" for h in headers])
    monkeypatch.setattr(onboarding_sheets.core, "call_with_backoff", lambda func, *args, **kwargs: func(*args, **kwargs))
    monkeypatch.setattr(onboarding_sheets, "_resolve_onboarding_and_promo_tab", lambda: ("sheet", "PromoFromConfig"))
    monkeypatch.setattr(onboarding_sheets.core, "get_worksheet", lambda sheet_id, tab: worksheet)
    monkeypatch.setattr(onboarding_sheets, "get_promo_source_clan_tag_header", lambda **kwargs: "source_clan_tag")

    with pytest.raises(RuntimeError) as excinfo:
        onboarding_sheets.patch_promo_final_close(
            ticket="M0392",
            thread_id=222,
            clan_tag="C1CV",
            source_clan_tag="C1C2",
            date_closed="2026-07-07",
        )

    message = str(excinfo.value)
    assert "operation=promo final close" in message
    assert f"normalized_field='{normalized_field}'" in message
    assert "ticket=M0392" in message
    assert "thread_id=222" in message


def test_patch_promo_final_close_missing_optional_headers_do_not_fail(monkeypatch):
    optional = {"clan name", "progression", "year", "month", "join_month", "updated_at"}
    headers = [h for h in LIVE_PROMO_HEADERS if h not in optional]
    worksheet = FakeWorksheet(headers=headers)
    worksheet.rows.append(["M0392" if h == "ticket number" else "222" if h == "thread_id" else "keep" for h in headers])
    monkeypatch.setattr(onboarding_sheets.core, "call_with_backoff", lambda func, *args, **kwargs: func(*args, **kwargs))
    monkeypatch.setattr(onboarding_sheets, "_resolve_onboarding_and_promo_tab", lambda: ("sheet", "PromoFromConfig"))
    monkeypatch.setattr(onboarding_sheets.core, "get_worksheet", lambda sheet_id, tab: worksheet)
    monkeypatch.setattr(onboarding_sheets, "get_promo_source_clan_tag_header", lambda **kwargs: "source_clan_tag")

    result = onboarding_sheets.patch_promo_final_close(
        ticket="M0392",
        thread_id=222,
        clan_tag="C1CV",
        source_clan_tag="C1C2",
        date_closed="2026-07-07",
        clan_name="Vindicators",
        progression="TH16",
        year="2026",
        month="July",
        join_month="July",
    )

    assert result == "updated"
    row = dict(zip(worksheet.rows[0], worksheet.rows[1]))
    assert row["clantag"] == "C1CV"
    assert row["source_clan_tag"] == "C1C2"
    assert row["date closed"] == "2026-07-07"


def test_patch_promo_final_close_can_atomically_mark_closed(promo_sheet):
    promo_sheet.rows.append([
        "M0392", "M0392-J_Turbo", "", "C1C2", "", "move", "2026-07-01 12:00:00",
        "Closed-M0392-J_Turbo", "111", "222", "333", "prompt_required", "keep-review",
        "created-value", "updated-value", "in_progress", "pending", "pending",
        "finalization started by ticket_tool", "", "", "", "", "",
    ])

    result = onboarding_sheets.patch_promo_final_close(
        ticket="M0392",
        thread_id=222,
        clan_tag="C1CV",
        source_clan_tag="C1C2",
        date_closed="2026-07-07",
        status="closed",
    )

    assert result == "updated"
    row = _row_map(promo_sheet)
    assert row["clantag"] == "C1CV"
    assert row["source_clan_tag"] == "C1C2"
    assert row["date closed"] == "2026-07-07"
    assert row["status"] == "closed"
    assert row["review_reason"] == "keep-review"
    assert row["created_at"] == "created-value"
