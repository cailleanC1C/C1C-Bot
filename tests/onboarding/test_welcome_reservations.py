import asyncio
import datetime as dt
import logging
from types import SimpleNamespace

import discord
from discord.ext import commands
import pytest

from modules.onboarding.watcher_welcome import (
    TicketContext,
    WelcomeTicketWatcher,
    ClanSelectView,
    _NO_PLACEMENT_TAG,
    _determine_reservation_decision,
    cleanup_reservation_for_ticket_close,
    is_welcome_ticket_thread_name,
    parse_welcome_thread_name,
    rename_thread_to_reserved,
    _clan_math_column_indices,
)
from shared.sheets import reservations as reservations_sheets
from shared.sheets import onboarding as onboarding_sheets


@pytest.fixture(autouse=True)
def _stub_find_welcome_row(monkeypatch):
    config = {
        "welcome_finalization_status_header": "finalization_status",
        "welcome_reservation_status_header": "reservation_status",
        "welcome_clan_update_status_header": "clan_update_status",
        "welcome_finalization_note_header": "finalization_note",
    }
    monkeypatch.setattr(onboarding_sheets, "_CONFIG_CACHE", config)
    monkeypatch.setattr(onboarding_sheets, "_CONFIG_CACHE_TS", 9999999999.0)

    def _fake_find_welcome_row(ticket):  # type: ignore[no-untyped-def]
        return 2, [ticket or "W0000", "Tester", "", "", "", "", "123", "", "open", "", "", "", "pending", "pending", "pending", ""]

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row",
        _fake_find_welcome_row,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.update_ticket_finalization_state",
        lambda *_args, **_kwargs: "updated",
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome._ensure_fresh_clans_for_placement",
        lambda **_kwargs: asyncio.sleep(0, result=True),
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.get_clan_header_map",
        lambda: {
            "open_spots": 31,
            "inactives": 32,
            "reservation_count": 33,
            "reservation_summary": 34,
            "manual_open_spots": 4,
            "manual_open_spots_seen": 35,
        },
    )

    async def _fake_preflight(_tag, *, delta=0):
        return None

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.preflight_clan_availability_update",
        _fake_preflight,
    )


def _make_reservation(
    tag: str, *, created: dt.datetime | None = None
) -> reservations_sheets.ReservationRow:
    created_at = created or dt.datetime.now(dt.timezone.utc)
    return reservations_sheets.ReservationRow(
        row_number=2,
        thread_id="123",
        ticket_user_id=111,
        recruiter_id=222,
        clan_tag=tag,
        reserved_until=None,
        created_at=created_at,
        status="active",
        notes="",
        username_snapshot="Tester",
        raw=[],
    )


def test_parse_thread_name_open() -> None:
    parts = parse_welcome_thread_name("W0298-Caillean AT")
    assert parts is not None
    assert parts.ticket_code == "W0298"
    assert parts.username == "Caillean AT"
    assert parts.state == "open"


def test_clan_math_column_indices_resolve_by_header(monkeypatch) -> None:
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.get_clan_header_map",
        lambda: {
            "open_spots": 22,
            "inactives": 8,
            "reservation_count": 41,
            "reservation_summary": 3,
        },
    )
    resolved = _clan_math_column_indices()
    assert resolved == {"open_spots": 22, "AF": 22, "AG": 8, "AH": 41, "AI": 3}


def test_clan_math_column_indices_missing_visibility_header_uses_fallback(monkeypatch) -> None:
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.get_clan_header_map",
        lambda: {"open_spots": 22, "inactives": 8, "reservation_count": 41},
    )
    assert _clan_math_column_indices() == {
        "open_spots": 22,
        "AF": 22,
        "AG": 8,
        "AH": 41,
        "AI": 34,
    }


def test_parse_thread_name_open_without_w_prefix() -> None:
    parts = parse_welcome_thread_name("0867-Caillean")
    assert parts is not None
    assert parts.ticket_code == "W0867"
    assert parts.username == "Caillean"
    assert parts.state == "open"


def test_parse_thread_name_open_without_w_prefix_em_dash() -> None:
    parts = parse_welcome_thread_name("0867—Caillean")
    assert parts is not None
    assert parts.ticket_code == "W0867"
    assert parts.username == "Caillean"
    assert parts.state == "open"


def test_parse_thread_name_reserved() -> None:
    parts = parse_welcome_thread_name("Res-W0298-Caillean AT-C1CE")
    assert parts is not None
    assert parts.ticket_code == "W0298"
    assert parts.username == "Caillean AT"
    assert parts.clan_tag == "C1CE"
    assert parts.state == "reserved"


def test_parse_thread_name_closed() -> None:
    parts = parse_welcome_thread_name("Closed-W0298-Caillean AT-NONE")
    assert parts is not None
    assert parts.ticket_code == "W0298"
    assert parts.username == "Caillean AT"
    assert parts.clan_tag == "NONE"
    assert parts.state == "closed"


def test_parse_thread_name_invalid() -> None:
    assert parse_welcome_thread_name("welcome-caillean") is None


def test_welcome_ticket_thread_name_guard_requires_canonical_prefix() -> None:
    assert is_welcome_ticket_thread_name("W0861-something") is True
    assert is_welcome_ticket_thread_name("W1234-") is True
    assert is_welcome_ticket_thread_name("[WK§]ᴹʸᵍᵃʳᵈᴼᴳ") is False
    assert is_welcome_ticket_thread_name("0861-something") is False
    assert is_welcome_ticket_thread_name("Closed-W0861-something") is False


def test_ensure_context_ignores_non_welcome_ticket_thread_before_sheet_lookup(
    monkeypatch, caplog
) -> None:
    watcher = WelcomeTicketWatcher(bot=SimpleNamespace())
    thread = SimpleNamespace(id=9876, name="[WK§]ᴹʸᵍᵃʳᵈᴼᴳ")
    sheet_lookups = []

    async def fake_to_thread(func, *args, **kwargs):
        sheet_lookups.append((func, args, kwargs))
        return None

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.asyncio.to_thread",
        fake_to_thread,
    )

    with caplog.at_level(logging.DEBUG, logger="c1c.onboarding.welcome_watcher"):
        context = asyncio.run(watcher._ensure_context(thread))

    assert context is None
    assert sheet_lookups == []
    assert "ignored_non_welcome_ticket_thread_name" in caplog.text
    assert "close_context_unresolved" not in caplog.text
    assert "close_context_resolved failed reading welcome row by thread_id" not in caplog.text


def test_ensure_context_keeps_lookup_errors_visible_for_valid_ticket_threads(
    monkeypatch, caplog
) -> None:
    watcher = WelcomeTicketWatcher(bot=SimpleNamespace())
    thread = SimpleNamespace(id=9877, name="W0861-something")
    sheet_lookups = []

    async def fake_to_thread(func, *args, **kwargs):
        sheet_lookups.append((func, args, kwargs))
        if func is onboarding_sheets.find_welcome_row_by_thread_id:
            raise RuntimeError("sheet unavailable")
        return func(*args, **kwargs)

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.asyncio.to_thread",
        fake_to_thread,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sessions.get_by_thread_id",
        lambda _thread_id: None,
    )

    with caplog.at_level(logging.WARNING, logger="c1c.onboarding.welcome_watcher"):
        context = asyncio.run(watcher._ensure_context(thread))

    assert context is not None
    assert context.ticket_number == "W0861"
    assert [call[0] for call in sheet_lookups] == [
        onboarding_sheets.find_welcome_row_by_thread_id
    ]
    assert "close_context_resolved failed reading welcome row by thread_id" in caplog.text


def test_decision_reservation_same_clan() -> None:
    row = _make_reservation("C1CE")
    decision = _determine_reservation_decision(
        "C1CE",
        row,
        no_placement_tag=_NO_PLACEMENT_TAG,
        final_is_real=True,
    )
    assert decision.label == "same"
    assert decision.status == "closed_same_clan"
    assert decision.open_deltas == {}
    assert decision.recompute_tags == ["C1CE"]


