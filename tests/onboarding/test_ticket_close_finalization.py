import asyncio

from modules.onboarding import watcher_promo, watcher_welcome
from shared.sheets import onboarding as onboarding_sheets


def test_finalization_headers_resolve_from_config(monkeypatch):
    monkeypatch.setattr(
        onboarding_sheets,
        "_CONFIG_CACHE",
        {
            "welcome_finalization_status_header": "finalization_status",
            "welcome_reservation_status_header": "reservation_status",
            "welcome_clan_update_status_header": "clan_update_status",
            "welcome_finalization_note_header": "finalization_note",
        },
    )
    monkeypatch.setattr(onboarding_sheets, "_CONFIG_CACHE_TS", 9999999999.0)

    headers = onboarding_sheets.get_welcome_headers()

    assert headers[-4:] == [
        "finalization_status",
        "reservation_status",
        "clan_update_status",
        "finalization_note",
    ]


def test_finalization_state_reads_configured_headers(monkeypatch):
    monkeypatch.setattr(
        onboarding_sheets,
        "_CONFIG_CACHE",
        {
            "promo_finalization_status_header": "finalization_status",
            "promo_reservation_status_header": "reservation_status",
            "promo_clan_update_status_header": "clan_update_status",
            "promo_finalization_note_header": "finalization_note",
            "promo_source_clan_tag_header": "source_clan_tag",
        },
    )
    monkeypatch.setattr(onboarding_sheets, "_CONFIG_CACHE_TS", 9999999999.0)

    state = onboarding_sheets.get_ticket_finalization_state(
        "promo",
        {
            "finalization_status": "done",
            "reservation_status": "released",
            "clan_update_status": "done",
            "finalization_note": "finalized by Ticket Tool close",
        },
    )

    assert state == {
        "finalization_status": "done",
        "reservation_status": "released",
        "clan_update_status": "done",
        "finalization_note": "finalized by Ticket Tool close",
    }


def test_discord_placement_log_is_concise(monkeypatch):
    sent = []

    async def fake_send(message):
        sent.append(message)

    monkeypatch.setattr(watcher_welcome.rt, "send_log_message", fake_send)

    asyncio.run(
        watcher_welcome._send_placement_log_line(
            flow="promo",
            outcome="success",
            ticket="M0354",
            player="Xaereth",
            source="C1CB",
            destination="C1CD",
            trigger="ticket_tool",
            reservation="none",
            clan_update="done",
            finalization_status="done",
        )
    )

    assert sent == [
        "✅ placement finalized • flow=promo • ticket=M0354 • player=Xaereth • C1CB→C1CD • reservation=none • clan_update=done • trigger=ticket_tool • finalization=done"
    ]
    assert "Traceback" not in sent[0]
    assert "{" not in sent[0]


def test_finalization_headers_require_config(monkeypatch):
    monkeypatch.setattr(onboarding_sheets, "_CONFIG_CACHE", {"unrelated": "value"})
    monkeypatch.setattr(onboarding_sheets, "_CONFIG_CACHE_TS", 9999999999.0)

    try:
        onboarding_sheets.get_finalization_headers("welcome")
    except RuntimeError as exc:
        assert "WELCOME_FINALIZATION_STATUS_HEADER" in str(exc)
    else:  # pragma: no cover - explicit guard for required Config behavior
        raise AssertionError("missing finalization Config should fail")


def test_discord_placement_log_outcome_shapes_are_concise(monkeypatch):
    sent = []

    async def fake_send(message):
        sent.append(message)

    monkeypatch.setattr(watcher_welcome.rt, "send_log_message", fake_send)

    async def run():
        await watcher_welcome._send_placement_log_line(
            flow="promo", outcome="prompt", ticket="M0354", player="Xaereth",
            trigger="ticket_tool", reason="missing_source_destination", action="prompted_staff"
        )
        await watcher_welcome._send_placement_log_line(
            flow="promo", outcome="already_done", ticket="M0354", trigger="startup_backfill", action="skipped"
        )
        await watcher_welcome._send_placement_log_line(
            flow="promo", outcome="unresolved", thread="Closed-0354-Xaereth", trigger="ticket_tool", reason="context_not_found", action="skipped"
        )
        await watcher_welcome._send_placement_log_line(
            flow="promo", outcome="partial", ticket="M0354", reservation="released", clan_update="failed", action="manual_check"
        )
        await watcher_welcome._send_placement_log_line(
            flow="promo", outcome="failed", ticket="M0354", reason="sheet_update_failed", action="manual_check"
        )

    asyncio.run(run())

    assert sent == [
        "⚠️ placement needs input • flow=promo • ticket=M0354 • player=Xaereth • trigger=ticket_tool • reason=missing_source_destination • action=prompted_staff",
        "ℹ️ placement already finalized • flow=promo • ticket=M0354 • trigger=backfill • action=skipped",
        "❌ placement unresolved • flow=promo • thread=Closed-0354-Xaereth • trigger=ticket_tool • reason=context_not_found • action=skipped",
        "⚠️ placement partial • flow=promo • ticket=M0354 • reservation=released • clan_update=failed • action=manual_check",
        "❌ placement failed • flow=promo • ticket=M0354 • reason=sheet_update_failed • action=manual_check",
    ]
    assert all("Traceback" not in message and "{" not in message for message in sent)

