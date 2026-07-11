from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio

import pytest

from modules.onboarding import cmd_finishplacement as fp
from modules.onboarding.watcher_promo import PromoTicketContext, PromoTicketWatcher
from modules.onboarding.watcher_welcome import WelcomeTicketWatcher


class DummyThread:
    def __init__(self, *, thread_id=123, name="W0919-player", parent_id=10):
        self.id = thread_id
        self.name = name
        self.parent_id = parent_id
        self.guild = SimpleNamespace(id=1)
        self.created_at = None
        self.edited = []
        self.sent = []

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        return SimpleNamespace(id=999, content=content)

    async def edit(self, **kwargs):
        self.edited.append(kwargs)
        if "name" in kwargs:
            self.name = kwargs["name"]


class DummyCtx:
    def __init__(self, channel, *, author=None):
        self.channel = channel
        self.author = author or SimpleNamespace(id=42)
        self.replies = []

    async def reply(self, content, **kwargs):
        self.replies.append((content, kwargs))


class DummyBot:
    def __init__(self, **cogs):
        self._cogs = cogs

    def get_cog(self, name):
        return self._cogs.get(name)


@pytest.fixture(autouse=True)
def _common(monkeypatch):
    monkeypatch.setattr(fp.discord, "Thread", DummyThread)
    monkeypatch.setattr(fp, "is_staff_member", lambda member: True)
    monkeypatch.setattr(fp, "is_admin_member", lambda member: False)
    monkeypatch.setattr(fp, "is_recruiter", lambda member: False)
    monkeypatch.setattr(
        fp.onboarding_sheets, "load_clan_tags", lambda: ["C1CB", "C1CD"]
    )
    monkeypatch.setattr(
        fp.thread_scopes, "is_welcome_parent", lambda thread: thread.parent_id == 10
    )
    monkeypatch.setattr(
        fp.thread_scopes, "is_promo_parent", lambda thread: thread.parent_id == 20
    )
    monkeypatch.setattr(fp.onboarding_sessions, "get_by_thread_id", lambda *_: None)
    monkeypatch.setattr(
        fp.onboarding_sheets, "find_welcome_row_by_thread_id", lambda *_: None
    )
    monkeypatch.setattr(
        fp.onboarding_sheets, "find_promo_row_by_thread_id", lambda *_: None
    )
    monkeypatch.setattr(fp.onboarding_sheets, "find_welcome_row", lambda *_: None)
    monkeypatch.setattr(fp.onboarding_sheets, "find_promo_row", lambda *_: None)


def test_staff_can_finish_welcome_ticket(monkeypatch):
    watcher = WelcomeTicketWatcher(SimpleNamespace())
    watcher._finalize_clan_tag = AsyncMock(
        side_effect=lambda _thread, context, *_args, **_kwargs: setattr(
            context, "state", "closed"
        )
    )
    cog = fp.FinishPlacementCog(DummyBot(WelcomeTicketWatcher=watcher))
    thread = DummyThread(name="W0919-player", parent_id=10)
    ctx = DummyCtx(thread)

    asyncio.run(
        fp.FinishPlacementCog.finishplacement.callback(cog, ctx, "NONE", "C1CD")
    )

    watcher._finalize_clan_tag.assert_awaited_once()
    called_context = watcher._finalize_clan_tag.await_args.args[1]
    assert called_context.ticket_number == "W0919"
    assert called_context.username == "player"
    assert watcher._finalize_clan_tag.await_args.args[2] == "C1CD"
    assert ctx.replies[-1][0] == "Placement finalized: **C1CD**."