def test_decision_reservation_moved_clan() -> None:
    row = _make_reservation("C1CE")
    decision = _determine_reservation_decision(
        "VAGR",
        row,
        no_placement_tag=_NO_PLACEMENT_TAG,
        final_is_real=True,
    )
    assert decision.label == "other"
    assert decision.status == "closed_other_clan"
    assert decision.open_deltas == {"C1CE": 1, "VAGR": -1}
    assert set(decision.recompute_tags) == {"C1CE", "VAGR"}


def test_decision_no_reservation_final_real_clan() -> None:
    decision = _determine_reservation_decision(
        "C1CE",
        None,
        no_placement_tag=_NO_PLACEMENT_TAG,
        final_is_real=True,
    )
    assert decision.label == "none"
    assert decision.status is None
    assert decision.open_deltas == {"C1CE": -1}
    assert decision.recompute_tags == ["C1CE"]


def test_decision_no_reservation_switches_final_clan() -> None:
    decision = _determine_reservation_decision(
        "VAGR",
        None,
        no_placement_tag=_NO_PLACEMENT_TAG,
        final_is_real=True,
        previous_final="C1CE",
    )
    assert decision.label == "none"
    assert decision.status is None
    assert decision.open_deltas == {"C1CE": 1, "VAGR": -1}
    assert set(decision.recompute_tags) == {"C1CE", "VAGR"}


def test_decision_reservation_cancelled_with_no_clan() -> None:
    row = _make_reservation("MART")
    decision = _determine_reservation_decision(
        _NO_PLACEMENT_TAG,
        row,
        no_placement_tag=_NO_PLACEMENT_TAG,
        final_is_real=False,
    )
    assert decision.label == "cancelled"
    assert decision.status == "cancelled"
    assert decision.open_deltas == {"MART": 1}
    assert decision.recompute_tags == ["MART"]


def test_decision_no_reservation_no_clan() -> None:
    decision = _determine_reservation_decision(
        _NO_PLACEMENT_TAG,
        None,
        no_placement_tag=_NO_PLACEMENT_TAG,
        final_is_real=False,
    )
    assert decision.label == "none"
    assert decision.status is None
    assert decision.open_deltas == {}
    assert decision.recompute_tags == []


@pytest.fixture(autouse=True)
def _finalization_config_mocks(monkeypatch):
    from shared.sheets import onboarding as onboarding_sheets

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

    def state_update(*_args, **_kwargs):
        return "updated"

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.update_ticket_finalization_state",
        state_update,
    )



class _DummyMessage:
    def __init__(self, thread: "_DummyThread", message_id: int, content: str) -> None:
        self._thread = thread
        self.id = message_id
        self._content = content

    async def edit(
        self, *, content: str | None = None, view: object | None = None
    ) -> None:
        if content is not None:
            self._thread.messages.append(content)


class _DummyThread:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self.name: str | None = None
        self.guild = object()
        self._message_counter = 0

    async def send(self, content: str, **_: object) -> _DummyMessage:
        self.messages.append(content)
        self._message_counter += 1
        return _DummyMessage(self, self._message_counter, content)

    async def edit(self, *, name: str) -> None:
        self.name = name

    async def fetch_message(self, message_id: int) -> _DummyMessage:
        return _DummyMessage(self, message_id, f"fetched:{message_id}")


class _DummyUser:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, content: str, **_: object) -> None:
        self.sent.append(content)


def test_handle_ticket_open_preserves_existing_values(monkeypatch) -> None:
    recorded: dict[str, list[str]] = {}

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_find(ticket: str):  # type: ignore[no-untyped-def]
        return 3, [ticket, "Old Tester", "MART", "2025-01-01 00:00:00"]

    def fake_append_welcome_ticket_row(
        ticket: str,
        username: str,
        clan_value: str,
        closed_value: str,
        **_kwargs,
    ):  # type: ignore[no-untyped-def]
        recorded["row"] = [ticket, username, clan_value, closed_value]
        return "updated"

    async def fake_locate_welcome_message(_thread):  # type: ignore[no-untyped-def]
        return SimpleNamespace(mentions=[])

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row",
        fake_find,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        fake_append_welcome_ticket_row,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.locate_welcome_message",
        fake_locate_welcome_message,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sessions.upsert_session",
        lambda **payload: recorded.setdefault("session", []).append(payload),
    )

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        context = TicketContext(thread_id=1, ticket_number="W0123", username="Tester")
        thread = _DummyThread()
        await watcher._handle_ticket_open(thread, context)
        await bot.close()

    asyncio.run(runner())

    row = recorded.get("row")
    assert row == ["W0123", "Tester", "MART", "2025-01-01 00:00:00"]


def test_ticket_tool_close_prompt_select_completes_welcome_finalization(monkeypatch, caplog) -> None:
    def assert_not_in_event_loop(label: str) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        raise RuntimeError(
            f"{label} must not run inside an active event loop; use the async variant."
        )

    class _PromptThread:
        def __init__(self) -> None:
            self.id = 4242
            self.name = "W4242-CloseTester"
            self.guild = object()
            self.sent_messages: list[_DummyMessage] = []
            self.edited_names: list[str] = []

        async def send(self, content: str | None = None, **kwargs):  # type: ignore[no-untyped-def]
            message = _DummyMessage(self, len(self.sent_messages) + 1, content or "")
            message.kwargs = kwargs
            self.sent_messages.append(message)
            return message

        async def edit(self, *, name: str, **_kwargs) -> None:
            self.name = name
            self.edited_names.append(name)

        async def fetch_message(self, message_id: int) -> _DummyMessage:
            return self.sent_messages[message_id - 1]

    class _Response:
        def __init__(self) -> None:
            self.deferred = False

        async def defer(self) -> None:
            self.deferred = True

    class _Followup:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send(self, content: str, **_kwargs) -> None:
            self.messages.append(content)

    class _Interaction:
        def __init__(self, channel, message) -> None:
            self.channel = channel
            self.message = message
            self.user = SimpleNamespace(id=99)
            self.response = _Response()
            self.followup = _Followup()

    row = ["W4242", "CloseTester", "", "", "", "", "4242", "", "open", "", "", "", "pending", "pending", "pending", ""]
    updates: list[dict[str, object]] = []
    placement_logs: list[dict[str, object]] = []
    written_rows: list[dict[str, object]] = []

    monkeypatch.setattr("modules.onboarding.watcher_welcome.discord.Thread", _PromptThread)

    def fake_find_welcome_row(ticket):  # type: ignore[no-untyped-def]
        assert_not_in_event_loop("find_welcome_row")
        return 2, row

    monkeypatch.setattr("modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row", fake_find_welcome_row)
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.update_ticket_finalization_state",
        lambda *a, **k: (assert_not_in_event_loop("update_ticket_finalization_state"), updates.append(k), "updated")[-1],
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        lambda *a, **k: (assert_not_in_event_loop("append_welcome_ticket_row"), written_rows.append(k), "updated")[-1],
    )
    monkeypatch.setattr("modules.onboarding.watcher_welcome._ensure_fresh_clans_for_placement", lambda **_: asyncio.sleep(0, result=True))
    def fake_find_clan_row(tag, force=False):  # type: ignore[no-untyped-def]
        assert_not_in_event_loop("find_clan_row")
        return (9, ["", "", tag] + [""] * 40)

    monkeypatch.setattr("modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row", fake_find_clan_row)
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.get_clan_header_map",
        lambda: (
            assert_not_in_event_loop("get_clan_header_map"),
            {
                "clan_tag": 2,
                "open_spots": 31,
                "inactives": 32,
                "reservation_count": 33,
                "reservation_summary": 34,
            },
        )[-1],
    )
    monkeypatch.setattr("modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit", lambda *a, **k: asyncio.sleep(0, result=[]))
    monkeypatch.setattr("modules.onboarding.watcher_welcome.availability.preflight_clan_availability_update", lambda *a, **k: asyncio.sleep(0, result=True))
    monkeypatch.setattr("modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr("modules.onboarding.watcher_welcome.availability.recompute_clan_availability", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr("modules.onboarding.watcher_welcome._send_welcome_repair_visibility", lambda: asyncio.sleep(0))
    monkeypatch.setattr("modules.onboarding.watcher_welcome._send_placement_log_line", lambda **kwargs: placement_logs.append(kwargs) or asyncio.sleep(0))
    monkeypatch.setattr("modules.onboarding.watcher_welcome._log_clan_math_event", lambda *a, **k: asyncio.sleep(0))
    monkeypatch.setattr("modules.onboarding.watcher_welcome._log_finalize_summary", lambda *a, **k: None)

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    watcher = WelcomeTicketWatcher(bot)
    watcher._clan_tags = ["C1CD"]
    watcher._clan_tag_set = {"C1CD"}
    context = TicketContext(thread_id=4242, ticket_number="W4242", username="CloseTester")
    thread = _PromptThread()

    async def runner() -> None:
        await watcher._handle_ticket_closed(thread, context, manual=False)
        assert thread.sent_messages
        prompt = thread.sent_messages[-1]
        view = prompt.kwargs["view"]
        interaction = _Interaction(thread, prompt)
        with caplog.at_level(logging.ERROR, logger="c1c.onboarding.welcome_watcher"):
            await view.handle_selection(interaction, "C1CD")
        await bot.close()

    asyncio.run(runner())

    assert "Which clan tag for CloseTester" in thread.sent_messages[0]._content
    assert written_rows and written_rows[0]["status"] == "closed"
    assert updates[0]["finalization_status"] == "prompt_required"
    assert updates[1]["finalization_status"] == "in_progress"
    assert updates[-1]["finalization_status"] == "done"
    assert context.state == "closed"
    assert thread.name == "Closed-W4242-CloseTester-C1CD"
    assert "must not run inside an active event loop" not in caplog.text
    assert "welcome close clan select callback failed" not in caplog.text
    assert any(log.get("outcome") == "success" for log in placement_logs)


def test_clan_select_failure_marker_failure_message_is_truthful(monkeypatch) -> None:
    class _Response:
        async def defer(self) -> None:
            return None

    class _Followup:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send(self, content: str, **_kwargs) -> None:
            self.messages.append(content)

    class _Interaction:
        def __init__(self) -> None:
            self.channel = SimpleNamespace(id=777)
            self.message = None
            self.response = _Response()
            self.followup = _Followup()

    async def fail_finalize(*_args, **_kwargs):
        raise RuntimeError("quota")

    def fail_marker(*_args, **_kwargs):
        raise RuntimeError("marker write failed")

    watcher = SimpleNamespace(finalize_from_interaction=fail_finalize)
    context = TicketContext(thread_id=777, ticket_number="W0777", username="QuotaFail")
    view = ClanSelectView(watcher, context, ["C1CD"])
    interaction = _Interaction()

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.update_ticket_finalization_state",
        fail_marker,
    )

    asyncio.run(view.handle_selection(interaction, "C1CD"))

    assert interaction.followup.messages
    assert "could not mark the ticket failed" in interaction.followup.messages[0]
    assert "ticket was marked failed" not in interaction.followup.messages[0]


