import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

import pytest

from modules.onboarding import sessions
from modules.onboarding.controllers.welcome_controller import WelcomeController
from modules.onboarding.sessions import ensure_session_for_thread
from modules.onboarding.watcher_promo import PromoTicketWatcher
from modules.onboarding.watcher_welcome import (
    WelcomeTicketWatcher,
    TicketContext,
    _process_incomplete_thread,
    _process_promo_thread,
)


class _DummyUser:
    def __init__(self, user_id: int):
        self.id = user_id
        self.bot = False


class _DummyMessage:
    def __init__(self, user_id: int, mentions: bool | list[_DummyUser] = True):
        self.id = 555
        self.author = _DummyUser(user_id)
        self.content = ""
        if mentions is False:
            self.mentions: list[_DummyUser] = []
        elif isinstance(mentions, list):
            self.mentions = mentions
        else:
            self.mentions = [_DummyUser(user_id)]


class _DummyThread:
    def __init__(self, thread_id: int, name: str, created_at: datetime):
        self.id = thread_id
        self.name = name
        self.created_at = created_at
        self.sent: list[_DummyMessage] = []
        self.archived = False
        self.locked = False

    async def send(self, content: str | None = None, **kwargs):
        message = _DummyMessage(0)
        message.content = content or ""
        message.created_at = datetime.now(timezone.utc)
        self.sent.append(message)
        return message

    async def edit(self, **kwargs):
        if "name" in kwargs and kwargs["name"] is not None:
            self.name = kwargs["name"]
        if "archived" in kwargs and kwargs["archived"] is not None:
            self.archived = kwargs["archived"]
        if "locked" in kwargs and kwargs["locked"] is not None:
            self.locked = kwargs["locked"]


class _DummyBot:
    def get_cog(self, _name: str):  # pragma: no cover - simple stub
        return None


@pytest.fixture()
def memory_sheet(monkeypatch):
    rows: dict[tuple[int, int], dict] = {}
    onboarding_rows: dict[str, dict] = {}

    def fake_load(user_id: int, thread_id: int):
        key = (int(user_id), int(thread_id))
        return rows.get(key)

    def fake_save(payload: dict, allow_create: bool = True):
        try:
            key = (int(payload.get("user_id") or 0), int(payload.get("thread_id") or 0))
        except Exception:
            key = (payload.get("user_id"), payload.get("thread_id"))
        rows[key] = payload
        return True

    fake_sheet_module = type("_FakeSheet", (), {"load": staticmethod(fake_load), "save": staticmethod(fake_save)})
    monkeypatch.setattr(sessions, "sess_sheet", fake_sheet_module)
    monkeypatch.setattr(
        "shared.sheets.onboarding_sessions.upsert_session",
        lambda **payload: onboarding_rows.setdefault(str(payload.get("thread_id", "")), payload),
    )
    monkeypatch.setattr("shared.sheets.onboarding_sessions.load", lambda *_: None)
    monkeypatch.setattr("shared.sheets.onboarding_sessions.load_all", lambda: list(onboarding_rows.values()))
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_onboarding_session_row",
        lambda **_: "inserted",
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_promo.onboarding_sheets.append_onboarding_session_row",
        lambda **_: "inserted",
    )
    return rows


@pytest.fixture(autouse=True)
def _clear_target_cache(monkeypatch):
    from modules.onboarding import watcher_welcome

    watcher_welcome._TARGET_CACHE.clear()


def _install_message_fixtures(
    monkeypatch, module, user_id: int = 42, message: _DummyMessage | None = None
):
    dummy_message = message or _DummyMessage(user_id)

    async def _locate(_thread):
        return dummy_message

    monkeypatch.setattr(module, "locate_welcome_message", _locate)


def _extract_target(message):
    target = message.mentions[0] if getattr(message, "mentions", None) else getattr(message, "author", None)
    return (target.id if target else None, getattr(message, "id", None))


