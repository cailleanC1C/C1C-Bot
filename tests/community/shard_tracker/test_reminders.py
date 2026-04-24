from __future__ import annotations

import asyncio
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
    def __init__(self) -> None:
        self.guild = None
        self.sent = 0

    async def send(self, **kwargs):
        self.sent += 1
        return SimpleNamespace(id=555)


def _clan(*, reminder_time_utc: str = "08:00") -> ShardClanRow:
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
        emoji_name_or_id="",
    )


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
