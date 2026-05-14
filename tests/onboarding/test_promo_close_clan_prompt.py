import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from modules.onboarding.watcher_promo import PromoTicketContext, PromoTicketWatcher


class DummyThread:
    def __init__(self, thread_id=1, name="R1234-user"):
        self.id = thread_id
        self.name = name
        self.archived = False
        self.locked = False
        self.parent_id = 123
        self.sent = []

    async def send(self, content=None, **kwargs):
        msg = SimpleNamespace(id=999, content=content or "", created_at=datetime.now(timezone.utc))
        self.sent.append((content, kwargs))
        return msg


class DummyAuthor:
    def __init__(self, bot=False, user_id=42):
        self.bot = bot
        self.id = user_id


class DummyBot:
    user = SimpleNamespace(id=111)


def _context(state="open", clan_tag=""):
    return PromoTicketContext(
        thread_id=1,
        ticket_number="R1234",
        username="user",
        promo_type="Returning",
        thread_created="2026-01-01 00:00:00",
        year="2026",
        month="January",
        state=state,
        clan_tag=clan_tag,
    )


def _setup_watcher(monkeypatch):
    monkeypatch.setattr("modules.common.feature_flags.is_enabled", lambda *_: True)
    monkeypatch.setattr("modules.onboarding.watcher_promo.get_promo_channel_id", lambda: 123)
    monkeypatch.setattr("modules.onboarding.watcher_promo.get_ticket_tool_bot_id", lambda: 555)
    monkeypatch.setattr("modules.onboarding.watcher_promo.thread_scopes.is_promo_parent", lambda *_: True)
    monkeypatch.setattr("modules.onboarding.watcher_promo.onboarding_sessions.get_by_thread_id", lambda *_: None)
    monkeypatch.setattr("modules.onboarding.watcher_promo.discord.Thread", DummyThread)
    return PromoTicketWatcher(bot=DummyBot())


def test_close_no_detected_tag_shows_picker(monkeypatch):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context()
    watcher._ensure_context = AsyncMock(return_value=ctx)
    watcher._begin_clan_prompt = AsyncMock()

    before = DummyThread(1)
    after = DummyThread(1)
    after.archived = True
    asyncio.run(watcher.on_thread_update(before, after))
    watcher._begin_clan_prompt.assert_awaited_once_with(after, ctx)


def test_close_preloaded_tag_still_shows_picker(monkeypatch):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context(clan_tag="C1CE")
    watcher._ensure_context = AsyncMock(return_value=ctx)
    watcher._begin_clan_prompt = AsyncMock()

    before = DummyThread(2)
    after = DummyThread(2)
    after.archived = True
    asyncio.run(watcher.on_thread_update(before, after))
    watcher._begin_clan_prompt.assert_awaited_once_with(after, ctx)


def test_close_with_existing_row_tag_still_shows_picker(monkeypatch):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context(clan_tag="C1CW")
    watcher._ensure_context = AsyncMock(return_value=ctx)
    watcher._begin_clan_prompt = AsyncMock()

    before = DummyThread(3)
    after = DummyThread(3)
    after.archived = True
    asyncio.run(watcher.on_thread_update(before, after))
    watcher._begin_clan_prompt.assert_awaited_once_with(after, ctx)


def test_finalize_not_called_during_close_handling(monkeypatch):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context()
    watcher._ensure_context = AsyncMock(return_value=ctx)
    watcher._begin_clan_prompt = AsyncMock()
    watcher._finalize_clan_tag = AsyncMock()

    before = DummyThread(4)
    after = DummyThread(4)
    after.archived = True
    asyncio.run(watcher.on_thread_update(before, after))
    watcher._finalize_clan_tag.assert_not_awaited()


def test_typed_tag_while_awaiting_clan_finalizes(monkeypatch):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context(state="awaiting_clan")
    watcher._ensure_context = AsyncMock(return_value=ctx)
    watcher._load_clan_tags = AsyncMock(return_value=["C1CE"])
    watcher._finalize_clan_tag = AsyncMock()

    thread = DummyThread(5)
    message = SimpleNamespace(channel=thread, author=DummyAuthor(bot=False, user_id=77), content="c1ce")
    asyncio.run(watcher.on_message(message))
    watcher._finalize_clan_tag.assert_awaited_once()


def test_dropdown_selection_finalizes(monkeypatch):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context(state="awaiting_clan")
    watcher._finalize_clan_tag = AsyncMock()
    interaction = SimpleNamespace(channel=DummyThread(6), user=DummyAuthor(), message=SimpleNamespace(), followup=SimpleNamespace(send=AsyncMock()))
    view = SimpleNamespace()
    asyncio.run(watcher.finalize_from_interaction(ctx, "C1CE", interaction, view))
    watcher._finalize_clan_tag.assert_awaited_once()


def test_after_clan_confirm_progression_still_completes(monkeypatch):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context(state="awaiting_clan")
    watcher._ensure_context = AsyncMock(return_value=ctx)
    watcher._load_clan_tags = AsyncMock(return_value=["C1CE"])
    monkeypatch.setattr("modules.onboarding.watcher_promo.onboarding_sheets.upsert_promo", lambda *_: "updated")
    thread = DummyThread(7, "closed-R7777-user")
    thread.edit = AsyncMock()

    message = SimpleNamespace(channel=thread, author=DummyAuthor(bot=False), content="C1CE")
    asyncio.run(watcher.on_message(message))
    assert ctx.state == "closed"
    assert ctx.clan_tag == "C1CE"
    assert any("Please reply with progression" in (content or "") for content, _ in thread.sent) is False


def test_finalize_never_enters_awaiting_details(monkeypatch):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context(state="awaiting_clan")
    monkeypatch.setattr("modules.onboarding.watcher_promo.onboarding_sheets.upsert_promo", lambda *_: "updated")
    watcher._load_clan_tags = AsyncMock(return_value=["C1CE"])
    thread = DummyThread(8, "closed-R8888-user")
    thread.edit = AsyncMock()
    asyncio.run(
        watcher._finalize_clan_tag(
            thread,
            ctx,
            "C1CE",
            actor=None,
            prompt_message=None,
            view=None,
        )
    )
    assert ctx.state == "closed"


def test_promo_close_logs_open_spots_reconcile_skipped(monkeypatch, caplog):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context(state="awaiting_clan")
    monkeypatch.setattr("modules.onboarding.watcher_promo.onboarding_sheets.upsert_promo", lambda *_: "updated")
    watcher._load_clan_tags = AsyncMock(return_value=["C1CE"])
    thread = DummyThread(10, "closed-R1111-user")
    thread.edit = AsyncMock()
    with caplog.at_level(logging.INFO):
        asyncio.run(
            watcher._finalize_clan_tag(
                thread,
                ctx,
                "C1CE",
                actor=None,
                prompt_message=None,
                view=None,
            )
        )
    assert "reason=no_promo_open_spots_reconcile_currently" in caplog.text


def test_duplicate_close_signal_ignored_after_closed(monkeypatch):
    watcher = _setup_watcher(monkeypatch)
    ctx = _context(state="closed", clan_tag="C1CE")
    watcher._ensure_context = AsyncMock(return_value=ctx)
    watcher._begin_clan_prompt = AsyncMock()
    before = DummyThread(9)
    after = DummyThread(9)
    after.archived = True
    asyncio.run(watcher.on_thread_update(before, after))
    watcher._begin_clan_prompt.assert_not_awaited()
