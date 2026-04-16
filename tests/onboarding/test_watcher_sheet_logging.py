import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from modules.onboarding import watcher_promo
from modules.onboarding import watcher_welcome
from modules.onboarding.watcher_promo import PromoTicketWatcher
from modules.onboarding.watcher_welcome import (
    TicketContext,
    WelcomeTicketWatcher,
    WelcomeWatcher,
)


def test_welcome_ticket_logs_sheets(monkeypatch):
    watcher = WelcomeTicketWatcher(bot=MagicMock())
    watcher.bot.user = SimpleNamespace(id=999, bot=True)
    thread = SimpleNamespace(
        id=111,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        name="W0600-smurf",
    )
    context = TicketContext(thread_id=111, ticket_number="W0600", username="smurf")

    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "find_welcome_row", lambda ticket: None)
    starter = SimpleNamespace(
        mentions=[SimpleNamespace(id=222, bot=False)],
        content="<@222>",
        author=SimpleNamespace(id=watcher.bot.user.id, bot=True),
    )
    monkeypatch.setattr(watcher_welcome, "locate_welcome_message", AsyncMock(return_value=starter))
    monkeypatch.setattr(watcher_welcome, "ensure_session_for_thread", AsyncMock())

    welcome_calls = []
    session_calls = []
    session_sheet_rows = []

    def fake_append_welcome(ticket, username, clan_tag, date_closed, **kwargs):
        welcome_calls.append((ticket, username, clan_tag, date_closed, kwargs))
        return "inserted"

    def fake_append_session(**kwargs):
        session_calls.append(kwargs)
        return "inserted"

    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "append_welcome_ticket_row", fake_append_welcome)
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "append_onboarding_session_row", fake_append_session)
    monkeypatch.setattr(
        watcher_welcome.onboarding_sessions,
        "upsert_session",
        lambda **payload: session_sheet_rows.append(payload),
    )

    asyncio.run(watcher._handle_ticket_open(thread, context))

    assert welcome_calls[0][:4] == ("W0600", "smurf", "", "")
    assert welcome_calls[0][4]["user_id"] == 222
    assert welcome_calls[0][4]["thread_id"] == thread.id
    assert welcome_calls[0][4]["created_at"] == thread.created_at
    if session_sheet_rows:
        assert session_sheet_rows[0]["thread_id"] == thread.id
        assert session_sheet_rows[0]["thread_name"] == thread.name


def test_promo_ticket_logs_sheets(monkeypatch):
    watcher = PromoTicketWatcher(bot=MagicMock())
    watcher._features_enabled = lambda: True  # type: ignore[attr-defined]
    watcher._is_ticket_thread = lambda thread: True  # type: ignore[attr-defined]
    thread = SimpleNamespace(id=222, created_at=datetime(2025, 2, 2, tzinfo=timezone.utc), name="M0011-user")
    context = watcher_promo.PromoTicketContext(
        thread_id=222,
        ticket_number="M0011",
        username="caillean",
        promo_type="move",
        thread_created="2025-02-02",
        year="2025",
        month="February",
    )

    monkeypatch.setattr(watcher_promo.onboarding_sheets, "find_promo_row", lambda ticket: None)
    monkeypatch.setattr(watcher_promo, "locate_welcome_message", AsyncMock(return_value=object()))
    monkeypatch.setattr(watcher_promo, "extract_target_from_message", lambda _: (333, None))
    monkeypatch.setattr(watcher_promo, "ensure_session_for_thread", AsyncMock())

    promo_calls = []

    def fake_append_promo(*args, **kwargs):
        promo_calls.append((args, kwargs))
        return "inserted"

    monkeypatch.setattr(watcher_promo.onboarding_sheets, "append_promo_ticket_row", fake_append_promo)
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "append_onboarding_session_row", AsyncMock())

    asyncio.run(watcher.on_thread_create(thread))

    # Closing the ticket should not append another ticket row.
    asyncio.run(watcher._ensure_row_initialized(thread, context))

    args, kwargs = promo_calls[0]
    assert args == (
        "M0011",
        "user",
        "",
        "player move request",
        "2025-02-02 00:00:00",
        "2025",
        "February",
        "",
        "",
        "",
    )
    assert kwargs["user_id"] == 333
    assert kwargs["thread_id"] == thread.id
    assert kwargs["created_at"] == thread.created_at
    assert len(promo_calls) == 1
    # No onboarding session rows are created until the promo panel is posted.
    assert watcher_promo.onboarding_sheets.append_onboarding_session_row.await_count == 0


