import asyncio
from types import SimpleNamespace

import discord

from modules.community.live_arena import victory_ledger_thread_parent as fix
from modules.community.live_arena import victory_ledger_workspace as workspace


class FakeMessage:
    def __init__(self, message_id):
        self.id = int(message_id)
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class FakeThread:
    def __init__(self, thread_id, name):
        self.id = int(thread_id)
        self.name = name
        self.archived = False
        self.sent = []
        self.messages = {}

    async def send(self, **kwargs):
        message = FakeMessage(9000 + len(self.sent))
        self.sent.append((message, kwargs))
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id):
        return self.messages.get(int(message_id))

    async def edit(self, **kwargs):
        if "archived" in kwargs:
            self.archived = kwargs["archived"]


class FakeContainer:
    def __init__(self, channel_id="50"):
        self.id = int(channel_id)
        self.threads = []
        self.create_calls = []

    async def create_thread(self, **kwargs):
        self.create_calls.append(kwargs)
        thread = FakeThread(6000 + len(self.create_calls), kwargs["name"])
        self.threads.append(thread)
        return thread


class FakeForumContainer(FakeContainer):
    async def create_thread(self, **kwargs):
        self.create_calls.append(kwargs)
        thread = FakeThread(7000 + len(self.create_calls), kwargs["name"])
        starter = FakeMessage(8000 + len(self.create_calls))
        thread.messages[starter.id] = starter
        self.threads.append(thread)
        return SimpleNamespace(thread=thread, message=starter)


class FakeLedgerThread:
    def __init__(self, parent):
        self.id = 10
        self.parent = parent


class FakeRepository:
    def __init__(self):
        self.upserts = []

    async def upsert_discord_resource(self, **kwargs):
        self.upserts.append(kwargs)


class FakeTemplate:
    title = "Round Archive"

    def embed(self):
        return "intro-embed"


def test_ledger_thread_creates_workspace_sibling_on_parent_channel(monkeypatch):
    container = FakeContainer()
    ledger = FakeLedgerThread(container)
    repo = FakeRepository()
    resources = {}

    monkeypatch.setattr(fix, "_is_thread_destination", lambda _channel: True)
    monkeypatch.setattr(fix, "_is_forum_container", lambda _channel: False)

    async def original(**_kwargs):
        raise AssertionError("thread destination must not use the legacy child-thread path")

    thread = asyncio.run(
        fix._ensure_thread_from_ledger_parent(
            workspace,
            original,
            bot=object(),
            repository=repo,
            parent=ledger,
            key="round_archive",
            template=FakeTemplate(),
            resources=resources,
        )
    )

    assert thread.id == 6001
    assert len(container.create_calls) == 1
    call = container.create_calls[0]
    assert call["type"] == discord.ChannelType.public_thread
    assert call["name"] == "Round Archive"
    assert len(thread.sent) == 1
    assert repo.upserts[0]["channel_id"] == "50"
    assert repo.upserts[0]["thread_id"] == "6001"
    assert repo.upserts[0]["message_id"] == str(thread.sent[0][0].id)


def test_ledger_forum_post_creates_sibling_forum_post_without_duplicate_intro(monkeypatch):
    container = FakeForumContainer("51")
    ledger = FakeLedgerThread(container)
    repo = FakeRepository()
    resources = {}

    monkeypatch.setattr(fix, "_is_thread_destination", lambda _channel: True)
    monkeypatch.setattr(fix, "_is_forum_container", lambda _channel: True)

    async def original(**_kwargs):
        raise AssertionError("thread destination must not use the legacy child-thread path")

    thread = asyncio.run(
        fix._ensure_thread_from_ledger_parent(
            workspace,
            original,
            bot=object(),
            repository=repo,
            parent=ledger,
            key="tournament_results",
            template=FakeTemplate(),
            resources=resources,
        )
    )

    assert thread.id == 7001
    call = container.create_calls[0]
    assert "type" not in call
    assert call["embed"] == "intro-embed"
    assert thread.sent == []
    assert repo.upserts[0]["channel_id"] == "51"
    assert repo.upserts[0]["thread_id"] == "7001"
    assert repo.upserts[0]["message_id"] == "8001"


def test_non_thread_destination_delegates_to_original(monkeypatch):
    monkeypatch.setattr(fix, "_is_thread_destination", lambda _channel: False)
    seen = []

    async def original(**kwargs):
        seen.append(kwargs)
        return "legacy-result"

    result = asyncio.run(
        fix._ensure_thread_from_ledger_parent(
            workspace,
            original,
            bot="bot",
            repository="repo",
            parent="channel",
            key="hall_of_fame",
            template="template",
            resources={},
        )
    )

    assert result == "legacy-result"
    assert seen[0]["parent"] == "channel"