def test_clan_select_failure_message_is_truthful(monkeypatch, caplog) -> None:
    class _Response:
        async def defer(self) -> None:
            return None

    class _Followup:
        def __init__(self) -> None:
            self.messages: list[str] = []

        async def send(self, content: str, **_kwargs) -> None:
            self.messages.append(content)

    class _Interaction:
        def __init__(self) -> None:
            self.channel = SimpleNamespace(id=888)
            self.message = None
            self.response = _Response()
            self.followup = _Followup()

    async def fail_finalize(*_args, **_kwargs):
        raise RuntimeError("welcome finalization row not found for ticket=W0888")

    marker_updates: list[dict[str, object]] = []
    watcher = SimpleNamespace(
        finalize_from_interaction=fail_finalize,
        _welcome_finalization_phase="finalization_state_preflight",
    )
    context = TicketContext(thread_id=888, ticket_number="W0888", username="MissingRow")
    view = ClanSelectView(watcher, context, ["C1CD"])
    interaction = _Interaction()

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.update_ticket_finalization_state",
        lambda *a, **k: marker_updates.append(k) or "updated",
    )

    with caplog.at_level(logging.ERROR, logger="c1c.onboarding.welcome_watcher"):
        asyncio.run(view.handle_selection(interaction, "C1CD"))

    assert interaction.followup.messages
    assert "close finalization row is missing" in interaction.followup.messages[0]
    assert "ticket was marked failed" in interaction.followup.messages[0]
    assert marker_updates[-1]["finalization_status"] == "failed"
    assert "RuntimeError" in caplog.text
    assert "welcome finalization row not found for ticket=W0888" in caplog.text
    record = next(
        record
        for record in caplog.records
        if record.message == "welcome close clan select callback failed"
    )
    assert record.error_type == "RuntimeError"
    assert record.error_message == "welcome finalization row not found for ticket=W0888"
    assert record.error_repr == "RuntimeError('welcome finalization row not found for ticket=W0888')"
    assert record.ticket == "W0888"
    assert record.thread_id == 888
    assert record.clan_tag == "C1CD"
    assert record.finalization_phase == "finalization_state_preflight"


def test_finalize_reconciles_when_row_inserted(monkeypatch) -> None:
    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_upsert(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return "inserted"

    reservation_calls: list[str] = []
    adjustments: list[tuple[str, int]] = []
    recomputed: list[str] = []
    human_logs: list[str] = []

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        reservation_calls.append("lookup")
        return []

    async def fake_adjust(tag: str, delta: int):
        adjustments.append((tag, delta))

    async def fake_recompute(tag: str, *, guild=None):  # type: ignore[no-untyped-def]
        recomputed.append(tag)

    def fake_find_clan(tag: str, *, force=False):  # type: ignore[no-untyped-def]
        return tag, ["", "", tag]

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        fake_upsert,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit",
        fake_find_reservations,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots",
        fake_adjust,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.recompute_clan_availability",
        fake_recompute,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row",
        fake_find_clan,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.human_log",
        SimpleNamespace(
            human=lambda level, message: human_logs.append(f"{level}:{message}")
        ),
    )

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CE", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)

        context = TicketContext(
            thread_id=1,
            ticket_number="W0123",
            username="Tester",
            recruit_id=111,
            recruit_display="Tester",
        )

        thread = _DummyThread()

        await watcher._finalize_clan_tag(
            thread,
            context,
            "C1CE",
            actor=None,
            source="test",
            prompt_message=None,
            view=None,
        )

        await bot.close()

    asyncio.run(runner())

    assert (
        reservation_calls
    ), "should look up reservations even when the row was inserted"
    assert adjustments == [("C1CE", -1)]
    assert recomputed == ["C1CE"]
    assert human_logs, "human log entry should be emitted"


def test_ticket_open_with_mention_writes_welcome_sheet(monkeypatch) -> None:
    recorded: dict[str, object] = {}

    async def fake_locate_welcome_message(_thread):  # type: ignore[no-untyped-def]
        class _Mention:
            def __init__(self, user_id: int) -> None:
                self.id = user_id

        return SimpleNamespace(mentions=[_Mention(7777)])

    def fake_append_welcome_ticket_row(
        ticket: str,
        username: str,
        clan_value: str,
        closed_value: str,
        **kwargs,
    ):  # type: ignore[no-untyped-def]
        recorded["welcome_row"] = {
            "ticket": ticket,
            "username": username,
            "clan_value": clan_value,
            "closed_value": closed_value,
            "user_id": kwargs.get("user_id"),
        }
        return "inserted"

    async def fake_persist_session(**kwargs):  # type: ignore[no-untyped-def]
        recorded.setdefault("sessions", []).append(kwargs)

    async def fake_save_welcome_ticket(ticket_number: str, username: str):
        recorded.setdefault("welcome_sheet", []).append((ticket_number, username))

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.locate_welcome_message",
        fake_locate_welcome_message,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        fake_append_welcome_ticket_row,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.persist_session_for_thread",
        fake_persist_session,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.welcome_tickets.save",
        fake_save_welcome_ticket,
    )

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        thread = SimpleNamespace(
            id=123,
            name="W0608-smurf",
            created_at=dt.datetime(2025, 1, 1, tzinfo=dt.timezone.utc),
        )
        context = TicketContext(thread_id=123, ticket_number="W0608", username="Smurf")
        await watcher._handle_ticket_open(thread, context)
        await bot.close()

    asyncio.run(runner())

    assert recorded["welcome_row"] == {
        "ticket": "W0608",
        "username": "Smurf",
        "clan_value": "",
        "closed_value": "",
        "user_id": 7777,
    }
    if recorded.get("sessions"):
        session_payload = recorded["sessions"][0]
        assert session_payload["user_id"] == 7777
    if recorded.get("welcome_sheet"):
        assert recorded.get("welcome_sheet") == [("W0608", "smurf")]


