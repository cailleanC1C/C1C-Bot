import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from cogs.recruitment_welcome import WelcomeBridge
from modules.recruitment import welcome as welcome_module
from shared.sheets import recruitment as recruitment_sheets


class FakeChannel:
    def __init__(self, channel_id: int):
        self.id = channel_id
        self.sent = []

    async def send(self, *args, **kwargs):
        content = kwargs.get("content")
        embed = kwargs.get("embed")
        if args:
            content = args[0]
        payload = {"content": content, "embed": embed}
        self.sent.append(payload)
        return SimpleNamespace(content=content, embed=embed)


class FakeMember:
    def __init__(self, member_id: int, display_name: str | None = None):
        self.id = member_id
        self.display_name = display_name or f"user-{member_id}"
        self.mention = f"<@{member_id}>"


class FakeMessage:
    _next_id = 10

    def __init__(self, *, mentions=None):
        self.id = FakeMessage._next_id
        FakeMessage._next_id += 1
        self.mentions = list(mentions or [])
        self.delete_calls = 0
        self.reference = None

    async def delete(self):
        self.delete_calls += 1


class FakeGuild:
    def __init__(self, guild_id: int, *, channels=None, name: str | None = None):
        self.id = guild_id
        self.channels = {ch.id: ch for ch in channels or []}
        self.emojis = []
        self.name = name or f"Guild-{guild_id}"

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)

    async def fetch_member(self, member_id: int):
        raise LookupError(member_id)


class FakeBot:
    def __init__(self, *, channels=None):
        self.channels = {ch.id: ch for ch in channels or []}

    def get_channel(self, channel_id: int):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        channel = self.channels.get(channel_id)
        if channel is None:
            raise LookupError(channel_id)
        return channel


class FakeContext:
    def __init__(self, *, bot, guild, channel, author, message):
        self.bot = bot
        self.guild = guild
        self.channel = channel
        self.author = author
        self.message = message
        self.replies = []

    async def reply(self, content=None):
        self.replies.append(content)
        return await self.channel.send(content=content)

    async def send(self, content=None):
        return await self.channel.send(content=content)


@pytest.fixture(autouse=True)
def stub_logs(monkeypatch):
    monkeypatch.setattr(welcome_module.rt, "send_log_message", AsyncMock())
    delete_mock = AsyncMock()
    monkeypatch.setattr(welcome_module, "_delete_message", delete_mock)
    return delete_mock


def _template_rows():
    return [
        {
            "TAG": "C1C",
            "TITLE": "Default {CLAN} Title",
            "BODY": "Default body for {CLAN}",
            "FOOTER": "Default footer",
            "GENERAL_NOTICE": "General {MENTION} {CLAN}",
        },
        {
            "TAG": "C1CM",
            "TITLE": "Welcome {MENTION}",
            "BODY": "Body line with {CLANLEAD}",
            "FOOTER": "Footer {DEPUTIES}",
            "TARGET_CHANNEL_ID": "123",
            "CREST_URL": "https://example.com/crest.png",
            "PING_USER": "Y",
            "ACTIVE": "Y",
            "CLAN": "C1C Match",
            "CLANLEAD": "Lead Name",
            "DEPUTIES": "Deputy One, Deputy Two",
        },
    ]


def test_welcome_happy_path_posts_embed(monkeypatch, stub_logs):
    async def scenario():
        clan_channel = FakeChannel(123)
        general_channel = FakeChannel(999)
        bot = FakeBot(channels=[clan_channel, general_channel])
        guild = FakeGuild(42, channels=[clan_channel, general_channel])
        author = FakeMember(7, "Coordinator")
        recruit = FakeMember(8, "New Recruit")
        message = FakeMessage(mentions=[recruit])
        ctx = FakeContext(bot=bot, guild=guild, channel=FakeChannel(555), author=author, message=message)

        template_loader = AsyncMock(return_value=_template_rows())
        monkeypatch.setattr(
            welcome_module.sheets,
            "get_cached_welcome_templates",
            template_loader,
        )
        monkeypatch.setattr(welcome_module, "get_welcome_general_channel_id", lambda: 999)

        bridge = WelcomeBridge(bot)
        await bridge.welcome.callback(bridge, ctx, "C1CM")  # type: ignore[misc]

        assert len(clan_channel.sent) == 1
        sent = clan_channel.sent[0]
        assert sent["content"] == recruit.mention
        embed = sent["embed"]
        assert embed.title == "Welcome <@8>"
        assert embed.thumbnail.url == "https://example.com/crest.png"
        assert embed.footer.text == "Footer Deputy One, Deputy Two"
        assert "Lead Name" in embed.description

        assert len(general_channel.sent) == 1
        assert recruit.mention in general_channel.sent[0]["content"]
        template_loader.assert_awaited_once_with()
        await asyncio.sleep(0)

    asyncio.run(scenario())
    stub_logs.assert_called_once()


