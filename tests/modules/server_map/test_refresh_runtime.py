import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from modules.ops import server_map


class _StubCategory:
    def __init__(self, name: str, position: int, category_id: int) -> None:
        self.name = name
        self.position = position
        self.id = category_id
        self.type = discord.ChannelType.category


class _StubChannel:
    def __init__(
        self,
        name: str,
        position: int,
        channel_id: int,
        channel_type: discord.ChannelType,
        category: _StubCategory | None,
    ) -> None:
        self.name = name
        self.position = position
        self.id = channel_id
        self.type = channel_type
        self.category_id = getattr(category, "id", None)


class _FakeMessage:
    _counter = 6000

    def __init__(self, content: str) -> None:
        type(self)._counter += 1
        self.id = type(self)._counter
        self.content = content
        self.pinned = False

    async def edit(self, content: str) -> None:
        self.content = content

    async def delete(self) -> None:  # pragma: no cover - defensive stub
        return None

    async def pin(self) -> None:
        self.pinned = True


class _FakeTextChannel:
    def __init__(self, guild: object, channel_id: int) -> None:
        self.guild = guild
        self.id = channel_id
        self.sent_messages: list[_FakeMessage] = []

    async def send(self, body: str) -> _FakeMessage:
        message = _FakeMessage(body)
        self.sent_messages.append(message)
        return message


def test_refresh_server_map_skips_when_feature_disabled(monkeypatch):
    async def _run() -> None:
        bot = AsyncMock()
        bot.wait_until_ready = AsyncMock()

        log_messages: list[str] = []

        async def fake_log(message: str) -> None:
            log_messages.append(message)

        monkeypatch.setattr(server_map.feature_flags, "is_enabled", lambda key: False)

        def _should_not_call() -> None:  # pragma: no cover - defensive assertion
            raise AssertionError("server map config should not be read when disabled")

        monkeypatch.setattr(server_map, "get_server_map_channel_id", _should_not_call)
        monkeypatch.setattr(server_map, "get_server_map_refresh_days", _should_not_call)
        monkeypatch.setattr(server_map.runtime_helpers, "send_log_message", fake_log)

        result = await server_map.refresh_server_map(bot, force=True, actor="scheduler")

        assert result.status == "disabled"
        assert result.reason == "feature_disabled"
        assert log_messages == ["📘 Server map — skipped • reason=feature_disabled"]
        bot.wait_until_ready.assert_awaited()

    asyncio.run(_run())


def test_refresh_server_map_requires_channel_when_enabled(monkeypatch):
    async def _run() -> None:
        bot = AsyncMock()
        bot.wait_until_ready = AsyncMock()

        log_messages: list[str] = []

        async def fake_log(message: str) -> None:
            log_messages.append(message)

        monkeypatch.setattr(server_map.feature_flags, "is_enabled", lambda key: True)
        monkeypatch.setattr(server_map, "get_server_map_channel_id", lambda: None)
        monkeypatch.setattr(server_map.runtime_helpers, "send_log_message", fake_log)

        result = await server_map.refresh_server_map(bot, force=True, actor="scheduler")

        assert result.status == "error"
        assert result.reason == "missing_channel_id"
        assert log_messages == ["❌ Server map — error • reason=missing_channel_id"]
        bot.wait_until_ready.assert_awaited()

    asyncio.run(_run())


