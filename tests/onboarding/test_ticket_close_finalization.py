import asyncio
import datetime as dt
from types import SimpleNamespace

from modules.onboarding import cmd_finishplacement, watcher_promo, watcher_welcome
from shared.sheets import onboarding as onboarding_sheets


def _seed_finalization_config(monkeypatch):
    monkeypatch.setattr(
        onboarding_sheets,
        "_CONFIG_CACHE",
        {
            "welcome_finalization_status_header": "finalization_status",
            "welcome_reservation_status_header": "reservation_status",
            "welcome_clan_update_status_header": "clan_update_status",
            "welcome_finalization_note_header": "finalization_note",
            "promo_finalization_status_header": "finalization_status",
            "promo_reservation_status_header": "reservation_status",
            "promo_clan_update_status_header": "clan_update_status",
            "promo_finalization_note_header": "finalization_note",
            "promo_source_clan_tag_header": "source_clan_tag",
        },
    )
    monkeypatch.setattr(onboarding_sheets, "_CONFIG_CACHE_TS", 9999999999.0)


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
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
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
    assert discord_logs == []
    assert summary["unresolved"] == 1


def test_promo_backfill_closed_row_fetch_fails_marks_skipped_unresolved(monkeypatch):
    row = {
        "ticket number": "M1002",
        "username": "ClosedUser",
        "thread_id": "123",
        "status": "closed",
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
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
    assert discord_logs == []
    assert summary["unresolved"] == 1


def test_welcome_backfill_open_row_fetched_archived_thread_triggers_prompt(monkeypatch):
    row = {
        "ticket_number": "W1003",
        "username": "ArchivedUser",
        "thread_id": "123",
        "status": "open",
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
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
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "finalization_status": "pending",
    }
    prompted = []
    _patch_backfill_rows(monkeypatch, watcher_promo, [(2, row)])
    watcher = watcher_promo.PromoTicketWatcher(_BackfillBot(channel=_BackfillThread(archived=True, name="M1003-ArchivedUser")))
    context = watcher_promo.PromoTicketContext(thread_id=123, ticket_number="M1003", username="ArchivedUser", promo_type="move", thread_created="", year="2026", month="June")
    monkeypatch.setattr(watcher, "_ensure_context", lambda thread: asyncio.sleep(0, result=context))
    monkeypatch.setattr(watcher, "_begin_clan_prompt", lambda thread, context, trigger="manual_backfill": prompted.append((thread, context, trigger)) or asyncio.sleep(0))

    summary = asyncio.run(watcher.run_close_backfill())

    assert prompted and prompted[0][2] == "manual_backfill"
    assert summary["prompt_required"] == 1


def test_welcome_startup_does_not_schedule_close_backfill(monkeypatch):
    created = []
    monkeypatch.setattr(watcher_welcome.asyncio, "create_task", lambda coro, *, name=None: created.append(name))
    watcher = watcher_welcome.WelcomeTicketWatcher(SimpleNamespace())

    assert watcher is not None
    assert "welcome_close_backfill_startup" not in created


def test_welcome_backfill_loader_error_counts_error(monkeypatch):
    monkeypatch.setattr(
        watcher_welcome.onboarding_sheets,
        "list_ticket_rows_for_finalization_backfill",
        lambda flow: (_ for _ in ()).throw(RuntimeError("loader failed")),
    )
    watcher = watcher_welcome.WelcomeTicketWatcher(SimpleNamespace())

    summary = asyncio.run(watcher.run_close_backfill(window_hours=24))

    assert summary["scanned"] == 0
    assert summary["error"] == 1


def test_promo_backfill_loader_error_counts_error(monkeypatch):
    monkeypatch.setattr(
        watcher_promo.onboarding_sheets,
        "list_ticket_rows_for_finalization_backfill",
        lambda flow: (_ for _ in ()).throw(RuntimeError("loader failed")),
    )
    watcher = watcher_promo.PromoTicketWatcher(SimpleNamespace(get_channel=lambda tid: None))

    summary = asyncio.run(watcher.run_close_backfill(window_hours=24))

    assert summary["scanned"] == 0
    assert summary["error"] == 1


def test_old_finalized_rows_outside_window_are_skipped_not_already_done(monkeypatch):
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)
    rows = [
        (
            2,
            {
                "ticket number": "M9999",
                "username": "OldDone",
                "status": "closed",
                "thread_id": "9999",
                "updated_at": old.isoformat(),
                "finalization_status": "done",
            },
        )
    ]
    _patch_backfill_rows(monkeypatch, watcher_promo, rows)
    watcher = watcher_promo.PromoTicketWatcher(SimpleNamespace(get_channel=lambda tid: None))

    summary = asyncio.run(watcher.run_close_backfill(window_hours=24))

    assert summary["already_done"] == 0
    assert summary["skipped_old"] == 1