def test_ticket_open_without_mention_avoids_fallback_user(monkeypatch) -> None:
    recorded: dict[str, object] = {}

    async def fake_locate_welcome_message(_thread):  # type: ignore[no-untyped-def]
        return SimpleNamespace(mentions=[], author=SimpleNamespace(id=9999))

    def fake_append_welcome_ticket_row(
        ticket: str,
        username: str,
        clan_value: str,
        closed_value: str,
        **kwargs,
    ):  # type: ignore[no-untyped-def]
        recorded["welcome_row"] = kwargs.get("user_id")
        return "inserted"

    async def fake_save_welcome_ticket(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        recorded["welcome_sheet_called"] = True

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.locate_welcome_message",
        fake_locate_welcome_message,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        fake_append_welcome_ticket_row,
    )

    async def fake_persist_session(**kwargs):  # type: ignore[no-untyped-def]
        recorded.setdefault("sessions", []).append(kwargs)

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.persist_session_for_thread",
        fake_persist_session,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.welcome_tickets.save",
        fake_save_welcome_ticket,
    )

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        thread = SimpleNamespace(
            id=456,
            name="W0707-woozy",
            created_at=dt.datetime(2025, 2, 2, tzinfo=dt.timezone.utc),
        )
        context = TicketContext(thread_id=456, ticket_number="W0707", username="Woozy")
        await watcher._handle_ticket_open(thread, context)
        await bot.close()

    asyncio.run(runner())

    assert recorded["welcome_row"] is None
    assert "sessions" not in recorded
    assert "welcome_sheet_called" not in recorded


def test_finalize_skips_when_upsert_unexpected(monkeypatch, caplog) -> None:
    def fake_upsert(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return "unknown"

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return []

    async def fail_async(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError(
            "should not run durable reconciliation mutations when row is unknown"
        )

    def fake_find_clan(tag: str, *, force=False):  # type: ignore[no-untyped-def]
        row = [""] * 35
        row[2] = tag
        row[31] = "1"
        row[32] = "0"
        row[33] = "0"
        row[34] = ""
        return 7, row

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        fake_upsert,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit",
        fake_find_reservations,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots",
        fail_async,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.recompute_clan_availability",
        fail_async,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row",
        fake_find_clan,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.get_clan_header_map",
        lambda: {
            "open_spots": 31,
            "inactives": 32,
            "reservation_count": 33,
            "reservation_summary": 34,
        },
    )

    caplog.set_level(logging.WARNING, logger="c1c.onboarding.welcome_watcher")

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CE", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        context = TicketContext(thread_id=1, ticket_number="W0999", username="Tester")
        thread = _DummyThread()
        await watcher._finalize_clan_tag(
            thread,
            context,
            "C1CE",
            actor=None,
            source="test",
            prompt_message=None,
            view=None,
        )
        await bot.close()

    asyncio.run(runner())

    assert any(
        "onboarding_row_missing" in record.getMessage() for record in caplog.records
    ), "should log skip reason when row cannot be confirmed"


def test_finalize_no_reservation_consumes_open_spot(monkeypatch, caplog) -> None:
    adjustments: list[tuple[str, int]] = []
    recomputed: list[str] = []

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_upsert(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return "updated"

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return []

    async def fail_update(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("should not update reservation status when none exist")

    async def fake_adjust(tag: str, delta: int):
        adjustments.append((tag, delta))

    async def fake_recompute(tag: str, guild=None):  # type: ignore[no-untyped-def]
        recomputed.append(tag)

    def fake_find_clan(tag: str, *, force=False):  # type: ignore[no-untyped-def]
        return tag, ["", "", tag]

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        fake_upsert,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit",
        fake_find_reservations,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.update_reservation_status",
        fail_update,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots",
        fake_adjust,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.recompute_clan_availability",
        fake_recompute,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row",
        fake_find_clan,
    )

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CE", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        context = TicketContext(thread_id=1, ticket_number="W0456", username="Tester")
        context.state = "awaiting_clan"
        thread = _DummyThread()
        caplog.set_level(logging.INFO, logger="c1c.onboarding.welcome_watcher")
        await watcher._finalize_clan_tag(
            thread,
            context,
            "C1CE",
            actor=None,
            source="test",
            prompt_message=None,
            view=None,
        )
        await bot.close()

    asyncio.run(runner())

    assert ("C1CE", -1) in adjustments
    assert "C1CE" in recomputed
    log_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "c1c.onboarding.welcome_watcher"
        and record.levelno == logging.INFO
    ]
    assert (
        "✅ welcome_close — ticket=W0456 • user=Tester • final=C1CE • reservation=none • result=ok"
        in log_messages
    )


def test_finalize_manual_logs_manual_event(monkeypatch, caplog) -> None:
    adjustments: list[tuple[str, int]] = []
    recomputed: list[str] = []

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_upsert(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return "updated"

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return []

    async def fail_update(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("should not update reservation status when none exist")

    async def fake_adjust(tag: str, delta: int):
        adjustments.append((tag, delta))

    async def fake_recompute(tag: str, guild=None):  # type: ignore[no-untyped-def]
        recomputed.append(tag)

    def fake_find_clan(tag: str, *, force=False):  # type: ignore[no-untyped-def]
        return tag, ["", "", tag]

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        fake_upsert,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit",
        fake_find_reservations,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.update_reservation_status",
        fail_update,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots",
        fake_adjust,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.recompute_clan_availability",
        fake_recompute,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row",
        fake_find_clan,
    )

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CE", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        context = TicketContext(thread_id=1, ticket_number="W1456", username="Tester")
        context.state = "awaiting_clan"
        context.close_source = "manual_fallback"
        thread = _DummyThread()
        caplog.set_level(logging.INFO, logger="c1c.onboarding.welcome_watcher")
        await watcher._finalize_clan_tag(
            thread,
            context,
            "C1CE",
            actor=None,
            source="test",
            prompt_message=None,
            view=None,
        )
        await bot.close()

    asyncio.run(runner())

    assert ("C1CE", -1) in adjustments
    assert "C1CE" in recomputed
    log_messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "c1c.onboarding.welcome_watcher"
        and record.levelno == logging.INFO
    ]
    assert (
        "⚠️ welcome_close_manual — ticket=W1456 • user=Tester • final=C1CE "
        "• reservation=none • result=ok • source=manual_fallback" in log_messages
    )


def test_finalize_manual_consumes_seat_without_reservation(monkeypatch) -> None:
    adjustments: list[tuple[str, int]] = []
    recomputed: list[str] = []
    rows: list[list[str]] = []

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_upsert(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        rows.append(list(_args[:4]))
        return "inserted"

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return []

    async def fake_adjust(tag: str, delta: int):
        adjustments.append((tag, delta))

    async def fake_recompute(tag: str, guild=None):  # type: ignore[no-untyped-def]
        recomputed.append(tag)

    def fake_find_clan(tag: str, *, force=False):  # type: ignore[no-untyped-def]
        return tag, ["", "", tag]

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        fake_upsert,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit",
        fake_find_reservations,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots",
        fake_adjust,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.recompute_clan_availability",
        fake_recompute,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row",
        fake_find_clan,
    )

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CE", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        context = TicketContext(thread_id=1, ticket_number="W2222", username="Tester")
        context.state = "awaiting_clan"
        context.close_source = "manual_fallback"
        thread = _DummyThread()
        await watcher._finalize_clan_tag(
            thread,
            context,
            "C1CE",
            actor=None,
            source="manual_test",
            prompt_message=None,
            view=None,
        )
        await bot.close()

    asyncio.run(runner())

    assert ("C1CE", -1) in adjustments
    assert recomputed == ["C1CE"]
    assert rows and rows[0][2] == "C1CE"


def test_finalize_rejects_unknown_tag_sends_notice(monkeypatch) -> None:
    async def fail_to_thread(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("upsert should not run for invalid tags")

    monkeypatch.setattr("asyncio.to_thread", fail_to_thread)

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CE", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        context = TicketContext(thread_id=1, ticket_number="W2000", username="Tester")
        context.state = "awaiting_clan"
        thread = _DummyThread()
        actor = _DummyUser()
        await watcher._finalize_clan_tag(
            thread,
            context,
            "unknown",
            actor=actor,
            source="message",
            prompt_message=None,
            view=None,
        )
        await bot.close()

        assert actor.sent
        assert "clan tag" in actor.sent[0]
        assert thread.messages == []

    asyncio.run(runner())


def test_finalize_matching_reservation(monkeypatch) -> None:
    status_updates: list[tuple[int, str]] = []
    adjustments: list[tuple[str, int]] = []

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_upsert(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return "updated"

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return [_make_reservation("C1CE")]

    async def fake_update(row_number: int, status: str):
        status_updates.append((row_number, status))

    async def fake_adjust(tag: str, delta: int):
        adjustments.append((tag, delta))

    async def fake_recompute(tag: str, guild=None):  # type: ignore[no-untyped-def]
        pass

    def fake_find_clan(tag: str, *, force=False):  # type: ignore[no-untyped-def]
        return tag, ["", "", tag]

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        fake_upsert,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit",
        fake_find_reservations,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.update_reservation_status",
        fake_update,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots",
        fake_adjust,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.recompute_clan_availability",
        fake_recompute,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row",
        fake_find_clan,
    )

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CE", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        context = TicketContext(
            thread_id=1,
            ticket_number="W0007",
            username="Tester",
            recruit_id=111,
            recruit_display="Tester",
        )
        context.state = "awaiting_clan"
        thread = _DummyThread()
        await watcher._finalize_clan_tag(
            thread,
            context,
            "C1CE",
            actor=None,
            source="test",
            prompt_message=None,
            view=None,
        )
        await bot.close()

        assert context.reservation_label == "same"

    asyncio.run(runner())

    assert adjustments == []
    assert status_updates == [(2, "closed_same_clan")]


def test_finalize_moved_reservation(monkeypatch) -> None:
    adjustments: list[tuple[str, int]] = []
    status_updates: list[tuple[int, str]] = []

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_upsert(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return "updated"

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return [_make_reservation("MART")]

    async def fake_update(row_number: int, status: str):
        status_updates.append((row_number, status))

    async def fake_adjust(tag: str, delta: int):
        adjustments.append((tag, delta))

    async def fake_recompute(tag: str, guild=None):  # type: ignore[no-untyped-def]
        pass

    def fake_find_clan(tag: str, *, force=False):  # type: ignore[no-untyped-def]
        return tag, ["", "", tag]

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        fake_upsert,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit",
        fake_find_reservations,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.update_reservation_status",
        fake_update,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots",
        fake_adjust,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.recompute_clan_availability",
        fake_recompute,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row",
        fake_find_clan,
    )

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CE", "MART", "VAGR", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        context = TicketContext(
            thread_id=1,
            ticket_number="W0008",
            username="Tester",
            recruit_id=111,
            recruit_display="Tester",
        )
        context.state = "awaiting_clan"
        thread = _DummyThread()
        await watcher._finalize_clan_tag(
            thread,
            context,
            "VAGR",
            actor=None,
            source="test",
            prompt_message=None,
            view=None,
        )
        await bot.close()

        assert context.reservation_label == "other"

    asyncio.run(runner())

    assert ("MART", 1) in adjustments
    assert ("VAGR", -1) in adjustments
    assert status_updates == [(2, "closed_other_clan")]