def test_welcome_thread_open_creates_session(memory_sheet, monkeypatch):
    created_at = datetime(2025, 1, 2, 12, 0, tzinfo=timezone.utc)
    thread = _DummyThread(101, "W1234-user", created_at)
    context = TicketContext(thread_id=thread.id, ticket_number="W1234", username="user")

    _install_message_fixtures(monkeypatch, __import__("modules.onboarding.watcher_welcome", fromlist=["locate_welcome_message"]))
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.extract_target_from_message",
        _extract_target,
    )
    monkeypatch.setattr(
        "shared.sheets.onboarding.upsert_welcome",
        lambda row, headers: "inserted",
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        lambda *_, **__: "inserted",
    )
    monkeypatch.setattr(
        "shared.sheets.onboarding.find_welcome_row",
        lambda ticket: None,
    )
    monkeypatch.setattr(
        "modules.common.feature_flags.is_enabled",
        lambda flag: True,
    )
    monkeypatch.setattr("modules.onboarding.watcher_welcome.get_welcome_channel_id", lambda: 123)
    monkeypatch.setattr("modules.onboarding.watcher_welcome.get_ticket_tool_bot_id", lambda: None)

    asyncio.run(ensure_session_for_thread(99, thread.id, updated_at=created_at))
    watcher = WelcomeTicketWatcher(bot=_DummyBot())
    asyncio.run(watcher._handle_ticket_open(thread, context))

    assert (42, 101) in memory_sheet
    payload = memory_sheet[(42, 101)]
    assert payload.get("completed") is False
    assert payload.get("answers") == {}
    assert payload.get("updated_at") == created_at.isoformat()


def test_welcome_ticket_open_falls_back_to_author_when_no_mentions(memory_sheet, monkeypatch):
    created_at = datetime(2025, 2, 2, 12, 0, tzinfo=timezone.utc)
    thread = _DummyThread(505, "W5678-user", created_at)
    thread.parent_id = 123
    context = TicketContext(thread_id=thread.id, ticket_number="W5678", username="user")

    dummy_message = _DummyMessage(99, mentions=False)
    _install_message_fixtures(
        monkeypatch,
        __import__("modules.onboarding.watcher_welcome", fromlist=["locate_welcome_message"]),
        message=dummy_message,
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.extract_target_from_message",
        _extract_target,
    )
    monkeypatch.setattr(
        "shared.sheets.onboarding.upsert_welcome",
        lambda row, headers: "inserted",
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.onboarding_sheets.append_welcome_ticket_row",
        lambda *_, **__: "inserted",
    )
    monkeypatch.setattr(
        "shared.sheets.onboarding.find_welcome_row",
        lambda ticket: None,
    )
    monkeypatch.setattr(
        "modules.common.feature_flags.is_enabled",
        lambda flag: True,
    )
    monkeypatch.setattr("modules.onboarding.watcher_welcome.get_welcome_channel_id", lambda: 123)
    monkeypatch.setattr("modules.onboarding.watcher_welcome.get_ticket_tool_bot_id", lambda: None)

    asyncio.run(ensure_session_for_thread(99, thread.id, updated_at=created_at))
    watcher = WelcomeTicketWatcher(bot=_DummyBot())
    asyncio.run(watcher._handle_ticket_open(thread, context))

    assert (99, 505) in memory_sheet
    payload = memory_sheet[(99, 505)]
    assert payload.get("completed") is False
    assert payload.get("answers") == {}
    assert payload.get("updated_at") == created_at.isoformat()