def test_staff_can_finish_welcome_ticket_with_none_destination(monkeypatch):
    monkeypatch.setattr(
        fp.onboarding_sheets,
        "load_clan_tags",
        lambda: (_ for _ in ()).throw(
            AssertionError("should not validate NONE as a clan tag")
        ),
    )
    watcher = WelcomeTicketWatcher(SimpleNamespace())
    watcher._finalize_clan_tag = AsyncMock(
        side_effect=lambda _thread, context, *_args, **_kwargs: setattr(
            context, "state", "closed"
        )
    )
    cog = fp.FinishPlacementCog(DummyBot(WelcomeTicketWatcher=watcher))
    thread = DummyThread(name="W0919-player", parent_id=10)
    ctx = DummyCtx(thread)

    asyncio.run(
        fp.FinishPlacementCog.finishplacement.callback(cog, ctx, "NONE", "NONE")
    )

    watcher._finalize_clan_tag.assert_awaited_once()
    assert watcher._finalize_clan_tag.await_args.args[2] == "NONE"
    assert ctx.replies[-1][0] == "Placement finalized: **NONE**."
    assert all(
        "Destination clan tag is required." not in reply[0] for reply in ctx.replies
    )


def test_staff_can_finish_promo_ticket_with_lowercase_none_destination(monkeypatch):
    monkeypatch.setattr(
        fp.onboarding_sheets,
        "load_clan_tags",
        lambda: (_ for _ in ()).throw(
            AssertionError("should not validate NONE as a clan tag")
        ),
    )
    watcher = PromoTicketWatcher(SimpleNamespace())
    watcher._complete_close = AsyncMock(
        side_effect=lambda _thread, context, *_args, **_kwargs: setattr(
            context, "state", "closed"
        )
    )
    cog = fp.FinishPlacementCog(DummyBot(PromoTicketWatcher=watcher))
    thread = DummyThread(name="M0354-player", parent_id=20)
    ctx = DummyCtx(thread)

    asyncio.run(
        fp.FinishPlacementCog.finishplacement.callback(cog, ctx, "none", "none")
    )

    watcher._complete_close.assert_awaited_once()
    promo_context = watcher._complete_close.await_args.args[1]
    assert promo_context.source_clan_tag == "NONE"
    assert promo_context.clan_tag == "NONE"
    assert watcher._complete_close.await_args.kwargs["previous_final"] == "NONE"
    assert ctx.replies[-1][0] == "Placement finalized: **NONE** → **NONE**."
    assert all(
        "Destination clan tag is required." not in reply[0] for reply in ctx.replies
    )


def test_destination_none_does_not_update_open_spots_or_reservations_in_command(monkeypatch):
    watcher = PromoTicketWatcher(SimpleNamespace())

    async def fake_complete_close(_thread, context, *_args, **_kwargs):
        if context.clan_tag != "NONE":
            raise AssertionError("destination NONE was not preserved for finalizer")
        context.state = "closed"

    watcher._complete_close = AsyncMock(side_effect=fake_complete_close)
    cog = fp.FinishPlacementCog(DummyBot(PromoTicketWatcher=watcher))
    thread = DummyThread(name="M0354-player", parent_id=20)
    ctx = DummyCtx(thread)

    asyncio.run(
        fp.FinishPlacementCog.finishplacement.callback(cog, ctx, "NONE", "NONE")
    )

    watcher._complete_close.assert_awaited_once()
    promo_context = watcher._complete_close.await_args.args[1]
    assert promo_context.source_clan_tag == "NONE"
    assert promo_context.clan_tag == "NONE"


def test_staff_can_finish_promo_move_ticket(monkeypatch):
    watcher = PromoTicketWatcher(SimpleNamespace())
    watcher._complete_close = AsyncMock(
        side_effect=lambda _thread, context, *_args, **_kwargs: setattr(
            context, "state", "closed"
        )
    )
    cog = fp.FinishPlacementCog(DummyBot(PromoTicketWatcher=watcher))
    thread = DummyThread(name="M0354-player", parent_id=20)
    ctx = DummyCtx(thread)

    asyncio.run(
        fp.FinishPlacementCog.finishplacement.callback(cog, ctx, "C1CB", "C1CD")
    )

    watcher._complete_close.assert_awaited_once()
    promo_context = watcher._complete_close.await_args.args[1]
    assert promo_context.ticket_number == "M0354"
    assert promo_context.source_clan_tag == "C1CB"
    assert promo_context.clan_tag == "C1CD"
    assert watcher._complete_close.await_args.kwargs["previous_final"] == "C1CB"
    assert ctx.replies[-1][0] == "Placement finalized: **C1CB** → **C1CD**."