def test_finalize_none_tag_cancels_reservation(monkeypatch) -> None:
    adjustments: list[tuple[str, int]] = []
    status_updates: list[tuple[int, str]] = []

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_upsert(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return "updated"

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return [_make_reservation("MART")]

    async def fake_update(row_number: int, status: str):
        status_updates.append((row_number, status))

    async def fake_adjust(tag: str, delta: int):
        adjustments.append((tag, delta))

    async def fake_recompute(tag: str, guild=None):  # type: ignore[no-untyped-def]
        pass

    def fail_find_clan(tag: str):  # type: ignore[no-untyped-def]
        raise AssertionError("should not look up clan row for NONE")

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        fake_upsert,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit",
        fake_find_reservations,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.update_reservation_status",
        fake_update,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots",
        fake_adjust,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.recompute_clan_availability",
        fake_recompute,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row",
        fail_find_clan,
    )

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["MART", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        context = TicketContext(
            thread_id=1,
            ticket_number="W0009",
            username="Tester",
            recruit_id=111,
            recruit_display="Tester",
        )
        context.state = "awaiting_clan"
        thread = _DummyThread()
        await watcher._finalize_clan_tag(
            thread,
            context,
            _NO_PLACEMENT_TAG,
            actor=None,
            source="test",
            prompt_message=None,
            view=None,
        )
        await bot.close()

        assert context.reservation_label == "cancelled"

    asyncio.run(runner())

    assert adjustments == [("MART", 1)]
    assert status_updates == [(2, "cancelled")]


def test_finalize_none_tag_without_reservation(monkeypatch) -> None:
    adjustments: list[tuple[str, int]] = []
    status_updates: list[tuple[int, str]] = []

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_upsert(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return "updated"

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return []

    async def fake_update(row_number: int, status: str):
        status_updates.append((row_number, status))

    async def fake_adjust(tag: str, delta: int):
        adjustments.append((tag, delta))

    recomputed: list[str] = []

    async def fake_recompute(tag: str, guild=None):  # type: ignore[no-untyped-def]
        recomputed.append(tag)

    def fail_find_clan(tag: str):  # type: ignore[no-untyped-def]
        raise AssertionError("should not look up clan row for NONE")

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        fake_upsert,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit",
        fake_find_reservations,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.update_reservation_status",
        fake_update,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots",
        fake_adjust,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.recompute_clan_availability",
        fake_recompute,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row",
        fail_find_clan,
    )

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["MART", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        context = TicketContext(
            thread_id=1,
            ticket_number="W0010",
            username="Tester",
            recruit_id=222,
            recruit_display="Tester",
        )
        context.state = "awaiting_clan"
        thread = _DummyThread()
        await watcher._finalize_clan_tag(
            thread,
            context,
            _NO_PLACEMENT_TAG,
            actor=None,
            source="test",
            prompt_message=None,
            view=None,
        )
        await bot.close()

        assert context.reservation_label == "none"

    asyncio.run(runner())

    assert adjustments == []
    assert status_updates == []
    assert recomputed == []


def test_finalize_posts_clan_math_log(monkeypatch) -> None:
    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_upsert(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return "updated"

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return []

    base_row = [""] * 35
    base_row[2] = "C1CE"
    base_row[4] = "3"
    base_row[31] = "3"
    base_row[32] = "0"
    base_row[33] = "0"
    base_row[34] = ""
    clan_rows: dict[str, dict[str, object]] = {
        "C1CE": {"row_number": 12, "values": list(base_row)}
    }

    def _normalize(tag: str) -> str:
        return "".join(ch for ch in tag.upper() if ch.isalnum())

    def fake_find_clan_row(tag: str, *, force=False):  # type: ignore[no-untyped-def]
        entry = clan_rows.get(_normalize(tag))
        if not entry:
            return None
        return entry["row_number"], list(entry["values"])

    async def fake_adjust(tag: str, delta: int):
        entry = clan_rows[_normalize(tag)]
        values = entry["values"]  # type: ignore[assignment]
        current = int(values[4])
        new_value = current + delta
        values[4] = str(new_value)
        return new_value

    async def fake_recompute(tag: str, guild=None):  # type: ignore[no-untyped-def]
        entry = clan_rows[_normalize(tag)]
        values = entry["values"]  # type: ignore[assignment]
        manual = int(values[4])
        values[31] = str(manual)
        values[33] = "0"
        values[34] = ""

    log_messages: list[str] = []

    async def fake_send_log(message: str) -> None:
        log_messages.append(message)

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        fake_upsert,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit",
        fake_find_reservations,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots",
        fake_adjust,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.recompute_clan_availability",
        fake_recompute,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row",
        fake_find_clan_row,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.get_clan_header_map",
        lambda: {
            "open_spots": 31,
            "inactives": 32,
            "reservation_count": 33,
            "reservation_summary": 34,
            "manual_open_spots": 4,
            "manual_open_spots_seen": 35,
        },
    )

    async def _fake_preflight(_tag, *, delta=0):
        return None

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.preflight_clan_availability_update",
        _fake_preflight,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.rt.send_log_message", fake_send_log
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.get_admin_role_ids", lambda: set()
    )

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CE", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        context = TicketContext(
            thread_id=1,
            ticket_number="W0456",
            username="Tester",
            recruit_id=333,
            recruit_display="Tester",
        )
        context.state = "awaiting_clan"
        thread = _DummyThread()
        await watcher._finalize_clan_tag(
            thread,
            context,
            "C1CE",
            actor=None,
            source="select",
            prompt_message=None,
            view=None,
        )
        await bot.close()

    asyncio.run(runner())

    assert log_messages, "clan math log should be emitted"
    message = log_messages[-1]
    assert "W0456" in message
    assert "Tester" in message
    assert "→ C1CE" in message
    assert "source=ticket_tool" in message
    assert "reservation=none" in message
    assert "result=ok" in message
    assert "decision_result=applied_open_delta" in message
    assert "- C1CE row 12" in message
    assert "open_spots: 3 → 2" in message
    assert "AF: 3 → 2" in message
    assert "<@&" not in message