class _BackfillCtx:
    def __init__(self):
        self.replies = []

    async def reply(self, message, **kwargs):
        self.replies.append((message, kwargs))


class _CommandWatcher:
    def __init__(self):
        self.calls = []

    async def run_close_backfill(self, *, window_hours):
        self.calls.append(window_hours)
        return {
            "scanned": 1,
            "finalized": 0,
            "prompt_required": 0,
            "already_done": 0,
            "unresolved": 0,
            "error": 0,
            "skipped_old": 0,
            "skipped_no_timestamp": 0,
        }


def test_ticketbackfill_rejects_invalid_flow():
    ctx = _BackfillCtx()
    cog = cmd_finishplacement.FinishPlacementCog(SimpleNamespace(get_cog=lambda name: None))

    asyncio.run(cmd_finishplacement.FinishPlacementCog.ticketbackfill.callback(cog, ctx, "bad", "1h"))

    assert ctx.replies
    assert "Invalid flow" in ctx.replies[0][0]


def test_ticketbackfill_rejects_missing_invalid_and_too_large_windows():
    cog = cmd_finishplacement.FinishPlacementCog(SimpleNamespace(get_cog=lambda name: None))

    for window, expected in [(None, "Missing window"), ("bad", "Invalid window"), ("8d", "Window too large")]:
        ctx = _BackfillCtx()
        asyncio.run(cmd_finishplacement.FinishPlacementCog.ticketbackfill.callback(cog, ctx, "welcome", window))
        assert ctx.replies
        assert expected in ctx.replies[0][0]


def test_ticketbackfill_valid_window_calls_watcher_with_window_hours():
    watcher = _CommandWatcher()
    ctx = _BackfillCtx()
    cog = cmd_finishplacement.FinishPlacementCog(
        SimpleNamespace(get_cog=lambda name: watcher if name == "WelcomeTicketWatcher" else None)
    )

    asyncio.run(cmd_finishplacement.FinishPlacementCog.ticketbackfill.callback(cog, ctx, "welcome", "3d"))

    assert watcher.calls == [72]
    assert "flow=welcome" in ctx.replies[0][0]
    assert "window=3d" in ctx.replies[0][0]


def test_ticketbackfill_rejects_overlap(monkeypatch):
    lock = asyncio.Lock()
    asyncio.run(lock.acquire())
    monkeypatch.setattr(cmd_finishplacement, "_TICKET_BACKFILL_LOCK", lock)
    watcher = _CommandWatcher()
    ctx = _BackfillCtx()
    cog = cmd_finishplacement.FinishPlacementCog(SimpleNamespace(get_cog=lambda name: watcher))

    try:
        asyncio.run(cmd_finishplacement.FinishPlacementCog.ticketbackfill.callback(cog, ctx, "all", "1h"))
    finally:
        lock.release()
        monkeypatch.setattr(cmd_finishplacement, "_TICKET_BACKFILL_LOCK", None)

    assert watcher.calls == []
    assert ctx.replies
    assert "already running" in ctx.replies[0][0]