def test_welcome_does_not_call_sync_template_loader(monkeypatch):
    async def scenario():
        clan_channel = FakeChannel(123)
        bot = FakeBot(channels=[clan_channel])
        guild = FakeGuild(42, channels=[clan_channel])
        ctx = FakeContext(
            bot=bot,
            guild=guild,
            channel=clan_channel,
            author=FakeMember(7),
            message=FakeMessage(),
        )
        async_loader = AsyncMock(return_value=_template_rows())
        sync_loader = Mock(side_effect=AssertionError("sync loader called in event loop"))
        monkeypatch.setattr(welcome_module.sheets, "get_cached_welcome_templates", async_loader)
        monkeypatch.setattr(recruitment_sheets, "get_cached_welcome_templates", sync_loader)
        monkeypatch.setattr(welcome_module, "get_welcome_general_channel_id", lambda: None)

        await welcome_module.WelcomeCommandService(bot).post_welcome(ctx, "C1CM")

        async_loader.assert_awaited_once_with()
        sync_loader.assert_not_called()

    asyncio.run(scenario())


def test_welcome_template_load_failure_keeps_existing_response(monkeypatch):
    async def scenario():
        channel = FakeChannel(123)
        bot = FakeBot(channels=[channel])
        ctx = FakeContext(
            bot=bot,
            guild=FakeGuild(42, channels=[channel]),
            channel=channel,
            author=FakeMember(7),
            message=FakeMessage(),
        )
        loader = AsyncMock(side_effect=RuntimeError("sheet unavailable"))
        monkeypatch.setattr(welcome_module.sheets, "get_cached_welcome_templates", loader)

        await welcome_module.WelcomeCommandService(bot).post_welcome(ctx, "C1CM")

        loader.assert_awaited_once_with()
        assert ctx.replies == [
            "⚠️ Failed to load welcome templates. Try again after the next refresh."
        ]

    asyncio.run(scenario())


def test_default_merge_uses_c1c_row(monkeypatch):
    async def scenario():
        rows = _template_rows()
        rows[1]["BODY"] = ""
        rows[1]["FOOTER"] = ""
        clan_channel = FakeChannel(123)
        bot = FakeBot(channels=[clan_channel])
        guild = FakeGuild(1, channels=[clan_channel])
        author = FakeMember(5)
        message = FakeMessage()
        ctx = FakeContext(bot=bot, guild=guild, channel=clan_channel, author=author, message=message)

        monkeypatch.setattr(
            welcome_module.sheets,
            "get_cached_welcome_templates",
            AsyncMock(return_value=rows),
        )
        monkeypatch.setattr(welcome_module, "get_welcome_general_channel_id", lambda: None)

        bridge = WelcomeBridge(bot)
        await bridge.welcome.callback(bridge, ctx, "C1CM")  # type: ignore[misc]

        sent = clan_channel.sent[0]
        embed = sent["embed"]
        assert embed.description == "Default body for C1C Match"
        assert embed.footer.text == "Default footer"

    asyncio.run(scenario())