def test_finalize_error_pings_admins(monkeypatch) -> None:
    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_upsert(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return "updated"

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return []

    base_row = [""] * 35
    base_row[2] = "C1CM"
    base_row[4] = "4"
    base_row[31] = "4"
    clan_rows: dict[str, dict[str, object]] = {
        "C1CM": {"row_number": 7, "values": list(base_row)}
    }

    def _normalize(tag: str) -> str:
        return "".join(ch for ch in tag.upper() if ch.isalnum())

    def fake_find_clan_row(tag: str, *, force=False):  # type: ignore[no-untyped-def]
        entry = clan_rows.get(_normalize(tag))
        if not entry:
            return None
        return entry["row_number"], list(entry["values"])

    async def failing_adjust(tag: str, delta: int):
        raise RuntimeError("boom")

    async def fake_recompute(tag: str, guild=None):  # type: ignore[no-untyped-def]
        return None

    log_messages: list[str] = []

    async def fake_send_log(message: str) -> None:
        log_messages.append(message)

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        fake_upsert,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit",
        fake_find_reservations,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots",
        failing_adjust,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.recompute_clan_availability",
        fake_recompute,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row",
        fake_find_clan_row,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.get_clan_header_map",
        lambda: {
            "open_spots": 31,
            "inactives": 32,
            "reservation_count": 33,
            "reservation_summary": 34,
            "manual_open_spots": 4,
            "manual_open_spots_seen": 35,
        },
    )

    async def _fake_preflight(_tag, *, delta=0):
        return None

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.preflight_clan_availability_update",
        _fake_preflight,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.rt.send_log_message", fake_send_log
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.get_admin_role_ids",
        lambda: {111, 222},
    )

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CM", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        context = TicketContext(
            thread_id=1,
            ticket_number="W0990",
            username="Tester",
            recruit_id=444,
            recruit_display="Tester",
        )
        context.state = "awaiting_clan"
        thread = _DummyThread()
        await watcher._finalize_clan_tag(
            thread,
            context,
            "C1CM",
            actor=None,
            source="message",
            prompt_message=None,
            view=None,
        )
        await bot.close()

    asyncio.run(runner())

    assert log_messages, "failure should produce clan math log"
    message = log_messages[-1]
    assert "result=error" in message
    assert "reason=partial_actions" in message
    assert "decision_result=failed_open_delta" in message
    assert "<@&111>" in message and "<@&222>" in message
    assert "open_spots: 4 → 4" in message


def test_finalize_manual_path_logs_source(monkeypatch) -> None:
    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_upsert(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return "updated"

    reservation = _make_reservation("C1CE")

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return [reservation]

    async def fake_update(row_number: int, status: str):
        assert row_number == reservation.row_number
        assert status == "closed_same_clan"

    base_row = [""] * 35
    base_row[2] = "C1CE"
    base_row[4] = "2"
    base_row[31] = "2"
    base_row[32] = "0"
    base_row[33] = "1"
    base_row[34] = "1 -> Test"
    clan_rows: dict[str, dict[str, object]] = {
        "C1CE": {"row_number": 9, "values": list(base_row)}
    }

    def _normalize(tag: str) -> str:
        return "".join(ch for ch in tag.upper() if ch.isalnum())

    def fake_find_clan_row(tag: str, *, force=False):  # type: ignore[no-untyped-def]
        entry = clan_rows.get(_normalize(tag))
        if not entry:
            return None
        return entry["row_number"], list(entry["values"])

    adjustments: list[tuple[str, int]] = []

    async def fake_adjust(tag: str, delta: int):
        adjustments.append((tag, delta))

    async def fake_recompute(tag: str, guild=None):  # type: ignore[no-untyped-def]
        entry = clan_rows[_normalize(tag)]
        values = entry["values"]  # type: ignore[assignment]
        values[31] = values[31]
        values[33] = "1"

    log_messages: list[str] = []

    async def fake_send_log(message: str) -> None:
        log_messages.append(message)

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        fake_upsert,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit",
        fake_find_reservations,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.update_reservation_status",
        fake_update,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots",
        fake_adjust,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.recompute_clan_availability",
        fake_recompute,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row",
        fake_find_clan_row,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.get_clan_header_map",
        lambda: {
            "open_spots": 31,
            "inactives": 32,
            "reservation_count": 33,
            "reservation_summary": 34,
            "manual_open_spots": 4,
            "manual_open_spots_seen": 35,
        },
    )

    async def _fake_preflight(_tag, *, delta=0):
        return None

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.preflight_clan_availability_update",
        _fake_preflight,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.rt.send_log_message", fake_send_log
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.get_admin_role_ids", lambda: set()
    )

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CE", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        context = TicketContext(
            thread_id=1,
            ticket_number="W0666",
            username="Tester",
            recruit_id=555,
            recruit_display="Tester",
        )
        context.state = "awaiting_clan"
        context.close_source = "manual_fallback"
        thread = _DummyThread()
        await watcher._finalize_clan_tag(
            thread,
            context,
            "C1CE",
            actor=None,
            source="message",
            prompt_message=None,
            view=None,
        )
        await bot.close()

    asyncio.run(runner())

    assert log_messages, "manual path should log clan math"
    message = log_messages[-1]
    assert "source=manual_fallback" in message
    assert f"reservation=row{reservation.row_number}(same)" in message
    assert "result=ok" in message
    assert "decision_result=skipped_open_delta" in message
    assert "skip_reason=reservation_consumed_or_matched" in message
    assert "- C1CE row 9" in message
    assert "open_spots: 2 → 2" in message
    assert adjustments == []
    assert "<@&" not in message