def test_refresh_server_map_applies_category_blacklist(monkeypatch):
    async def _run() -> None:
        hidden = _StubCategory("STAFF", 1, 4001)
        visible = _StubCategory("PUBLIC", 2, 5001)
        hidden_channel = _StubChannel(
            "staff-chat", 1, 6001, discord.ChannelType.text, hidden
        )
        public_channel = _StubChannel(
            "town-square", 2, 6002, discord.ChannelType.text, visible
        )
        lobby = _StubChannel("lobby", 3, 6003, discord.ChannelType.text, None)
        guild = SimpleNamespace(
            name="C1C",
            categories=[hidden, visible],
            channels=[hidden, visible, hidden_channel, public_channel, lobby],
        )
        guild.get_channel = lambda cid: next(
            (channel for channel in guild.channels if getattr(channel, "id", None) == cid),
            None,
        )

        fake_channel = _FakeTextChannel(guild, channel_id=7001)
        bot = SimpleNamespace()
        bot.wait_until_ready = AsyncMock()
        bot.get_channel = lambda channel_id: fake_channel if channel_id == fake_channel.id else None
        bot.fetch_channel = AsyncMock()

        log_messages: list[str] = []

        async def fake_log(message: str) -> None:
            log_messages.append(message)

        async def fake_fetch_state() -> dict[str, str]:
            return {}

        async def fake_update_state(entries: dict[str, str]) -> None:
            return None

        monkeypatch.setattr(server_map.feature_flags, "is_enabled", lambda key: True)
        monkeypatch.setattr(server_map, "get_server_map_channel_id", lambda: fake_channel.id)
        monkeypatch.setattr(server_map, "get_server_map_refresh_days", lambda: 30)
        monkeypatch.setattr(server_map.server_map_state, "fetch_state", fake_fetch_state)
        monkeypatch.setattr(server_map.server_map_state, "update_state", fake_update_state)
        monkeypatch.setattr(server_map.runtime_helpers, "send_log_message", fake_log)
        monkeypatch.setattr(server_map.discord, "TextChannel", _FakeTextChannel)
        monkeypatch.setenv("SERVER_MAP_CATEGORY_BLACKLIST", str(hidden.id))
        monkeypatch.setenv("SERVER_MAP_CHANNEL_BLACKLIST", "")

        result = await server_map.refresh_server_map(bot, force=True, actor="manual")

        assert result.status == "ok"
        assert fake_channel.sent_messages
        body = fake_channel.sent_messages[0].content
        assert "## STAFF" not in body
        assert "<#6001>" not in body
        assert "## PUBLIC" in body
        assert "<#6002>" in body
        assert log_messages[-1].startswith(
            "✅ Server map updated — cmd=servermap"
        )
        assert "target=<#7001>" in log_messages[-1]
        bot.wait_until_ready.assert_awaited()

    asyncio.run(_run())


def test_refresh_server_map_filters_blacklisted_channels_and_logs_config(monkeypatch):
    async def _run() -> None:
        visible = _StubCategory("PUBLIC", 1, 5101)
        other = _StubCategory("OTHER", 2, 5201)
        plaza = _StubChannel("plaza", 1, 6101, discord.ChannelType.text, visible)
        market = _StubChannel("market", 2, 6102, discord.ChannelType.text, visible)
        lounge = _StubChannel("lounge", 3, 6103, discord.ChannelType.text, None)
        guild = SimpleNamespace(
            name="C1C",
            categories=[visible, other],
            channels=[visible, other, plaza, market, lounge],
        )
        guild.get_channel = lambda cid: next(
            (channel for channel in guild.channels if getattr(channel, "id", None) == cid),
            None,
        )

        fake_channel = _FakeTextChannel(guild, channel_id=7101)
        bot = SimpleNamespace()
        bot.wait_until_ready = AsyncMock()
        bot.get_channel = lambda channel_id: fake_channel if channel_id == fake_channel.id else None
        bot.fetch_channel = AsyncMock()

        log_messages: list[str] = []

        async def fake_log(message: str) -> None:
            log_messages.append(message)

        async def fake_fetch_state() -> dict[str, str]:
            return {}

        async def fake_update_state(entries: dict[str, str]) -> None:
            return None

        monkeypatch.setattr(server_map.feature_flags, "is_enabled", lambda key: True)
        monkeypatch.setattr(server_map, "get_server_map_channel_id", lambda: fake_channel.id)
        monkeypatch.setattr(server_map, "get_server_map_refresh_days", lambda: 30)
        monkeypatch.setattr(server_map.server_map_state, "fetch_state", fake_fetch_state)
        monkeypatch.setattr(server_map.server_map_state, "update_state", fake_update_state)
        monkeypatch.setattr(server_map.runtime_helpers, "send_log_message", fake_log)
        monkeypatch.setattr(server_map.discord, "TextChannel", _FakeTextChannel)
        monkeypatch.setenv("SERVER_MAP_CATEGORY_BLACKLIST", "999999")
        monkeypatch.setenv("SERVER_MAP_CHANNEL_BLACKLIST", f"{market.id}, {lounge.id}")

        result = await server_map.refresh_server_map(bot, force=True, actor="manual")

        assert result.status == "ok"
        assert fake_channel.sent_messages
        body = fake_channel.sent_messages[0].content
        assert "<#6102>" not in body
        assert "<#6103>" not in body
        assert "<#6101>" in body
        assert "## PUBLIC" in body
        assert log_messages[-1].startswith(
            "✅ Server map updated — cmd=servermap"
        )
        assert "target=<#7101>" in log_messages[-1]
        bot.wait_until_ready.assert_awaited()

    asyncio.run(_run())