class _BackfillBot:
    def __init__(self, *, channel=None, fetch_raises=False):
        self.channel = channel
        self.fetch_raises = fetch_raises
        self.fetch_calls = []

    def get_channel(self, channel_id):
        return self.channel

    async def fetch_channel(self, channel_id):
        self.fetch_calls.append(channel_id)
        if self.fetch_raises:
            raise RuntimeError("fetch failed")
        return self.channel


class _BackfillThread:
    def __init__(self, *, archived=False, locked=False, name="W1234-Open"):
        self.id = 123
        self.name = name
        self.archived = archived
        self.locked = locked


def _patch_backfill_rows(monkeypatch, module, rows):
    config = {
        "welcome_finalization_status_header": "finalization_status",
        "welcome_reservation_status_header": "reservation_status",
        "welcome_clan_update_status_header": "clan_update_status",
        "welcome_finalization_note_header": "finalization_note",
        "promo_finalization_status_header": "finalization_status",
        "promo_reservation_status_header": "reservation_status",
        "promo_clan_update_status_header": "clan_update_status",
        "promo_finalization_note_header": "finalization_note",
        "promo_source_clan_tag_header": "source_clan_tag",
    }
    monkeypatch.setattr(onboarding_sheets, "_CONFIG_CACHE", config)
    monkeypatch.setattr(onboarding_sheets, "_CONFIG_CACHE_TS", 9999999999.0)

    async def fake_to_thread(func, *args, **kwargs):
        if func is module.onboarding_sheets.list_ticket_rows_for_finalization_backfill:
            return rows
        return func(*args, **kwargs)

    monkeypatch.setattr(module.asyncio, "to_thread", fake_to_thread)


def test_welcome_backfill_open_row_fetch_fails_does_not_mark_unresolved(monkeypatch):
    row = {
        "ticket_number": "W1001",
        "username": "OpenUser",
        "thread_id": "123",
        "status": "open",
        "finalization_status": "pending",
    }
    updates = []
    discord_logs = []
    _patch_backfill_rows(monkeypatch, watcher_welcome, [(2, row)])
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "update_ticket_finalization_state", lambda *a, **k: updates.append((a, k)))
    async def fake_placement_log(**kwargs):
        discord_logs.append(kwargs)

    monkeypatch.setattr(watcher_welcome, "_send_placement_log_line", fake_placement_log)

    watcher = watcher_welcome.WelcomeTicketWatcher(_BackfillBot(fetch_raises=True))
    summary = asyncio.run(watcher.run_close_backfill())

    assert updates == []
    assert discord_logs == []
    assert summary["unresolved"] == 0


def test_promo_backfill_open_row_fetched_open_thread_does_not_mark_unresolved(monkeypatch):
    row = {
        "ticket number": "M1001",
        "username": "OpenUser",
        "thread_id": "123",
        "status": "open",
        "finalization_status": "pending",
    }
    updates = []
    discord_logs = []
    _patch_backfill_rows(monkeypatch, watcher_promo, [(2, row)])
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "update_ticket_finalization_state", lambda *a, **k: updates.append((a, k)))
    monkeypatch.setattr(watcher_promo, "_send_placement_log_line", lambda **kwargs: discord_logs.append(kwargs))

    watcher = watcher_promo.PromoTicketWatcher(_BackfillBot(channel=_BackfillThread(name="M1001-OpenUser")))
    summary = asyncio.run(watcher.run_close_backfill())

    assert updates == []
    assert discord_logs == []
    assert summary["unresolved"] == 0