def test_finalize_non_real_tag_logs_skip_reason(monkeypatch) -> None:
    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        lambda *_args, **_kwargs: "updated",
    )

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return []

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit",
        fake_find_reservations,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row",
        lambda _tag, *, force=False: None,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.get_clan_header_map",
        lambda: {
            "open_spots": 31,
            "inactives": 32,
            "reservation_count": 33,
            "reservation_summary": 34,
            "manual_open_spots": 4,
            "manual_open_spots_seen": 35,
        },
    )

    async def _fake_preflight(_tag, *, delta=0):
        return None

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.preflight_clan_availability_update",
        _fake_preflight,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.get_admin_role_ids", lambda: set()
    )

    log_messages: list[str] = []

    async def fake_send_log(message: str) -> None:
        log_messages.append(message)

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.rt.send_log_message", fake_send_log
    )

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CZ", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        context = TicketContext(
            thread_id=1,
            ticket_number="W7777",
            username="Tester",
            recruit_id=12,
            recruit_display="Tester",
        )
        context.state = "awaiting_clan"
        await watcher._finalize_clan_tag(
            _DummyThread(),
            context,
            "C1CZ",
            actor=None,
            source="select",
            prompt_message=None,
            view=None,
        )
        await bot.close()

    asyncio.run(runner())
    assert log_messages
    message = log_messages[-1]
    assert "decision_result=skipped_open_delta" in message
    assert "skip_reason=non_real_final_tag" in message
    assert "open_spots" not in message or "open_spots: " not in message


def test_manual_close_missing_row_prompts(monkeypatch, caplog) -> None:
    inserted_rows: list[list[str]] = []

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_find_row(ticket: str):  # type: ignore[no-untyped-def]
        return None

    def fake_upsert(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        inserted_rows.append(list(_args[:4]))
        return "inserted"

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row",
        fake_find_row,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        fake_upsert,
    )

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CE", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        context = TicketContext(thread_id=1, ticket_number="W0500", username="Tester")
        thread = _DummyThread()
        caplog.set_level(logging.WARNING, logger="c1c.onboarding.welcome_watcher")
        await watcher._handle_manual_close(
            thread,
            context,
            reason="manual_close_without_ticket_tool",
        )
        await bot.close()

        assert context.state == "awaiting_clan"
        assert context.row_created_during_close is True
        assert thread.messages and "Which clan tag" in thread.messages[0]

    asyncio.run(runner())

    assert inserted_rows and inserted_rows[0][:2] == ["W0500", "Tester"]
    assert any(
        "onboarding_row_missing_manual_close" in record.getMessage()
        for record in caplog.records
    )


def test_manual_close_existing_clan_skips_prompt(monkeypatch) -> None:
    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_find_row(ticket: str):  # type: ignore[no-untyped-def]
        return 5, [ticket, "Tester", "C1CE", "2025-01-01 00:00:00"]

    finalized_rows: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fail_upsert(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        finalized_rows.append((_args, _kwargs))
        return "updated"

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row",
        fake_find_row,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        fail_upsert,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row",
        lambda tag, *, force=False: (10, ["", "", tag, "", "4"] + [""] * 30) if tag == "C1CE" else None,
    )

    async def preflight_ok(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.preflight_clan_availability_update",
        preflight_ok,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.recompute_clan_availability",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=[]),
    )

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CE", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        context = TicketContext(thread_id=1, ticket_number="W0501", username="Tester")
        thread = _DummyThread()
        await watcher._handle_manual_close(
            thread,
            context,
            reason="manual_close_without_ticket_tool",
        )
        await bot.close()

        assert context.state == "closed"
        assert not thread.messages
        assert finalized_rows

    asyncio.run(runner())


class _RenameThread:
    def __init__(self, name: str) -> None:
        self.name = name
        self.id = 123
        self.renames: list[str] = []

    async def edit(self, *, name: str) -> None:
        self.name = name
        self.renames.append(name)


def test_rename_thread_to_reserved_success() -> None:
    thread = _RenameThread("W0999-Tester")

    async def runner() -> None:
        await rename_thread_to_reserved(thread, "C1CE")

    asyncio.run(runner())

    assert thread.name == "Res-W0999-Tester-C1CE"
    assert thread.renames == ["Res-W0999-Tester-C1CE"]


def test_rename_thread_to_reserved_promo_ticket_success() -> None:
    thread = _RenameThread("M0351-Debido")

    async def runner() -> None:
        await rename_thread_to_reserved(thread, "C1CK")

    asyncio.run(runner())

    assert thread.name == "Res-M0351-Debido-C1CK"
    assert thread.renames == ["Res-M0351-Debido-C1CK"]


def test_rename_thread_to_reserved_preserves_discord_accepted_username() -> None:
    thread = _RenameThread("M0363-[C1C] SoulAnon")

    async def runner() -> None:
        await rename_thread_to_reserved(thread, "C1CM")

    asyncio.run(runner())

    assert thread.name == "Res-M0363-[C1C] SoulAnon-C1CM"
    assert thread.renames == ["Res-M0363-[C1C] SoulAnon-C1CM"]


def test_rename_thread_to_reserved_truncates_only_when_discord_limit_requires(
    monkeypatch, caplog
) -> None:
    username = "[C1C] " + "SoulAnon" * 20
    original_target_name = f"Res-M0363-{username}-C1CM"
    thread = _RenameThread(f"M0363-{username}")
    human_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.human_log",
        SimpleNamespace(
            human=lambda level, message: human_calls.append((level, message))
        ),
    )
    caplog.set_level(logging.WARNING, logger="c1c.onboarding.welcome_watcher")

    async def runner() -> None:
        await rename_thread_to_reserved(thread, "C1CM")

    asyncio.run(runner())

    assert len(thread.name) == 100
    assert thread.name.startswith("Res-M0363-[C1C] SoulAnon")
    assert thread.name.endswith("-C1CM")
    assert " " in thread.name
    assert "[C1C]" in thread.name
    assert any(
        record.getMessage() == "reservation_thread_name_truncated"
        and getattr(record, "original_target_name", None) == original_target_name
        and getattr(record, "truncated_target_name", None) == thread.name
        for record in caplog.records
    )
    assert human_calls
    assert human_calls[0][0] == "warning"
    assert "reservation_thread_name_truncated" in human_calls[0][1]

def test_rename_thread_to_reserved_updates_existing_reserved_clan() -> None:
    thread = _RenameThread("Res-M0351-Debido-C1CD")

    async def runner() -> None:
        await rename_thread_to_reserved(thread, "C1CK")

    asyncio.run(runner())

    assert thread.name == "Res-M0351-Debido-C1CK"
    assert thread.renames == ["Res-M0351-Debido-C1CK"]


def test_rename_thread_to_reserved_unparsed_logs_error(monkeypatch, caplog) -> None:
    thread = _RenameThread("W554-cail")
    human_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.human_log",
        SimpleNamespace(
            human=lambda level, message: human_calls.append((level, message))
        ),
    )

    caplog.set_level(logging.ERROR, logger="c1c.onboarding.welcome_watcher")

    async def runner() -> None:
        await rename_thread_to_reserved(thread, "C1CE")

    asyncio.run(runner())

    assert not thread.renames
    messages = [
        record.getMessage()
        for record in caplog.records
        if record.name == "c1c.onboarding.welcome_watcher"
    ]
    assert any("reservation thread rename skipped" in message for message in messages)
    assert any(
        getattr(record, "reason", None) == "parse_failed" for record in caplog.records
    )
    assert any(
        getattr(record, "current_thread_name", None) == "W554-cail"
        for record in caplog.records
    )
    assert any(getattr(record, "clan_tag", None) == "C1CE" for record in caplog.records)
    assert human_calls
    level, message = human_calls[0]
    assert level == "error"
    assert "reservation_thread_rename" in message
    assert "thread=W554-cail" in message
    assert "reason=parse_failed" in message