def test_promo_thread_open_creates_session(memory_sheet, monkeypatch):
    created_at = datetime(2025, 1, 3, 9, 30, tzinfo=timezone.utc)
    thread = _DummyThread(202, "R1234-player", created_at)

    _install_message_fixtures(monkeypatch, __import__("modules.onboarding.watcher_promo", fromlist=["locate_welcome_message"]))
    monkeypatch.setattr(
        "modules.onboarding.watcher_promo.extract_target_from_message",
        _extract_target,
    )
    monkeypatch.setattr(
        "shared.sheets.onboarding.upsert_promo",
        lambda row, headers: "inserted",
    )
    monkeypatch.setattr(
        "modules.onboarding.watcher_promo.onboarding_sheets.append_promo_ticket_row",
        lambda *_, **__: "inserted",
    )
    monkeypatch.setattr(
        "shared.sheets.onboarding.find_promo_row",
        lambda ticket: None,
    )
    monkeypatch.setattr(
        "modules.common.feature_flags.is_enabled",
        lambda flag: True,
    )

    watcher = PromoTicketWatcher(bot=_DummyBot())
    asyncio.run(watcher.on_thread_create(thread))

    # No onboarding session row should be written until the panel appears.
    assert memory_sheet == {}

    context = watcher._tickets[thread.id]
    context.user_id = 42

    monkeypatch.setattr(
        "shared.sheets.onboarding.find_promo_row",
        lambda ticket: (0, {}),
    )
    monkeypatch.setattr(watcher, "_load_clan_tags", AsyncMock(return_value=["C1CE", "C1CW"]))

    asyncio.run(watcher._begin_clan_prompt(thread, context))

    message = thread.sent[-1]
    assert (42, 202) in memory_sheet
    payload = memory_sheet[(42, 202)]
    assert payload.get("completed") is False
    assert payload.get("panel_message_id") == 555
    assert payload.get("updated_at") == message.created_at.isoformat()


def test_panel_start_updates_existing_session(memory_sheet, monkeypatch):
    thread_id = 303
    user_id = 77
    created_at = datetime(2025, 1, 4, 8, 0, tzinfo=timezone.utc)
    panel_time = created_at + timedelta(minutes=10)

    asyncio.run(ensure_session_for_thread(user_id, thread_id, updated_at=created_at))

    controller = WelcomeController(bot=_DummyBot())
    monkeypatch.setattr(controller, "_resolve_applicant_id", lambda *_: user_id)

    asyncio.run(
        controller._persist_session_start(
            thread_id,
            panel_message_id=999,
            session_data=None,
            panel_created_at=panel_time,
        )
    )

    payload = memory_sheet[(user_id, thread_id)]
    assert payload.get("panel_message_id") == 999
    assert payload.get("updated_at") == panel_time.isoformat()
    assert payload.get("step_index") == 0


def test_inactivity_scan_creates_and_updates_single_session_row(memory_sheet, monkeypatch):
    created_at = datetime(2025, 1, 5, 10, 0, tzinfo=timezone.utc)
    thread = _DummyThread(404, "W1234-empty", created_at)

    _install_message_fixtures(monkeypatch, __import__("modules.onboarding.watcher_welcome", fromlist=["locate_welcome_message"]))
    monkeypatch.setattr(
        "modules.onboarding.watcher_welcome.extract_target_from_message",
        _extract_target,
    )

    reminder_time = created_at + timedelta(hours=3, minutes=1)
    warning_time = created_at + timedelta(hours=24, minutes=5)
    close_time = created_at + timedelta(hours=36, minutes=5)

    asyncio.run(_process_incomplete_thread(bot=_DummyBot(), thread=thread, now=reminder_time))
    asyncio.run(_process_incomplete_thread(bot=_DummyBot(), thread=thread, now=warning_time))
    asyncio.run(_process_incomplete_thread(bot=_DummyBot(), thread=thread, now=close_time))

    assert len(memory_sheet) == 1
    assert (42, 404) in memory_sheet
    payload = memory_sheet[(42, 404)]
    assert payload.get("user_id") in {"42", 42}
    assert payload.get("completed") is False
    assert payload.get("first_reminder_at") is not None
    assert payload.get("warning_sent_at") is not None
    assert payload.get("auto_closed_at") is not None


