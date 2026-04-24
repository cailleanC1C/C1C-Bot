from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

from modules.community.shard_tracker.cog import (
    ShardTracker,
    _SKIP_ALREADY_SENT,
    _SKIP_NOT_IN_TIME_WINDOW,
)
from modules.community.shard_tracker.data import ShardClanRow


class _Destination:
    def __init__(self, guild=None) -> None:
        self.guild = guild
        self.sent = 0
        self.last_kwargs = None

    async def send(self, **kwargs):
        self.sent += 1
        self.last_kwargs = kwargs
        return SimpleNamespace(id=555)


def _clan(*, reminder_time_utc: str = "08:00", emoji_name_or_id: str = "") -> ShardClanRow:
    return ShardClanRow(
        clan_key="alpha",
        enabled=True,
        share_channel_id=123,
        share_thread_id=None,
        reminder_enabled=True,
        opt_in_role_id=456,
        reminder_day="friday",
        reminder_time_utc=reminder_time_utc,
        title="Title",
        body="Body",
        footer="Footer",
        color_hex="#112233",
        emoji_name_or_id=emoji_name_or_id,
    )


class _FakeEmoji:
    def __init__(self, *, emoji_id: int, name: str, animated: bool = False) -> None:
        self.id = emoji_id
        self.name = name
        self.animated = animated

    async def read(self) -> bytes:
        return b"emoji-bytes"


class _FakeGuild:
    def __init__(self, emojis: list[_FakeEmoji]) -> None:
        self.emojis = emojis
        self._by_id = {emoji.id: emoji for emoji in emojis}

    def get_emoji(self, emoji_id: int):
        return self._by_id.get(emoji_id)


def test_weekly_reminder_skips_outside_window() -> None:
    async def runner() -> None:
        bot = SimpleNamespace(get_channel=lambda _id: _Destination())
        cog = ShardTracker(bot)
        cog._notify_admins = AsyncMock()
        cog._resolve_share_destination = lambda _clan: (_Destination(), None)
        cog.store.get_clans = AsyncMock(return_value=[_clan(reminder_time_utc="01:00")])
        cog.store.get_sent_weekly_reminder_keys = AsyncMock(return_value=set())
        cog.store.mark_weekly_reminder_sent = AsyncMock()

        stats = await cog.process_weekly_clan_reminders(
            now=datetime(2026, 4, 24, 8, 0, tzinfo=timezone.utc),
            source="test",
        )

        assert stats.rows_loaded == 1
        assert stats.eligible == 0
        assert stats.sent == 0
        assert stats.skip_reasons.get(_SKIP_NOT_IN_TIME_WINDOW) == 1

    asyncio.run(runner())


def test_weekly_reminder_blank_emoji_sends_without_file() -> None:
    async def runner() -> None:
        destination = _Destination()
        bot = SimpleNamespace(get_channel=lambda _id: destination)
        cog = ShardTracker(bot)
        cog._notify_admins = AsyncMock()
        cog._resolve_share_destination = lambda _clan: (destination, None)
        cog.store.get_clans = AsyncMock(return_value=[_clan(reminder_time_utc="08:00")])
        cog.store.get_sent_weekly_reminder_keys = AsyncMock(return_value=set())
        cog.store.mark_weekly_reminder_sent = AsyncMock()

        stats = await cog.process_weekly_clan_reminders(
            now=datetime(2026, 4, 24, 8, 0, tzinfo=timezone.utc),
            force_window=True,
            source="test",
        )

        assert stats.sent == 1
        assert destination.last_kwargs is not None
        assert "files" not in destination.last_kwargs

    asyncio.run(runner())


def test_weekly_reminder_invalid_emoji_warns_and_sends_without_file(caplog) -> None:
    async def runner() -> None:
        destination = _Destination()
        bot = SimpleNamespace(get_channel=lambda _id: destination, get_emoji=lambda _id: None, emojis=[])
        cog = ShardTracker(bot)
        cog._notify_admins = AsyncMock()
        cog._resolve_share_destination = lambda _clan: (destination, None)
        clan = _clan(reminder_time_utc="08:00", emoji_name_or_id="missing_emoji")
        cog.store.get_clans = AsyncMock(return_value=[clan])
        cog.store.get_sent_weekly_reminder_keys = AsyncMock(return_value=set())
        cog.store.mark_weekly_reminder_sent = AsyncMock()

        with caplog.at_level(logging.WARNING, logger="c1c.shards.cog"):
            stats = await cog.process_weekly_clan_reminders(
                now=datetime(2026, 4, 24, 8, 0, tzinfo=timezone.utc),
                force_window=True,
                source="test",
            )

        assert stats.sent == 1
        assert destination.last_kwargs is not None
        assert "files" not in destination.last_kwargs
        assert "shard reminder emoji unresolved" in caplog.text
        assert any(getattr(record, "clan_key", None) == "alpha" for record in caplog.records)

    asyncio.run(runner())


def test_weekly_reminder_valid_emoji_adds_file_attachment() -> None:
    async def runner() -> None:
        emoji = _FakeEmoji(emoji_id=123456789012345678, name="shardanim", animated=True)
        guild = _FakeGuild([emoji])
        destination = _Destination(guild=guild)
        destination.guild = guild
        bot = SimpleNamespace(
            get_channel=lambda _id: destination,
            get_emoji=lambda emoji_id: guild.get_emoji(emoji_id),
            emojis=[emoji],
        )
        cog = ShardTracker(bot)
        cog._notify_admins = AsyncMock()
        cog._resolve_share_destination = lambda _clan: (destination, None)
        clan = _clan(reminder_time_utc="08:00", emoji_name_or_id="<a:shardanim:123456789012345678>")
        cog.store.get_clans = AsyncMock(return_value=[clan])
        cog.store.get_sent_weekly_reminder_keys = AsyncMock(return_value=set())
        cog.store.mark_weekly_reminder_sent = AsyncMock()

        stats = await cog.process_weekly_clan_reminders(
            now=datetime(2026, 4, 24, 8, 0, tzinfo=timezone.utc),
            force_window=True,
            source="test",
        )

        assert stats.sent == 1
        assert destination.last_kwargs is not None
        files = destination.last_kwargs.get("files")
        assert files and len(files) == 1
        assert files[0].filename == "shard_reminder_emoji.gif"

    asyncio.run(runner())


def test_weekly_reminder_force_window_respects_dedupe_until_force_send() -> None:
    async def runner() -> None:
        destination = _Destination()
        bot = SimpleNamespace(get_channel=lambda _id: destination)
        cog = ShardTracker(bot)
        cog._notify_admins = AsyncMock()
        cog._resolve_share_destination = lambda _clan: (destination, None)
        cog.store.get_clans = AsyncMock(return_value=[_clan(reminder_time_utc="01:00")])
        cog.store.get_sent_weekly_reminder_keys = AsyncMock(return_value={"2026-04-24"})
        cog.store.mark_weekly_reminder_sent = AsyncMock()

        stats_dedupe = await cog.process_weekly_clan_reminders(
            now=datetime(2026, 4, 24, 8, 0, tzinfo=timezone.utc),
            force_window=True,
            source="test",
        )
        stats_force_send = await cog.process_weekly_clan_reminders(
            now=datetime(2026, 4, 24, 8, 0, tzinfo=timezone.utc),
            force_window=True,
            force_send=True,
            source="test",
        )

        assert stats_dedupe.skip_reasons.get(_SKIP_ALREADY_SENT) == 1
        assert stats_dedupe.sent == 0
        assert stats_force_send.sent == 1
        assert destination.sent == 1

    asyncio.run(runner())