def test_finalize_preflight_failure_applies_no_discord_actions(monkeypatch) -> None:
    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)
    welcome_writes: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_append_welcome_ticket_row(*args, **kwargs):  # type: ignore[no-untyped-def]
        welcome_writes.append((args, kwargs))
        return "updated"

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        fake_append_welcome_ticket_row,
    )

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return []

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit",
        fake_find_reservations,
    )

    row = [""] * 37
    row[2] = "C1CE"
    row[4] = "not a number"
    row[31] = "3"
    row[35] = "3"

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.find_clan_row",
        lambda tag, *, force=False: (12, list(row)) if tag == "C1CE" else None,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.get_clan_header_map",
        lambda: {
            "open_spots": 31,
            "inactives": 32,
            "reservation_count": 33,
            "reservation_summary": 34,
            "manual_open_spots": 4,
            "manual_open_spots_seen": 35,
        },
    )

    async def failing_preflight(tag: str, *, delta=0):
        raise ValueError("non_numeric_manual_open_spots_value")

    adjust_calls: list[tuple[str, int]] = []
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.preflight_clan_availability_update",
        failing_preflight,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.adjust_manual_open_spots",
        lambda tag, delta: adjust_calls.append((tag, delta)),
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.availability.recompute_clan_availability",
        lambda *_args, **_kwargs: None,
    )

    log_messages: list[str] = []

    async def fake_send_log(message: str) -> None:
        log_messages.append(message)

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.rt.send_log_message", fake_send_log
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.get_admin_role_ids", lambda: set()
    )

    async def runner() -> tuple[_DummyThread, TicketContext]:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        watcher = WelcomeTicketWatcher(bot)
        watcher._clan_tags = ["C1CE", _NO_PLACEMENT_TAG]
        watcher._clan_tag_set = set(watcher._clan_tags)
        context = TicketContext(
            thread_id=1,
            ticket_number="W0777",
            username="Tester",
            recruit_id=777,
            recruit_display="Tester",
        )
        context.state = "awaiting_clan"
        thread = _DummyThread()
        await watcher._finalize_clan_tag(
            thread,
            context,
            "C1CE",
            actor=None,
            source="select",
            prompt_message=None,
            view=None,
        )
        await bot.close()
        return thread, context

    thread, context = asyncio.run(runner())

    assert context.state == "awaiting_clan"
    assert not getattr(thread, "edited_names", [])
    assert welcome_writes == []
    assert adjust_calls == []
    assert log_messages
    assert "result=error" in log_messages[-1]
    assert "reason=open_delta_preflight_failed" in log_messages[-1]
    assert "action_state=no_discord_member_action_applied" in log_messages[-1]


def test_promo_move_decision_releases_source_and_consumes_destination() -> None:
    decision = _determine_reservation_decision(
        "F-IT",
        None,
        no_placement_tag=_NO_PLACEMENT_TAG,
        final_is_real=True,
        previous_final="C1CV",
        previous_is_real=True,
    )
    assert decision.open_deltas == {"C1CV": 1, "F-IT": -1}


def test_promo_move_decision_same_source_destination_noop() -> None:
    decision = _determine_reservation_decision(
        "F-IT",
        None,
        no_placement_tag=_NO_PLACEMENT_TAG,
        final_is_real=True,
        consume_open_spot=False,
        previous_final="F-IT",
        previous_is_real=True,
    )
    assert decision.open_deltas == {}


def test_promo_move_destination_reservation_still_releases_source() -> None:
    row = _make_reservation("F-IT")
    decision = _determine_reservation_decision(
        "F-IT",
        row,
        no_placement_tag=_NO_PLACEMENT_TAG,
        final_is_real=True,
        previous_final="C1CV",
        previous_is_real=True,
    )
    assert decision.label == "same"
    assert decision.open_deltas == {"C1CV": 1}


def test_promo_move_missing_source_skips_open_spot_math() -> None:
    applied = []

    async def run() -> None:
        result = await cleanup_reservation_for_ticket_close(
            scope="promo",
            ticket="M0352",
            user="Lucifer",
            user_id=None,
            final_tag="F-IT",
            previous_final="",
            require_source_for_open_spot_math=True,
            ensure_fresh_fn=lambda **_kwargs: asyncio.sleep(0, result=True),
            find_active_reservations_fn=lambda *_args, **_kwargs: asyncio.sleep(0, result=[]),
            find_clan_row_fn=lambda *_args, **_kwargs: (10, []),
            adjust_manual_open_spots_fn=lambda tag, delta: applied.append((tag, delta)) or asyncio.sleep(0, result=0),
            recompute_clan_availability_fn=lambda *_args, **_kwargs: asyncio.sleep(0, result=None),
        )
        assert result.skipped is True
        assert result.reason == "source_clan_missing"
        assert "skip_reason=source_clan_missing" in result.decision_line

    asyncio.run(run())
    assert applied == []


def test_promo_source_lookup_failure_warns_and_records_debug(caplog) -> None:
    applied = []

    async def run() -> None:
        caplog.set_level(logging.INFO)
        result = await cleanup_reservation_for_ticket_close(
            scope="promo",
            ticket="M0361",
            user="Caillean",
            user_id=None,
            final_tag="C1CD",
            previous_final="C1CB",
            require_source_for_open_spot_math=True,
            ensure_fresh_fn=lambda **_kwargs: asyncio.sleep(0, result=True),
            find_active_reservations_fn=lambda *_args, **_kwargs: asyncio.sleep(0, result=[]),
            find_clan_row_fn=lambda tag, **_kwargs: (20, []) if tag == "C1CD" else None,
            adjust_manual_open_spots_fn=lambda tag, delta: applied.append((tag, delta)) or asyncio.sleep(0, result=0),
            recompute_clan_availability_fn=lambda *_args, **_kwargs: asyncio.sleep(0, result=None),
        )
        assert result.source_clan_lookup_key == "C1CB"
        assert result.source_clan_row_found is False
        assert result.previous_is_real is False
        assert result.source_clan_not_real_reason == "source_clan_row_not_found"

    asyncio.run(run())
    assert applied == [("C1CD", -1)]
    assert "promo_source_clan_lookup_failed" in caplog.text
    assert "source_clan_tag=C1CB" in caplog.text
    assert "source_clan_lookup_key=C1CB" in caplog.text
    assert "source_clan_row_found=False" in caplog.text
    assert "previous_is_real=False" in caplog.text


def test_promo_source_none_only_consumes_destination() -> None:
    applied = []

    async def run() -> None:
        result = await cleanup_reservation_for_ticket_close(
            scope="promo",
            ticket="M0361",
            user="Caillean",
            user_id=None,
            final_tag="C1CD",
            previous_final=_NO_PLACEMENT_TAG,
            require_source_for_open_spot_math=True,
            ensure_fresh_fn=lambda **_kwargs: asyncio.sleep(0, result=True),
            find_active_reservations_fn=lambda *_args, **_kwargs: asyncio.sleep(0, result=[]),
            find_clan_row_fn=lambda tag, **_kwargs: (20, []) if tag == "C1CD" else None,
            adjust_manual_open_spots_fn=lambda tag, delta: applied.append((tag, delta)) or asyncio.sleep(0, result=0),
            recompute_clan_availability_fn=lambda *_args, **_kwargs: asyncio.sleep(0, result=None),
        )
        assert result.requested_open_deltas == {"C1CD": -1}
        assert result.source_clan_not_real_reason == "source_clan_none"

    asyncio.run(run())
    assert applied == [("C1CD", -1)]


def test_promo_same_source_destination_normalized_no_delta() -> None:
    decision = _determine_reservation_decision(
        "C1-CD",
        None,
        no_placement_tag=_NO_PLACEMENT_TAG,
        final_is_real=True,
        consume_open_spot=False,
        previous_final="C1CD",
        previous_is_real=True,
    )
    assert decision.open_deltas == {}


def test_clan_math_column_indices_uses_fallbacks_for_missing_visibility_columns(monkeypatch) -> None:
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.recruitment_sheets.get_clan_header_map",
        lambda: {"open_spots": 4},
    )
    assert _clan_math_column_indices() == {
        "open_spots": 4,
        "AF": 4,
        "AG": 32,
        "AH": 33,
        "AI": 34,
    }
