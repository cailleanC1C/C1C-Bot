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
    _NO_PLACEMENT_TAG,
    _determine_reservation_decision,
    build_closed_thread_name,
    parse_welcome_thread_name,
    rename_thread_to_reserved,
)
from shared.sheets import reservations as reservations_sheets


@pytest.fixture(autouse=True)
def _stub_find_welcome_row(monkeypatch):
    def _fake_find_welcome_row(_ticket):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row",
        _fake_find_welcome_row,
    )


def _make_reservation(tag: str, *, created: dt.datetime | None = None) -> reservations_sheets.ReservationRow:
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


def test_parse_thread_name_open_without_w_prefix() -> None:
    parts = parse_welcome_thread_name("0867-Caillean")
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


class _DummyMessage:
    def __init__(self, thread: "_DummyThread", message_id: int, content: str) -> None:
        self._thread = thread
        self.id = message_id
        self._content = content

    async def edit(self, *, content: str | None = None, view: object | None = None) -> None:
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


def test_finalize_reconciles_when_row_inserted(monkeypatch) -> None:
    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_upsert(row, headers):  # type: ignore[no-untyped-def]
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

    def fake_find_clan(tag: str):  # type: ignore[no-untyped-def]
        return tag, ["", "", tag]

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.upsert_welcome",
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
        SimpleNamespace(human=lambda level, message: human_logs.append(f"{level}:{message}")),
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

    assert reservation_calls, "should look up reservations even when the row was inserted"
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
    assert recorded.get("sessions")
    session_payload = recorded["sessions"][0]
    assert session_payload["user_id"] == 7777
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
    def fake_upsert(row, headers):  # type: ignore[no-untyped-def]
        return "unknown"

    async def fail_async(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("should not run reconciliation when row is unknown")

    def fail_sync(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("should not read clan rows when row is unknown")

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.upsert_welcome",
        fake_upsert,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.reservations_sheets.find_active_reservations_for_recruit",
        fail_async,
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
        fail_sync,
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

    def fake_upsert(row, headers):  # type: ignore[no-untyped-def]
        return "updated"

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return []

    async def fail_update(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("should not update reservation status when none exist")

    async def fake_adjust(tag: str, delta: int):
        adjustments.append((tag, delta))

    async def fake_recompute(tag: str, guild=None):  # type: ignore[no-untyped-def]
        recomputed.append(tag)

    def fake_find_clan(tag: str):  # type: ignore[no-untyped-def]
        return tag, ["", "", tag]

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.upsert_welcome",
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
        if record.name == "c1c.onboarding.welcome_watcher" and record.levelno == logging.INFO
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

    def fake_upsert(row, headers):  # type: ignore[no-untyped-def]
        return "updated"

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return []

    async def fail_update(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("should not update reservation status when none exist")

    async def fake_adjust(tag: str, delta: int):
        adjustments.append((tag, delta))

    async def fake_recompute(tag: str, guild=None):  # type: ignore[no-untyped-def]
        recomputed.append(tag)

    def fake_find_clan(tag: str):  # type: ignore[no-untyped-def]
        return tag, ["", "", tag]

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.upsert_welcome",
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
        if record.name == "c1c.onboarding.welcome_watcher" and record.levelno == logging.INFO
    ]
    assert (
        "⚠️ welcome_close_manual — ticket=W1456 • user=Tester • final=C1CE "
        "• reservation=none • result=ok • source=manual_fallback"
        in log_messages
    )


def test_finalize_manual_consumes_seat_without_reservation(monkeypatch) -> None:
    adjustments: list[tuple[str, int]] = []
    recomputed: list[str] = []
    rows: list[list[str]] = []

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_upsert(row, headers):  # type: ignore[no-untyped-def]
        rows.append(list(row))
        return "inserted"

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return []

    async def fake_adjust(tag: str, delta: int):
        adjustments.append((tag, delta))

    async def fake_recompute(tag: str, guild=None):  # type: ignore[no-untyped-def]
        recomputed.append(tag)

    def fake_find_clan(tag: str):  # type: ignore[no-untyped-def]
        return tag, ["", "", tag]

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.upsert_welcome",
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

    def fake_upsert(row, headers):  # type: ignore[no-untyped-def]
        return "updated"

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return [_make_reservation("C1CE")]

    async def fake_update(row_number: int, status: str):
        status_updates.append((row_number, status))

    async def fake_adjust(tag: str, delta: int):
        adjustments.append((tag, delta))

    async def fake_recompute(tag: str, guild=None):  # type: ignore[no-untyped-def]
        pass

    def fake_find_clan(tag: str):  # type: ignore[no-untyped-def]
        return tag, ["", "", tag]

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.upsert_welcome",
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

    def fake_upsert(row, headers):  # type: ignore[no-untyped-def]
        return "updated"

    async def fake_find_reservations(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return [_make_reservation("MART")]

    async def fake_update(row_number: int, status: str):
        status_updates.append((row_number, status))

    async def fake_adjust(tag: str, delta: int):
        adjustments.append((tag, delta))

    async def fake_recompute(tag: str, guild=None):  # type: ignore[no-untyped-def]
        pass

    def fake_find_clan(tag: str):  # type: ignore[no-untyped-def]
        return tag, ["", "", tag]

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.upsert_welcome",
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

    def fake_upsert(row, headers):  # type: ignore[no-untyped-def]
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
        "modules.onboarding.watcher_welcome.onboarding_sheets.upsert_welcome",
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

    def fake_upsert(row, headers):  # type: ignore[no-untyped-def]
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
        "modules.onboarding.watcher_welcome.onboarding_sheets.upsert_welcome",
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

    def fake_upsert(row, headers):  # type: ignore[no-untyped-def]
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

    def fake_find_clan_row(tag: str):  # type: ignore[no-untyped-def]
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
        "modules.onboarding.watcher_welcome.onboarding_sheets.upsert_welcome",
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
        lambda: {"open_spots": 4},
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
    assert "- C1CE row 12" in message
    assert "open_spots: 3 → 2" in message
    assert "AF: 3 → 2" in message
    assert "<@&" not in message


def test_finalize_error_pings_admins(monkeypatch) -> None:
    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_upsert(row, headers):  # type: ignore[no-untyped-def]
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

    def fake_find_clan_row(tag: str):  # type: ignore[no-untyped-def]
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
        "modules.onboarding.watcher_welcome.onboarding_sheets.upsert_welcome",
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
        lambda: {"open_spots": 4},
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
    assert "<@&111>" in message and "<@&222>" in message
    assert "open_spots: 4 → 4" in message


def test_finalize_manual_path_logs_source(monkeypatch) -> None:
    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_upsert(row, headers):  # type: ignore[no-untyped-def]
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

    def fake_find_clan_row(tag: str):  # type: ignore[no-untyped-def]
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
        "modules.onboarding.watcher_welcome.onboarding_sheets.upsert_welcome",
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
        lambda: {"open_spots": 4},
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
    assert "- C1CE row 9" in message
    assert "open_spots: 2 → 2" in message
    assert adjustments == []
    assert "<@&" not in message

def test_manual_close_missing_row_prompts(monkeypatch, caplog) -> None:
    inserted_rows: list[list[str]] = []

    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_find_row(ticket: str):  # type: ignore[no-untyped-def]
        return None

    def fake_upsert(row, headers):  # type: ignore[no-untyped-def]
        inserted_rows.append(list(row))
        return "inserted"

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row",
        fake_find_row,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.upsert_welcome",
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
    assert any("onboarding_row_missing_manual_close" in record.getMessage() for record in caplog.records)


def test_manual_close_existing_clan_skips_prompt(monkeypatch) -> None:
    async def fake_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        return func(*args, **kwargs)

    monkeypatch.setattr("asyncio.to_thread", fake_to_thread)

    def fake_find_row(ticket: str):  # type: ignore[no-untyped-def]
        return 5, [ticket, "Tester", "C1CE", "2025-01-01 00:00:00"]

    def fail_upsert(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("should not upsert when row exists with clan")

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row",
        fake_find_row,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.upsert_welcome",
        fail_upsert,
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

        assert context.state == "open"
        assert not thread.messages

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


def test_rename_thread_to_reserved_unparsed_logs_error(monkeypatch, caplog) -> None:
    thread = _RenameThread("W554-cail")
    human_calls: list[tuple[str, str]] = []

    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.human_log",
        SimpleNamespace(human=lambda level, message: human_calls.append((level, message))),
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
    assert any("welcome_reserve_rename_error" in message for message in messages)
    assert human_calls
    level, message = human_calls[0]
    assert level == "error"
    assert "welcome_reserve_rename_error" in message
    assert "thread=W554-cail" in message