def test_welcome_finalization_state_read_failure_blocks_side_effects(monkeypatch):
    _seed_finalization_config(monkeypatch)
    side_effects = []
    updates = []
    discord_logs = []

    watcher = watcher_welcome.WelcomeTicketWatcher(SimpleNamespace())
    monkeypatch.setattr(watcher, "_load_clan_tags", lambda: asyncio.sleep(0, result=["C1CD"]))
    monkeypatch.setattr(watcher, "_tag_known", lambda tag: True)
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "find_welcome_row", lambda ticket: (_ for _ in ()).throw(RuntimeError("read failed")))
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "update_ticket_finalization_state", lambda *a, **k: updates.append(k) or "updated")
    monkeypatch.setattr(watcher_welcome.reservations_sheets, "find_active_reservations_for_recruit", lambda *a, **k: side_effects.append("reservation_lookup"))
    monkeypatch.setattr(watcher_welcome.availability, "adjust_manual_open_spots", lambda *a, **k: side_effects.append("open_spots"))
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "append_welcome_ticket_row", lambda *a, **k: side_effects.append("welcome_row_write"))

    async def fake_placement_log(**kwargs):
        discord_logs.append(kwargs)

    monkeypatch.setattr(watcher_welcome, "_send_placement_log_line", fake_placement_log)

    context = watcher_welcome.TicketContext(thread_id=123, ticket_number="W2001", username="ReadFail")
    asyncio.run(watcher._finalize_clan_tag(_BackfillThread(name="W2001-ReadFail"), context, "C1CD", actor=None, source="ticket_tool", prompt_message=None, view=None))

    assert side_effects == []
    assert updates and updates[0]["finalization_status"] == "failed"
    assert discord_logs == [{"flow": "welcome", "outcome": "failed", "ticket": "W2001", "reason": "finalization_state_preflight_failed", "action": "manual_check"}]


def test_welcome_finalization_in_progress_write_failure_blocks_side_effects(monkeypatch):
    _seed_finalization_config(monkeypatch)
    side_effects = []
    updates = []
    discord_logs = []

    watcher = watcher_welcome.WelcomeTicketWatcher(SimpleNamespace())
    monkeypatch.setattr(watcher, "_load_clan_tags", lambda: asyncio.sleep(0, result=["C1CD"]))
    monkeypatch.setattr(watcher, "_tag_known", lambda tag: True)
    row = ["W2002", "WriteFail", "", "", "", "", "123", "", "open", "", "", "", "pending", "pending", "pending", ""]
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "find_welcome_row", lambda ticket: (2, row))

    def fake_update(*args, **kwargs):
        updates.append(kwargs)
        if kwargs.get("finalization_status") == "in_progress":
            raise RuntimeError("write failed")
        return "updated"

    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "update_ticket_finalization_state", fake_update)
    monkeypatch.setattr(watcher_welcome.reservations_sheets, "find_active_reservations_for_recruit", lambda *a, **k: side_effects.append("reservation_lookup"))
    monkeypatch.setattr(watcher_welcome.availability, "adjust_manual_open_spots", lambda *a, **k: side_effects.append("open_spots"))
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "append_welcome_ticket_row", lambda *a, **k: side_effects.append("welcome_row_write"))

    async def fake_placement_log(**kwargs):
        discord_logs.append(kwargs)

    monkeypatch.setattr(watcher_welcome, "_send_placement_log_line", fake_placement_log)

    context = watcher_welcome.TicketContext(thread_id=123, ticket_number="W2002", username="WriteFail")
    asyncio.run(watcher._finalize_clan_tag(_BackfillThread(name="W2002-WriteFail"), context, "C1CD", actor=None, source="ticket_tool", prompt_message=None, view=None))

    assert side_effects == []
    assert [update["finalization_status"] for update in updates] == ["in_progress", "failed"]
    assert discord_logs == [{"flow": "welcome", "outcome": "failed", "ticket": "W2002", "reason": "finalization_state_preflight_failed", "action": "manual_check"}]