def test_welcome_backfill_closed_row_fetch_fails_marks_skipped_unresolved(monkeypatch):
    row = {
        "ticket_number": "W1002",
        "username": "ClosedUser",
        "thread_id": "123",
        "status": "closed",
        "finalization_status": "pending",
    }
    updates = []
    discord_logs = []
    _patch_backfill_rows(monkeypatch, watcher_welcome, [(2, row)])
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "update_ticket_finalization_state", lambda *a, **k: updates.append((a, k)) or "updated")
    async def fake_placement_log(**kwargs):
        discord_logs.append(kwargs)

    monkeypatch.setattr(watcher_welcome, "_send_placement_log_line", fake_placement_log)

    watcher = watcher_welcome.WelcomeTicketWatcher(_BackfillBot(fetch_raises=True))
    summary = asyncio.run(watcher.run_close_backfill())

    assert updates and updates[0][1]["finalization_status"] == "skipped_unresolved"
    assert discord_logs and discord_logs[0]["outcome"] == "unresolved"
    assert summary["unresolved"] == 1


def test_promo_backfill_closed_row_fetch_fails_marks_skipped_unresolved(monkeypatch):
    row = {
        "ticket number": "M1002",
        "username": "ClosedUser",
        "thread_id": "123",
        "status": "closed",
        "finalization_status": "pending",
    }
    updates = []
    discord_logs = []
    _patch_backfill_rows(monkeypatch, watcher_promo, [(2, row)])
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "update_ticket_finalization_state", lambda *a, **k: updates.append((a, k)) or "updated")
    async def fake_placement_log(**kwargs):
        discord_logs.append(kwargs)

    monkeypatch.setattr(watcher_promo, "_send_placement_log_line", fake_placement_log)

    watcher = watcher_promo.PromoTicketWatcher(_BackfillBot(fetch_raises=True))
    summary = asyncio.run(watcher.run_close_backfill())

    assert updates and updates[0][1]["finalization_status"] == "skipped_unresolved"
    assert discord_logs and discord_logs[0]["outcome"] == "unresolved"
    assert summary["unresolved"] == 1


def test_welcome_backfill_open_row_fetched_archived_thread_triggers_prompt(monkeypatch):
    row = {
        "ticket_number": "W1003",
        "username": "ArchivedUser",
        "thread_id": "123",
        "status": "open",
        "finalization_status": "pending",
    }
    prompted = []
    _patch_backfill_rows(monkeypatch, watcher_welcome, [(2, row)])
    watcher = watcher_welcome.WelcomeTicketWatcher(_BackfillBot(channel=_BackfillThread(archived=True, name="W1003-ArchivedUser")))
    monkeypatch.setattr(watcher, "_ensure_context", lambda thread: asyncio.sleep(0, result=watcher_welcome.TicketContext(thread_id=123, ticket_number="W1003", username="ArchivedUser")))
    monkeypatch.setattr(watcher, "_handle_ticket_closed", lambda thread, context, manual=False: prompted.append((thread, context, manual)) or asyncio.sleep(0))

    summary = asyncio.run(watcher.run_close_backfill())

    assert prompted and prompted[0][2] is True
    assert summary["prompt_required"] == 1


def test_promo_backfill_open_row_fetched_archived_thread_triggers_prompt(monkeypatch):
    row = {
        "ticket number": "M1003",
        "username": "ArchivedUser",
        "thread_id": "123",
        "status": "open",
        "finalization_status": "pending",
    }
    prompted = []
    _patch_backfill_rows(monkeypatch, watcher_promo, [(2, row)])
    watcher = watcher_promo.PromoTicketWatcher(_BackfillBot(channel=_BackfillThread(archived=True, name="M1003-ArchivedUser")))
    context = watcher_promo.PromoTicketContext(thread_id=123, ticket_number="M1003", username="ArchivedUser", promo_type="move", thread_created="", year="2026", month="June")
    monkeypatch.setattr(watcher, "_ensure_context", lambda thread: asyncio.sleep(0, result=context))
    monkeypatch.setattr(watcher, "_begin_clan_prompt", lambda thread, context, trigger="startup_backfill": prompted.append((thread, context, trigger)) or asyncio.sleep(0))

    summary = asyncio.run(watcher.run_close_backfill())

    assert prompted and prompted[0][2] == "startup_backfill"
    assert summary["prompt_required"] == 1