def test_ping_respects_toggle(monkeypatch):
    async def scenario():
        rows = _template_rows()
        rows[1]["PING_USER"] = "Y"
        clan_channel = FakeChannel(123)
        bot = FakeBot(channels=[clan_channel])
        guild = FakeGuild(2, channels=[clan_channel])
        author = FakeMember(11)
        recruit = FakeMember(33)
        message = FakeMessage(mentions=[recruit])
        ctx = FakeContext(bot=bot, guild=guild, channel=clan_channel, author=author, message=message)

        monkeypatch.setattr(
            welcome_module.sheets,
            "get_cached_welcome_templates",
            AsyncMock(return_value=rows),
        )
        monkeypatch.setattr(welcome_module, "get_welcome_general_channel_id", lambda: None)

        bridge = WelcomeBridge(bot)
        await bridge.welcome.callback(bridge, ctx, "C1CM")  # type: ignore[misc]

        sent = clan_channel.sent[0]
        assert sent["content"] == recruit.mention

        rows[1]["PING_USER"] = "N"
        clan_channel.sent.clear()
        await bridge.welcome.callback(bridge, ctx, "C1CM")  # type: ignore[misc]
        sent = clan_channel.sent[0]
        assert sent["content"] is None

    asyncio.run(scenario())


def test_target_channel_routing(monkeypatch):
    async def scenario():
        rows = _template_rows()
        rows[1]["TARGET_CHANNEL_ID"] = "123"
        clan_channel = FakeChannel(123)
        fallback_channel = FakeChannel(555)
        bot = FakeBot(channels=[clan_channel])
        guild = FakeGuild(3, channels=[clan_channel])
        author = FakeMember(42)
        message = FakeMessage()
        ctx = FakeContext(bot=bot, guild=guild, channel=fallback_channel, author=author, message=message)

        monkeypatch.setattr(
            welcome_module.sheets,
            "get_cached_welcome_templates",
            AsyncMock(return_value=rows),
        )
        monkeypatch.setattr(welcome_module, "get_welcome_general_channel_id", lambda: None)

        bridge = WelcomeBridge(bot)
        await bridge.welcome.callback(bridge, ctx, "C1CM")  # type: ignore[misc]

        assert len(clan_channel.sent) == 1
        assert not fallback_channel.sent

    asyncio.run(scenario())


def test_missing_or_inactive_rows(monkeypatch):
    async def scenario():
        rows = _template_rows()
        rows[1]["ACTIVE"] = "N"
        clan_channel = FakeChannel(123)
        bot = FakeBot(channels=[clan_channel])
        guild = FakeGuild(9, channels=[clan_channel])
        author = FakeMember(55)
        message = FakeMessage()
        ctx = FakeContext(bot=bot, guild=guild, channel=clan_channel, author=author, message=message)

        monkeypatch.setattr(
            welcome_module.sheets,
            "get_cached_welcome_templates",
            AsyncMock(return_value=rows),
        )
        monkeypatch.setattr(welcome_module, "get_welcome_general_channel_id", lambda: None)

        bridge = WelcomeBridge(bot)
        await bridge.welcome.callback(bridge, ctx, "C1CM")  # type: ignore[misc]
        assert "inactive" in ctx.replies[0]

        ctx.replies.clear()
        clan_channel.sent.clear()
        await bridge.welcome.callback(bridge, ctx, "MISSING")  # type: ignore[misc]
        assert ctx.replies[0] == "I can't find a configured welcome for **MISSING**. Add it in the sheet."

    asyncio.run(scenario())


def test_welcome_refresh(monkeypatch):
    async def scenario():
        bot = FakeBot()
        guild = FakeGuild(7)
        author = FakeMember(101)
        message = FakeMessage()
        ctx = FakeContext(bot=bot, guild=guild, channel=FakeChannel(1), author=author, message=message)

        refresh_result = SimpleNamespace(ok=True, error=None)
        monkeypatch.setattr(welcome_module.cache_telemetry, "refresh_now", AsyncMock(return_value=refresh_result))

        bridge = WelcomeBridge(bot)
        await bridge.welcome_refresh.callback(bridge, ctx)  # type: ignore[misc]
        assert ctx.replies[0] == "Welcome templates reloaded. ✅"

        refresh_result.ok = False
        refresh_result.error = "boom"
        ctx.replies.clear()
        await bridge.welcome_refresh.callback(bridge, ctx)  # type: ignore[misc]
        assert ctx.replies[0] == "Reload failed: `boom`"

    asyncio.run(scenario())