def test_promo_finalization_state_read_failure_blocks_side_effects(monkeypatch):
    _seed_finalization_config(monkeypatch)
    side_effects = []
    updates = []
    discord_logs = []

    watcher = watcher_promo.PromoTicketWatcher(SimpleNamespace())
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "find_promo_row", lambda ticket: (_ for _ in ()).throw(RuntimeError("read failed")))
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "update_ticket_finalization_state", lambda *a, **k: updates.append(k) or "updated")

    async def fake_cleanup(*args, **kwargs):
        side_effects.append("cleanup")

    monkeypatch.setattr(watcher_promo, "cleanup_reservation_for_ticket_close", fake_cleanup)
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "upsert_promo", lambda *a, **k: side_effects.append("promo_row_write"))
    monkeypatch.setattr(watcher_promo.availability, "adjust_manual_open_spots", lambda *a, **k: side_effects.append("open_spots"))

    async def fake_placement_log(**kwargs):
        discord_logs.append(kwargs)

    monkeypatch.setattr(watcher_promo, "_send_placement_log_line", fake_placement_log)

    context = watcher_promo.PromoTicketContext(thread_id=123, ticket_number="M2001", username="ReadFail", promo_type="move", thread_created="", year="2026", month="June", source_clan_tag="C1CB", clan_tag="C1CD")
    asyncio.run(watcher._complete_close(_BackfillThread(name="M2001-ReadFail"), context, "", "", trigger="ticket_tool"))

    assert side_effects == []
    assert updates and updates[0]["finalization_status"] == "failed"
    assert discord_logs == [{"flow": "promo", "outcome": "failed", "ticket": "M2001", "reason": "finalization_state_preflight_failed", "action": "manual_check"}]


def test_promo_finalization_in_progress_write_failure_blocks_side_effects(monkeypatch):
    _seed_finalization_config(monkeypatch)
    side_effects = []
    updates = []
    discord_logs = []

    watcher = watcher_promo.PromoTicketWatcher(SimpleNamespace())
    row = {"ticket number": "M2002", "username": "WriteFail", "clantag": "C1CD", "source_clan_tag": "C1CB", "finalization_status": "pending", "reservation_status": "pending", "clan_update_status": "pending", "finalization_note": ""}
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "find_promo_row", lambda ticket: (2, row))

    def fake_update(*args, **kwargs):
        updates.append(kwargs)
        if kwargs.get("finalization_status") == "in_progress":
            raise RuntimeError("write failed")
        return "updated"

    monkeypatch.setattr(watcher_promo.onboarding_sheets, "update_ticket_finalization_state", fake_update)

    async def fake_cleanup(*args, **kwargs):
        side_effects.append("cleanup")

    monkeypatch.setattr(watcher_promo, "cleanup_reservation_for_ticket_close", fake_cleanup)
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "upsert_promo", lambda *a, **k: side_effects.append("promo_row_write"))
    monkeypatch.setattr(watcher_promo.availability, "adjust_manual_open_spots", lambda *a, **k: side_effects.append("open_spots"))

    async def fake_placement_log(**kwargs):
        discord_logs.append(kwargs)

    monkeypatch.setattr(watcher_promo, "_send_placement_log_line", fake_placement_log)

    context = watcher_promo.PromoTicketContext(thread_id=123, ticket_number="M2002", username="WriteFail", promo_type="move", thread_created="", year="2026", month="June", source_clan_tag="C1CB", clan_tag="C1CD")
    asyncio.run(watcher._complete_close(_BackfillThread(name="M2002-WriteFail"), context, "", "", trigger="ticket_tool"))

    assert side_effects == []
    assert [update["finalization_status"] for update in updates] == ["in_progress", "failed"]
    assert discord_logs == [{"flow": "promo", "outcome": "failed", "ticket": "M2002", "reason": "finalization_state_preflight_failed", "action": "manual_check"}]


