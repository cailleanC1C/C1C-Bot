import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.onboarding import reaction_fallback


class DummyThread:
    def __init__(self, message):
        self.id = 5001
        self._message = message

    async def fetch_message(self, message_id):
        return self._message


class DummyGuild:
    def __init__(self, member, thread):
        self.id = 2001
        self._member = member
        self._thread = thread

    def get_member(self, user_id):
        return self._member if self._member.id == user_id else None

    async def fetch_member(self, user_id):
        raise AssertionError("should not fetch member")

    def get_thread(self, channel_id):
        return self._thread


class DummyBot:
    def __init__(self, guild):
        self.user = SimpleNamespace(id=9999)
        self._guild = guild

    def get_guild(self, guild_id):
        return self._guild if guild_id == self._guild.id else None

    def get_channel(self, channel_id):
        return None

    async def fetch_channel(self, channel_id):
        raise AssertionError("should not fetch channel")


@pytest.fixture(autouse=True)
def patch_feature_flag(monkeypatch):
    monkeypatch.setattr(
        reaction_fallback.feature_flags,
        "is_enabled",
        lambda flag: True,
    )


@pytest.fixture(autouse=True)
def patch_member_type(monkeypatch):
    class DummyMember:
        def __init__(self, member_id):
            self.id = member_id
            self.bot = False

    monkeypatch.setattr(reaction_fallback.discord, "Member", DummyMember)
    return DummyMember


@pytest.fixture(autouse=True)
def patch_ticket_tool(monkeypatch):
    monkeypatch.setattr(reaction_fallback, "get_ticket_tool_bot_id", lambda: 5555)


def _build_payload(member, thread, message):
    return SimpleNamespace(
        emoji=reaction_fallback.FALLBACK_EMOJI,
        guild_id=2001,
        user_id=member.id,
        channel_id=thread.id,
        message_id=message.id,
        member=member,
    )


async def _run_event(
    monkeypatch,
    caplog,
    *,
    message_content: str,
    is_admin: bool = False,
    is_recruiter: bool = True,
    welcome_parent: bool = True,
    promo_parent: bool = False,
    thread_join: bool = True,
    thread_join_error: Exception | None = None,
    message_author_id: int | None = None,
):
    author = SimpleNamespace(id=message_author_id) if message_author_id is not None else None
    message = SimpleNamespace(id=7001, content=message_content, author=author)
    thread = DummyThread(message)
    member = reaction_fallback.discord.Member(1234)
    guild = DummyGuild(member, thread)
    bot = DummyBot(guild)
    payload = _build_payload(member, thread, message)

    monkeypatch.setattr(
        reaction_fallback.thread_scopes,
        "is_welcome_parent",
        lambda current: welcome_parent,
    )
    monkeypatch.setattr(
        reaction_fallback.thread_scopes,
        "is_promo_parent",
        lambda current: promo_parent,
    )
    monkeypatch.setattr(
        reaction_fallback.rbac,
        "is_admin_member",
        lambda current: is_admin,
    )
    monkeypatch.setattr(
        reaction_fallback.rbac,
        "is_recruiter",
        lambda current: is_recruiter,
    )

    join_mock = AsyncMock(return_value=(thread_join, thread_join_error))
    monkeypatch.setattr(
        reaction_fallback.thread_membership,
        "ensure_thread_membership",
        join_mock,
    )

    start_mock = AsyncMock()
    monkeypatch.setattr(reaction_fallback, "start_welcome_dialog", start_mock)

    cog = reaction_fallback.OnboardingReactionFallbackCog(bot)

    caplog.set_level(logging.INFO)
    await cog.on_raw_reaction_add(payload)

    return start_mock, caplog, join_mock


def _record_details(record):
    details = None
    if isinstance(record.args, dict):
        details = record.args
    elif record.args:
        candidate = record.args[0]
        if isinstance(candidate, dict):
            details = candidate

    if details is not None:
        return details

    message = getattr(record, "msg", None)
    if isinstance(message, str):
        try:
            return json.loads(message)
        except json.JSONDecodeError:
            return None
    return None


def _find_log(caplog, key):
    for record in caplog.records:
        details = _record_details(record)
        if details and key in details:
            return details
    raise AssertionError(f"log with {key!r} not found")