def test_promo_none_source_passes_none_to_shared_finalizer(monkeypatch):
    watcher = PromoTicketWatcher(SimpleNamespace())
    watcher._complete_close = AsyncMock(
        side_effect=lambda _thread, context, *_args, **_kwargs: setattr(
            context, "state", "closed"
        )
    )
    cog = fp.FinishPlacementCog(DummyBot(PromoTicketWatcher=watcher))
    thread = DummyThread(name="M0354-player", parent_id=20)
    ctx = DummyCtx(thread)

    asyncio.run(
        fp.FinishPlacementCog.finishplacement.callback(cog, ctx, "NONE", "C1CD")
    )

    promo_context = watcher._complete_close.await_args.args[1]
    assert promo_context.source_clan_tag == "NONE"
    assert watcher._complete_close.await_args.kwargs["previous_final"] == "NONE"


def test_non_staff_is_rejected(monkeypatch):
    monkeypatch.setattr(fp, "is_staff_member", lambda member: False)
    monkeypatch.setattr(fp, "is_admin_member", lambda member: False)
    monkeypatch.setattr(fp, "is_recruiter", lambda member: False)
    watcher = WelcomeTicketWatcher(SimpleNamespace())
    watcher._finalize_clan_tag = AsyncMock()
    cog = fp.FinishPlacementCog(DummyBot(WelcomeTicketWatcher=watcher))
    ctx = DummyCtx(DummyThread(parent_id=10))

    asyncio.run(
        fp.FinishPlacementCog.finishplacement.callback(cog, ctx, "NONE", "C1CD")
    )

    watcher._finalize_clan_tag.assert_not_awaited()
    assert ctx.replies == [("Staff only.", {"mention_author": False})]


def test_wrong_thread_type_is_rejected():
    watcher = WelcomeTicketWatcher(SimpleNamespace())
    watcher._finalize_clan_tag = AsyncMock()
    cog = fp.FinishPlacementCog(DummyBot(WelcomeTicketWatcher=watcher))
    ctx = DummyCtx(DummyThread(parent_id=999))

    asyncio.run(
        fp.FinishPlacementCog.finishplacement.callback(cog, ctx, "NONE", "C1CD")
    )

    watcher._finalize_clan_tag.assert_not_awaited()
    assert ctx.replies[-1][0] == "Use this inside a welcome or promo ticket thread."


def test_already_finalized_ticket_does_not_call_finalizer():
    watcher = PromoTicketWatcher(SimpleNamespace())
    watcher._tickets[123] = PromoTicketContext(
        thread_id=123,
        ticket_number="M0354",
        username="player",
        promo_type="promo.m",
        thread_created="",
        year="2026",
        month="June",
        clan_tag="C1CD",
        source_clan_tag="C1CB",
        state="closed",
    )
    watcher._complete_close = AsyncMock()
    cog = fp.FinishPlacementCog(DummyBot(PromoTicketWatcher=watcher))
    ctx = DummyCtx(DummyThread(thread_id=123, name="M0354-player", parent_id=20))

    asyncio.run(
        fp.FinishPlacementCog.finishplacement.callback(cog, ctx, "C1CB", "C1CD")
    )

    watcher._complete_close.assert_not_awaited()
    assert ctx.replies[-1][0] == "This ticket already appears finalized."


def test_welcome_real_source_is_rejected_safely():
    watcher = WelcomeTicketWatcher(SimpleNamespace())
    watcher._finalize_clan_tag = AsyncMock()
    cog = fp.FinishPlacementCog(DummyBot(WelcomeTicketWatcher=watcher))
    ctx = DummyCtx(DummyThread(parent_id=10))

    asyncio.run(
        fp.FinishPlacementCog.finishplacement.callback(cog, ctx, "C1CB", "C1CD")
    )

    watcher._finalize_clan_tag.assert_not_awaited()
    assert (
        ctx.replies[-1][0] == "Welcome tickets only support `NONE` as the source clan."
    )