def test_promo_ticket_open_logs_error_on_failure(monkeypatch, caplog):
    watcher = PromoTicketWatcher(bot=MagicMock())
    thread = SimpleNamespace(id=333, created_at=datetime(2025, 3, 3, tzinfo=timezone.utc))
    context = watcher_promo.PromoTicketContext(
        thread_id=333,
        ticket_number="L0001",
        username="tester",
        promo_type="lead",
        thread_created="2025-03-03",
        year="2025",
        month="March",
    )

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(watcher_promo.onboarding_sheets, "append_promo_ticket_row", boom)

    with caplog.at_level("ERROR"):
        asyncio.run(watcher._log_ticket_open(thread, context))

    assert any("result=error" in str(record.msg) for record in caplog.records)


def test_welcome_ticket_open_logs_sheet_update(monkeypatch, caplog):
    watcher = WelcomeTicketWatcher(bot=MagicMock())
    watcher.bot.user = SimpleNamespace(id=999, bot=True)
    thread = SimpleNamespace(
        id=444,
        created_at=datetime(2025, 4, 4, tzinfo=timezone.utc),
        name="W4444-smurf",
    )
    context = TicketContext(thread_id=444, ticket_number="W4444", username="smurf")

    monkeypatch.setattr(watcher_welcome, "locate_welcome_message", AsyncMock(return_value=None))
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "find_welcome_row", lambda ticket: None)
    monkeypatch.setattr(watcher_welcome, "ensure_session_for_thread", AsyncMock())
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "append_welcome_ticket_row", lambda *_, **__: "inserted")

    with caplog.at_level("INFO", logger="c1c.onboarding.welcome_watcher"):
        asyncio.run(watcher._handle_ticket_open(thread, context))

    assert any(
        "sheet_update=ok" in record.message and "phase=created" in record.message
        for record in caplog.records
    )


def test_welcome_sheet_logging_failure_pings_admin(monkeypatch, caplog):
    watcher = WelcomeTicketWatcher(bot=MagicMock())
    watcher.bot.user = SimpleNamespace(id=999, bot=True)
    thread = SimpleNamespace(
        id=555,
        created_at=datetime(2025, 5, 5, tzinfo=timezone.utc),
        name="W5555-smurf",
    )
    context = TicketContext(thread_id=555, ticket_number="W5555", username="smurf")

    monkeypatch.setattr(watcher_welcome, "locate_welcome_message", AsyncMock(return_value=None))
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "find_welcome_row", lambda ticket: None)
    monkeypatch.setattr(watcher_welcome, "ensure_session_for_thread", AsyncMock())

    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "modules.onboarding.sheet_logging.get_admin_role_ids", lambda: {1234}
    )
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "append_welcome_ticket_row", boom)

    with caplog.at_level("ERROR", logger="c1c.onboarding.welcome_watcher"):
        asyncio.run(watcher._handle_ticket_open(thread, context))

    assert any(
        "sheet_update=failed" in record.message and "<@&1234>" in record.message
        for record in caplog.records
    )


def test_welcome_reminder_sheet_touch_logs(monkeypatch, caplog):
    watcher = WelcomeTicketWatcher(bot=MagicMock())
    context = TicketContext(thread_id=777, ticket_number="W7777", username="loggy")
    thread = SimpleNamespace(id=777, name="W7777-loggy")

    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "find_welcome_row", lambda ticket: None)
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "upsert_welcome", lambda *_args, **_kwargs: "updated")

    with caplog.at_level("INFO", logger="c1c.onboarding.welcome_watcher"):
        asyncio.run(
            watcher._touch_welcome_sheet_for_reminder(
                phase="reminder_24h",
                thread=thread,
                context=context,
                created_at=datetime.now(timezone.utc),
                user_ref="<@777>",
            )
        )

    assert any(
        "sheet_update=ok" in record.message and "phase=reminder_24h" in record.message
        for record in caplog.records
    )


def test_setup_is_idempotent_when_watchers_already_registered(monkeypatch):
    class DummyBot:
        def __init__(self):
            self._cogs = {}
            self.added = []

        def get_cog(self, name):
            return self._cogs.get(name)

        async def add_cog(self, cog):
            name = cog.__class__.__name__
            self._cogs[name] = cog
            self.added.append(name)

    bot = DummyBot()
    bot._cogs["WelcomeWatcher"] = WelcomeWatcher(bot)
    bot._cogs["WelcomeTicketWatcher"] = WelcomeTicketWatcher(bot)

    monkeypatch.setattr(watcher_welcome, "_ensure_reminder_job", lambda _bot: None)
    ensure_idle = AsyncMock()
    monkeypatch.setattr("modules.onboarding.idle_watcher.ensure_idle_watcher", ensure_idle)

    asyncio.run(watcher_welcome.setup(bot))

    assert bot.added == []
    ensure_idle.assert_awaited_once_with(bot)


