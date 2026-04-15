import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import modules.community.fusion.cog as fusion_cog_module
from modules.community.fusion.cog import FusionCog
from shared.sheets import fusion as fusion_sheets


def _fusion_row(
    *,
    announcement_channel_id: int | None = 123,
    announcement_message_id: int | None = 456,
    status: str = "published",
) -> fusion_sheets.FusionRow:
    return fusion_sheets.FusionRow(
        fusion_id="f-1",
        fusion_name="Mavara",
        champion="Mavara",
        fusion_type="traditional",
        fusion_structure="",
        reward_type="fragments",
        needed=400,
        available=450,
        start_at_utc=dt.datetime(2026, 4, 8, tzinfo=dt.timezone.utc),
        end_at_utc=dt.datetime(2026, 4, 22, tzinfo=dt.timezone.utc),
        announcement_channel_id=announcement_channel_id,
        opt_in_role_id=None,
        announcement_message_id=announcement_message_id,
        published_at=None,
        status=status,
    )


class FakeMessageable(discord.abc.Messageable):
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self.fetch_message = AsyncMock()
        self.send = AsyncMock()

    async def _get_channel(self):
        return self


def test_fusion_command_returns_jump_url(monkeypatch):
    async def _run() -> None:
        message = SimpleNamespace(jump_url="https://discord.com/channels/1/123/456")
        channel = FakeMessageable(123)
        channel.fetch_message = AsyncMock(return_value=message)
        bot = SimpleNamespace(
            get_channel=lambda _channel_id: channel,
            fetch_channel=AsyncMock(return_value=channel),
        )
        cog = FusionCog(bot)
        ctx = SimpleNamespace(reply=AsyncMock())

        async def _fake_get_publishable():
            return _fusion_row()

        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", _fake_get_publishable)

        await cog.fusion.callback(cog, ctx)

        ctx.reply.assert_awaited_once_with(message.jump_url, mention_author=False)
        channel.fetch_message.assert_awaited_once_with(456)

    asyncio.run(_run())


def test_fusion_command_handles_missing_announcement_message(monkeypatch):
    async def _run() -> None:
        channel = FakeMessageable(123)
        channel.fetch_message = AsyncMock(side_effect=RuntimeError("gone"))
        bot = SimpleNamespace(
            get_channel=lambda _channel_id: channel,
            fetch_channel=AsyncMock(return_value=channel),
        )
        cog = FusionCog(bot)
        ctx = SimpleNamespace(reply=AsyncMock())

        async def _fake_get_publishable():
            return _fusion_row()

        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", _fake_get_publishable)

        await cog.fusion.callback(cog, ctx)

        ctx.reply.assert_awaited_once_with(
            "Fusion is published but the announcement message is unavailable.",
            mention_author=False,
        )

    asyncio.run(_run())


def test_fusion_publish_persists_announcement_channel_id(monkeypatch):
    async def _run() -> None:
        channel = FakeMessageable(123)
        channel.send = AsyncMock(return_value=SimpleNamespace(id=999))
        bot = SimpleNamespace(get_channel=lambda _channel_id: channel, fetch_channel=AsyncMock())
        cog = FusionCog(bot)
        ctx = SimpleNamespace(reply=AsyncMock())

        async def _fake_get_publishable():
            return _fusion_row(announcement_channel_id=123, announcement_message_id=None, status="draft")

        update = AsyncMock()
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", _fake_get_publishable)
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[]))
        monkeypatch.setattr(fusion_cog_module, "build_fusion_announcement_embed", lambda *_args: object())
        monkeypatch.setattr(fusion_sheets, "update_fusion_publication", update)

        await cog.fusion_publish.callback(cog, ctx)

        assert update.await_count == 1
        _, kwargs = update.await_args
        assert kwargs["announcement_message_id"] == 999
        assert kwargs["announcement_channel_id"] == 123

    asyncio.run(_run())