def test_promo_close_reminder_pings_recruitment(memory_sheet, monkeypatch):
    created_at = datetime(2025, 3, 3, 15, 0, tzinfo=timezone.utc)
    thread = _DummyThread(606, "R1234-user", created_at)

    async def _resolve_target(*_args, **_kwargs):
        return 42, "user"

    monkeypatch.setattr("modules.onboarding.watcher_welcome._resolve_target_user", _resolve_target)
    monkeypatch.setattr("modules.onboarding.watcher_welcome.get_recruiter_role_ids", lambda: [999])
    monkeypatch.setattr("modules.onboarding.watcher_welcome.get_recruitment_coordinator_role_ids", lambda: [])
    monkeypatch.setattr("modules.onboarding.watcher_welcome.get_guardian_knight_role_ids", lambda: [])
    monkeypatch.setattr("modules.common.feature_flags.is_enabled", lambda *_: True)
    monkeypatch.setattr("modules.onboarding.watcher_promo.onboarding_sheets.upsert_promo", lambda *_, **__: "updated")

    asyncio.run(ensure_session_for_thread(42, thread.id, updated_at=created_at, thread_name=thread.name))

    reminder_time = created_at + timedelta(hours=3, minutes=5)
    warning_time = created_at + timedelta(hours=24, minutes=10)
    close_time = created_at + timedelta(hours=36, minutes=5)

    asyncio.run(_process_promo_thread(bot=_DummyBot(), thread=thread, now=reminder_time))
    asyncio.run(_process_promo_thread(bot=_DummyBot(), thread=thread, now=warning_time))
    asyncio.run(_process_promo_thread(bot=_DummyBot(), thread=thread, now=close_time))

    assert thread.sent, "close reminder should send a message"
    assert any("<@&999>" in msg.content for msg in thread.sent)


def test_auto_closed_thread_skips_manual_prompt(monkeypatch):
    monkeypatch.setattr("modules.common.feature_flags.is_enabled", lambda *_: True)
    monkeypatch.setattr("shared.sheets.onboarding.find_welcome_row", lambda *_: None)
    monkeypatch.setattr("modules.onboarding.watcher_welcome.onboarding_sheets.find_welcome_row", lambda *_: None)

    watcher = WelcomeTicketWatcher(bot=_DummyBot())
    monkeypatch.setattr(watcher, "_is_ticket_thread", lambda *_: True)
    manual_called = False

    async def _fail_manual(*_args, **_kwargs):
        nonlocal manual_called
        manual_called = True

    watcher._handle_manual_close = _fail_manual  # type: ignore[assignment]

    before = _DummyThread(808, "W1234-user", datetime.now(timezone.utc))
    after = _DummyThread(808, "closed-W1234-user-NONE", datetime.now(timezone.utc))
    after.archived = True
    watcher._auto_closed_threads.add(after.id)
    watcher._tickets[after.id] = TicketContext(thread_id=after.id, ticket_number="W1234", username="user")

    asyncio.run(watcher.on_thread_update(before, after))

    assert manual_called is False


def test_welcome_archive_close_posts_prompt_without_availability_preflight(monkeypatch):
    monkeypatch.setattr("modules.common.feature_flags.is_enabled", lambda *_: True)
    from modules.onboarding import watcher_welcome

    preflight_calls = []
    adjust_calls = []
    recompute_calls = []

    async def _preflight(*args, **kwargs):
        preflight_calls.append((args, kwargs))

    async def _adjust(*args, **kwargs):
        adjust_calls.append((args, kwargs))

    async def _recompute(*args, **kwargs):
        recompute_calls.append((args, kwargs))

    monkeypatch.setattr(watcher_welcome.availability, "preflight_clan_availability_update", _preflight)
    monkeypatch.setattr(watcher_welcome.availability, "adjust_manual_open_spots", _adjust)
    monkeypatch.setattr(watcher_welcome.availability, "recompute_clan_availability", _recompute)
    monkeypatch.setattr(watcher_welcome, "_send_placement_log_line", lambda **_: asyncio.sleep(0))
    monkeypatch.setattr(watcher_welcome.onboarding_sessions, "get_by_thread_id", lambda *_: None)
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "find_welcome_row", lambda *_: None)
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "update_ticket_finalization_state", lambda *_, **__: "updated")

    watcher = WelcomeTicketWatcher(bot=_DummyBot())
    monkeypatch.setattr(watcher, "_is_ticket_thread", lambda *_: True)
    monkeypatch.setattr(watcher, "_load_clan_tags", lambda: asyncio.sleep(0, result=["C1CD", "NONE"]))

    before = _DummyThread(9701, "W0971-Player", datetime.now(timezone.utc))
    after = _DummyThread(9701, "W0971-Player", datetime.now(timezone.utc))
    after.archived = True
    watcher._tickets[after.id] = TicketContext(thread_id=after.id, ticket_number="W0971", username="Player")

    asyncio.run(watcher.on_thread_update(before, after))

    assert after.sent, "archive close should post the clan selection prompt"
    assert "Which clan tag for Player" in after.sent[0].content
    assert preflight_calls == []
    assert adjust_calls == []
    assert recompute_calls == []