def test_welcome_reminder_sheet_touch_logs_failure(monkeypatch, caplog):
    watcher = WelcomeTicketWatcher(bot=MagicMock())
    context = TicketContext(thread_id=888, ticket_number="W8888", username="logger")
    thread = SimpleNamespace(id=888, name="W8888-logger")

    def boom(*_args, **_kwargs):
        raise RuntimeError("reminder boom")

    monkeypatch.setattr(
        "modules.onboarding.sheet_logging.get_admin_role_ids", lambda: {4242}
    )
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "find_welcome_row", lambda ticket: None)
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "upsert_welcome", boom)

    with caplog.at_level("ERROR", logger="c1c.onboarding.welcome_watcher"):
        asyncio.run(
            watcher._touch_welcome_sheet_for_reminder(
                phase="reminder_3h",
                thread=thread,
                context=context,
                created_at=datetime.now(timezone.utc),
                user_ref="<@888>",
            )
        )

    assert any(
        "sheet_update=failed" in record.message and "<@&4242>" in record.message
        for record in caplog.records
    )


def test_welcome_auto_close_logs_sheet_update(monkeypatch, caplog):
    watcher = WelcomeTicketWatcher(bot=MagicMock())
    context = TicketContext(thread_id=999, ticket_number="W9999", username="closer")
    thread = SimpleNamespace(id=999, name="W9999-closer", send=AsyncMock(), edit=AsyncMock())

    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "find_welcome_row", lambda ticket: None)
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "upsert_welcome", lambda *_args, **_kwargs: "updated")
    monkeypatch.setattr(watcher_welcome, "_log_finalize_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr(watcher_welcome.reservations_sheets, "find_active_reservations_for_recruit", AsyncMock(return_value=[]))
    monkeypatch.setattr(watcher_welcome.recruitment_sheets, "find_clan_row", lambda *_: None)
    monkeypatch.setattr(watcher_welcome, "_clan_math_column_indices", lambda: {})
    monkeypatch.setattr(watcher_welcome, "_capture_clan_snapshots", lambda *a, **k: {})

    with caplog.at_level("INFO", logger="c1c.onboarding.welcome_watcher"):
        asyncio.run(
            watcher._finalize_clan_tag(
                thread,
                context,
                watcher_welcome._NO_PLACEMENT_TAG,
                actor=None,
                source="auto_close",
                prompt_message=None,
                view=None,
                notify=False,
                rename_thread=False,
                sheet_phase="auto_close",
            )
        )

    assert any(
        "sheet_update=ok" in record.message and "phase=auto_close" in record.message
        for record in caplog.records
    )


def test_welcome_auto_close_logging_failure_mentions_admin(monkeypatch, caplog):
    watcher = WelcomeTicketWatcher(bot=MagicMock())
    context = TicketContext(thread_id=1001, ticket_number="W1001", username="closer")
    thread = SimpleNamespace(id=1001, name="W1001-closer", send=AsyncMock(), edit=AsyncMock())

    def boom(*_args, **_kwargs):
        raise RuntimeError("close boom")

    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "find_welcome_row", lambda ticket: None)
    monkeypatch.setattr(watcher_welcome.onboarding_sheets, "upsert_welcome", boom)
    monkeypatch.setattr("modules.onboarding.sheet_logging.get_admin_role_ids", lambda: {5150})
    monkeypatch.setattr(watcher_welcome, "_log_finalize_summary", lambda *args, **kwargs: None)
    monkeypatch.setattr(watcher_welcome.reservations_sheets, "find_active_reservations_for_recruit", AsyncMock(return_value=[]))
    monkeypatch.setattr(watcher_welcome.recruitment_sheets, "find_clan_row", lambda *_: None)
    monkeypatch.setattr(watcher_welcome, "_clan_math_column_indices", lambda: {})
    monkeypatch.setattr(watcher_welcome, "_capture_clan_snapshots", lambda *a, **k: {})

    with caplog.at_level("ERROR", logger="c1c.onboarding.welcome_watcher"):
        asyncio.run(
            watcher._finalize_clan_tag(
                thread,
                context,
                watcher_welcome._NO_PLACEMENT_TAG,
                actor=None,
                source="auto_close",
                prompt_message=None,
                view=None,
                notify=False,
                rename_thread=False,
                sheet_phase="auto_close",
            )
        )

    assert any(
        "sheet_update=failed" in record.message and "<@&5150>" in record.message
        for record in caplog.records
    )