def test_phrase_match_starts_dialog(monkeypatch, caplog):
    start_mock, caplog, join_mock = asyncio.run(
        _run_event(
            monkeypatch,
            caplog,
            message_content="Close the ticket By Reacting With 🎫 please!",
        )
    )

    start_mock.assert_awaited_once()
    join_mock.assert_awaited_once()
    details = _find_log(caplog, "trigger")
    assert details["trigger"] == "phrase_match"


def test_token_match_starts_dialog(monkeypatch, caplog):
    start_mock, caplog, join_mock = asyncio.run(
        _run_event(
            monkeypatch,
            caplog,
            message_content="Confirm closure with [#welcome:ticket]",
        )
    )

    start_mock.assert_awaited_once()
    join_mock.assert_awaited_once()
    details = _find_log(caplog, "trigger")
    assert details["trigger"] == "token_match"


def test_no_trigger_rejected_even_for_admin(monkeypatch, caplog):
    start_mock, caplog, join_mock = asyncio.run(
        _run_event(
            monkeypatch,
            caplog,
            message_content="Close the ticket now.",
            is_admin=True,
            is_recruiter=False,
        )
    )

    start_mock.assert_not_called()
    join_mock.assert_awaited_once()
    details = _find_log(caplog, "result")
    assert details["result"] == "no_trigger"


def test_wrong_parent_rejected(monkeypatch, caplog):
    start_mock, caplog, join_mock = asyncio.run(
        _run_event(
            monkeypatch,
            caplog,
            message_content="By reacting with 🎫",
            welcome_parent=False,
            promo_parent=False,
        )
    )

    start_mock.assert_not_called()
    join_mock.assert_not_called()
    assert not caplog.records


def test_promo_parent_allows_dialog(monkeypatch, caplog):
    start_mock, caplog, join_mock = asyncio.run(
        _run_event(
            monkeypatch,
            caplog,
            message_content="Close the ticket By Reacting With 🎫 please!",
            welcome_parent=False,
            promo_parent=True,
        )
    )

    start_mock.assert_not_called()
    join_mock.assert_awaited_once()
    details = _find_log(caplog, "result")
    assert details["result"] == "no_trigger"


def test_role_gate_rejected(monkeypatch, caplog):
    start_mock, caplog, join_mock = asyncio.run(
        _run_event(
            monkeypatch,
            caplog,
            message_content="[#welcome:ticket]",
            is_admin=False,
            is_recruiter=False,
        )
    )

    start_mock.assert_not_called()
    join_mock.assert_awaited_once()
    details = _find_log(caplog, "result")
    assert details["result"] == "ambiguous_target"


def test_thread_join_failure_logs(monkeypatch, caplog):
    start_mock, caplog, join_mock = asyncio.run(
        _run_event(
            monkeypatch,
            caplog,
            message_content="Close the ticket By Reacting With 🎫 please!",
            thread_join=False,
            thread_join_error=RuntimeError("cannot join"),
        )
    )

    start_mock.assert_not_called()
    join_mock.assert_awaited_once()
    details = _find_log(caplog, "result")
    assert details["result"] == "thread_join_failed"
    assert details["trigger"] == "thread_join"


def test_promo_trigger_starts_dialog(monkeypatch, caplog):
    start_mock, caplog, join_mock = asyncio.run(
        _run_event(
            monkeypatch,
            caplog,
            message_content="Ticket Tool says <!-- trigger:promo.r -->",
            welcome_parent=False,
            promo_parent=True,
            message_author_id=5555,
        )
    )

    start_mock.assert_awaited_once()
    join_mock.assert_awaited_once()
    details = _find_log(caplog, "result")
    assert details["result"] == "emoji_received"


def test_promo_trigger_rejected_for_wrong_author(monkeypatch, caplog):
    start_mock, caplog, join_mock = asyncio.run(
        _run_event(
            monkeypatch,
            caplog,
            message_content="Ticket Tool says <!-- trigger:promo.m -->",
            welcome_parent=False,
            promo_parent=True,
            message_author_id=1234,
        )
    )

    start_mock.assert_not_called()
    join_mock.assert_awaited_once()
    details = _find_log(caplog, "result")
    assert details["result"] == "no_trigger"