def test_welcome_manual_close_existing_sheet_clan_still_prompts_before_availability(monkeypatch):
    monkeypatch.setattr("modules.common.feature_flags.is_enabled", lambda *_: True)
    from modules.onboarding import watcher_welcome

    finalized = []
    prompted = []
    monkeypatch.setattr(watcher_welcome.onboarding_sessions, "get_by_thread_id", lambda *_: None)
    monkeypatch.setattr(
        watcher_welcome.onboarding_sheets,
        "find_welcome_row",
        lambda *_: (2, ["W0971", "Player", "C1CD", ""]),
    )

    watcher = WelcomeTicketWatcher(bot=_DummyBot())
    monkeypatch.setattr(watcher, "_is_ticket_thread", lambda *_: True)

    async def _finalize(*args, **kwargs):
        finalized.append((args, kwargs))

    async def _prompt(thread, context, manual=False):
        prompted.append((thread, context, manual))

    monkeypatch.setattr(watcher, "_finalize_clan_tag", _finalize)
    monkeypatch.setattr(watcher, "_handle_ticket_closed", _prompt)

    before = _DummyThread(9702, "W0971-Player", datetime.now(timezone.utc))
    after = _DummyThread(9702, "W0971-Player", datetime.now(timezone.utc))
    after.archived = True
    watcher._tickets[after.id] = TicketContext(thread_id=after.id, ticket_number="W0971", username="Player")

    asyncio.run(watcher.on_thread_update(before, after))

    assert finalized == []
    assert prompted and prompted[0][2] is True


def test_welcome_on_thread_update_logs_close_handler_exception_details(monkeypatch, caplog):
    monkeypatch.setattr("modules.common.feature_flags.is_enabled", lambda *_: True)
    from modules.onboarding import watcher_welcome

    watcher = WelcomeTicketWatcher(bot=_DummyBot())
    monkeypatch.setattr(watcher, "_is_ticket_thread", lambda *_: True)
    monkeypatch.setattr(watcher_welcome.onboarding_sessions, "get_by_thread_id", lambda *_: None)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("prompt exploded")

    monkeypatch.setattr(watcher, "_handle_manual_close", _boom)

    before = _DummyThread(9703, "W0971-Player", datetime.now(timezone.utc))
    after = _DummyThread(9703, "W0971-Player", datetime.now(timezone.utc))
    after.archived = True
    watcher._tickets[after.id] = TicketContext(thread_id=after.id, ticket_number="W0971", username="Player")

    with caplog.at_level("ERROR"):
        asyncio.run(watcher.on_thread_update(before, after))

    records = [r for r in caplog.records if r.message == "welcome on_thread_update close handler failed"]
    assert records
    assert records[0].phase == "manual_close_prompt"
    assert records[0].ticket == "W0971"
    assert records[0].thread_id == 9703
    assert records[0].thread_name == "W0971-Player"
    assert records[0].exc_info[0] is RuntimeError