def test_promo_final_close_patch_failure_does_not_mark_closed(monkeypatch):
    _seed_finalization_config(monkeypatch)
    updates = []
    metadata_statuses = []
    side_effects = []

    watcher = watcher_promo.PromoTicketWatcher(SimpleNamespace())
    row = {"ticket number": "M2003", "username": "PatchFail", "clantag": "", "source_clan_tag": "C1CB", "finalization_status": "pending", "reservation_status": "pending", "clan_update_status": "pending", "finalization_note": ""}
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "find_promo_row", lambda ticket: (2, row))
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "update_ticket_finalization_state", lambda *a, **k: updates.append(k) or "updated")
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "patch_promo_final_close", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("required close write failed")))
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "upsert_promo", lambda *a, **k: side_effects.append("upsert"))

    async def fake_metadata(**kwargs):
        metadata_statuses.append(kwargs.get("status"))
        return True

    monkeypatch.setattr(watcher, "_patch_ticket_metadata", fake_metadata)
    monkeypatch.setattr(watcher_promo, "cleanup_reservation_for_ticket_close", lambda *a, **k: side_effects.append("cleanup"))
    monkeypatch.setattr(watcher_promo, "_send_placement_log_line", lambda **kwargs: asyncio.sleep(0))

    context = watcher_promo.PromoTicketContext(thread_id=123, ticket_number="M2003", username="PatchFail", promo_type="move", thread_created="", year="2026", month="June", source_clan_tag="C1CB", clan_tag="C1CD")
    asyncio.run(watcher._complete_close(_BackfillThread(name="M2003-PatchFail"), context, "", "", trigger="ticket_tool"))

    assert "closed" not in metadata_statuses
    assert side_effects == []
    assert [update["finalization_status"] for update in updates] == ["in_progress", "failed"]
    assert "final close patch failed" in updates[-1]["finalization_note"]


def test_promo_missing_source_after_start_records_failed_finalization(monkeypatch):
    _seed_finalization_config(monkeypatch)
    updates = []
    side_effects = []

    watcher = watcher_promo.PromoTicketWatcher(SimpleNamespace())
    row = {"ticket number": "M2007", "username": "NoSource", "clantag": "", "source_clan_tag": "", "finalization_status": "pending", "reservation_status": "pending", "clan_update_status": "pending", "finalization_note": ""}
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "find_promo_row", lambda ticket: (2, row))
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "update_ticket_finalization_state", lambda *a, **k: updates.append(k) or "updated")
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "patch_promo_final_close", lambda **kwargs: side_effects.append("final_close"))
    monkeypatch.setattr(watcher_promo, "cleanup_reservation_for_ticket_close", lambda *a, **k: side_effects.append("cleanup"))

    sent = []

    class Thread(_BackfillThread):
        async def send(self, message):
            sent.append(message)

    context = watcher_promo.PromoTicketContext(thread_id=123, ticket_number="M2007", username="NoSource", promo_type="move", thread_created="", year="2026", month="June", source_clan_tag="", clan_tag="C1CD")
    asyncio.run(watcher._complete_close(Thread(name="M2007-NoSource"), context, "", "", trigger="ticket_tool"))

    assert [update["finalization_status"] for update in updates] == ["in_progress", "failed"]
    assert "source clan context was missing" in updates[-1]["finalization_note"]
    assert side_effects == []
    assert context.state != "closed"
    assert sent


def test_promo_success_closes_in_final_close_patch_not_metadata(monkeypatch):
    _seed_finalization_config(monkeypatch)
    operations = []
    updates = []

    class Thread(_BackfillThread):
        guild = None

        async def edit(self, **kwargs):
            operations.append(("thread_edit", kwargs))

    watcher = watcher_promo.PromoTicketWatcher(SimpleNamespace())
    row = {"ticket number": "M2006", "username": "OrderOk", "clantag": "", "source_clan_tag": "C1CB", "finalization_status": "pending", "reservation_status": "pending", "clan_update_status": "pending", "finalization_note": ""}
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "find_promo_row", lambda ticket: (2, row))
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "update_ticket_finalization_state", lambda *a, **k: operations.append(("state", k.get("finalization_status"))) or updates.append(k) or "updated")

    def fake_final_close(**kwargs):
        operations.append(("final_close", kwargs.get("status"), kwargs.get("source_clan_tag"), kwargs.get("clan_tag"), kwargs.get("date_closed")))
        return "updated"

    monkeypatch.setattr(watcher_promo.onboarding_sheets, "patch_promo_final_close", fake_final_close)

    async def fail_metadata(**kwargs):
        raise AssertionError("successful promo close must not use metadata status patch")

    monkeypatch.setattr(watcher, "_patch_ticket_metadata", fail_metadata)
    monkeypatch.setattr(watcher_promo, "_normalize_clan_math_targets", lambda tags: {})
    monkeypatch.setattr(watcher_promo, "_clan_math_column_indices", lambda: {})
    monkeypatch.setattr(watcher_promo, "_capture_clan_snapshots", lambda *a, **k: {})

    async def fake_cleanup(**kwargs):
        operations.append(("cleanup", None))
        return SimpleNamespace(
            skipped=True,
            ok=True,
            reason=None,
            reservation_row=None,
            old_status=None,
            new_status=None,
            recomputed_tags=[],
            decision_line="decision: ok",
            reservation_label="none",
            applied_open_deltas={},
            source_clan_lookup_key="C1CB",
            source_clan_row_found=True,
            source_clan_row_number=9,
            previous_is_real=True,
            source_clan_not_real_reason=None,
            source_clan_lookup_mode="tag",
        )

    monkeypatch.setattr(watcher_promo, "cleanup_reservation_for_ticket_close", fake_cleanup)
    monkeypatch.setattr(watcher_promo, "_send_placement_log_line", lambda **kwargs: operations.append(("placement_log", kwargs.get("outcome"))) or asyncio.sleep(0))
    monkeypatch.setattr(watcher_promo, "_log_clan_math_event", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr(watcher_promo.onboarding_sessions, "mark_completed", lambda thread_id: operations.append(("session_complete", thread_id)))

    context = watcher_promo.PromoTicketContext(thread_id=123, ticket_number="M2006", username="OrderOk", promo_type="move", thread_created="", year="2026", month="June", source_clan_tag="C1CB", clan_tag="C1CD")
    asyncio.run(watcher._complete_close(Thread(name="M2006-OrderOk"), context, "", "Destination", trigger="ticket_tool"))

    assert operations[0] == ("state", "in_progress")
    assert operations[1][:4] == ("final_close", "closed", "C1CB", "C1CD")
    assert operations[1][4]
    assert operations[2] == ("cleanup", None)
    assert updates[0]["finalization_status"] == "in_progress"
    assert updates[-1]["finalization_status"] == "done"
    assert context.state == "closed"


def test_promo_patch_ticket_metadata_returns_explicit_bool(monkeypatch):
    watcher = watcher_promo.PromoTicketWatcher(SimpleNamespace())
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "patch_promo_ticket_metadata", lambda **kwargs: "updated")
    context = watcher_promo.PromoTicketContext(thread_id=123, ticket_number="M2004", username="MetaOk", promo_type="move", thread_created="", year="2026", month="June")

    result = asyncio.run(watcher._patch_ticket_metadata(phase="test", thread=_BackfillThread(name="M2004-MetaOk"), context=context, status="closed"))

    assert result is True


def test_promo_patch_ticket_metadata_failure_returns_false(monkeypatch):
    watcher = watcher_promo.PromoTicketWatcher(SimpleNamespace())
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "patch_promo_ticket_metadata", lambda **kwargs: (_ for _ in ()).throw(RuntimeError("metadata write failed")))
    context = watcher_promo.PromoTicketContext(thread_id=123, ticket_number="M2005", username="MetaFail", promo_type="move", thread_created="", year="2026", month="June")

    result = asyncio.run(watcher._patch_ticket_metadata(phase="test", thread=_BackfillThread(name="M2005-MetaFail"), context=context, status="closed"))

    assert result is False
