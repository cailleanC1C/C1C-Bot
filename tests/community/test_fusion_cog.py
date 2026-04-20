import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import modules.community.fusion.cog as fusion_cog_module
from modules.community.fusion import logs as fusion_logs
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
        champion_image_url="",
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
        last_announcement_refresh_at=None,
        last_announcement_status_hash="",
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

        ctx.reply.assert_awaited_once_with(
            "🔗 Fusion’s up. Don’t get lost:\nhttps://discord.com/channels/1/123/456",
            mention_author=False,
        )
        channel.fetch_message.assert_awaited_once_with(456)
        channel.send.assert_not_awaited()

    asyncio.run(_run())


def test_fusion_command_handles_no_fusion(monkeypatch):
    async def _run() -> None:
        bot = SimpleNamespace(get_channel=lambda _channel_id: None, fetch_channel=AsyncMock())
        cog = FusionCog(bot)
        ctx = SimpleNamespace(reply=AsyncMock())

        async def _fake_get_publishable():
            return None

        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", _fake_get_publishable)

        await cog.fusion.callback(cog, ctx)

        ctx.reply.assert_awaited_once_with(
            "No fusion running. Enjoy the peace while it lasts.",
            mention_author=False,
        )

    asyncio.run(_run())


def test_fusion_command_recreates_missing_announcement(monkeypatch):
    async def _run() -> None:
        message = SimpleNamespace(id=999, jump_url="https://discord.com/channels/1/123/999")
        channel = FakeMessageable(123)
        channel.fetch_message = AsyncMock(side_effect=RuntimeError("gone"))
        channel.send = AsyncMock(return_value=message)
        bot = SimpleNamespace(
            get_channel=lambda _channel_id: channel,
            fetch_channel=AsyncMock(return_value=channel),
        )
        cog = FusionCog(bot)
        ctx = SimpleNamespace(reply=AsyncMock())

        async def _fake_get_publishable():
            return _fusion_row()

        update = AsyncMock()
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", _fake_get_publishable)
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[]))
        monkeypatch.setattr(fusion_cog_module, "build_fusion_announcement_embed", lambda *_args: object())
        monkeypatch.setattr(fusion_sheets, "update_fusion_publication", update)

        await cog.fusion.callback(cog, ctx)

        channel.send.assert_awaited_once()
        assert update.await_count == 1
        _, kwargs = update.await_args
        assert kwargs["announcement_message_id"] == 999
        assert kwargs["announcement_channel_id"] == 123
        ctx.reply.assert_awaited_once_with(
            "🔗 Fusion’s up. Don’t get lost:\nhttps://discord.com/channels/1/123/999",
            mention_author=False,
        )

    asyncio.run(_run())


def test_fusion_command_recreates_when_message_metadata_is_incomplete(monkeypatch):
    async def _run() -> None:
        message = SimpleNamespace(id=1001, jump_url="https://discord.com/channels/1/123/1001")
        channel = FakeMessageable(123)
        channel.send = AsyncMock(return_value=message)
        bot = SimpleNamespace(
            get_channel=lambda _channel_id: channel,
            fetch_channel=AsyncMock(return_value=channel),
        )
        cog = FusionCog(bot)
        ctx = SimpleNamespace(reply=AsyncMock())

        async def _fake_get_publishable():
            return _fusion_row(announcement_channel_id=123, announcement_message_id=None)

        update = AsyncMock()
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", _fake_get_publishable)
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[]))
        monkeypatch.setattr(fusion_cog_module, "build_fusion_announcement_embed", lambda *_args: object())
        monkeypatch.setattr(fusion_sheets, "update_fusion_publication", update)

        await cog.fusion.callback(cog, ctx)

        channel.fetch_message.assert_not_awaited()
        channel.send.assert_awaited_once()
        assert update.await_count == 1
        ctx.reply.assert_awaited_once_with(
            "🔗 Fusion’s up. Don’t get lost:\nhttps://discord.com/channels/1/123/1001",
            mention_author=False,
        )

    asyncio.run(_run())


def test_fusion_command_does_not_recreate_existing_announcement(monkeypatch):
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
        monkeypatch.setattr(fusion_sheets, "update_fusion_publication", AsyncMock())

        await cog.fusion.callback(cog, ctx)

        channel.send.assert_not_awaited()
        fusion_sheets.update_fusion_publication.assert_not_awaited()

    asyncio.run(_run())


def test_fusion_command_uses_generic_message_when_data_load_fails(monkeypatch):
    async def _run() -> None:
        bot = SimpleNamespace(get_channel=lambda _channel_id: None, fetch_channel=AsyncMock())
        cog = FusionCog(bot)
        ctx = SimpleNamespace(reply=AsyncMock())

        async def _fake_get_publishable():
            raise RuntimeError("sheets offline")

        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", _fake_get_publishable)

        await cog.fusion.callback(cog, ctx)

        ctx.reply.assert_awaited_once_with(
            "Couldn’t check the fusion right now. Try again in a moment.",
            mention_author=False,
        )

    asyncio.run(_run())


def test_fusion_command_falls_back_to_emergency_embed_when_announcement_unavailable(monkeypatch):
    async def _run() -> None:
        emergency_embed = object()
        bot = SimpleNamespace(get_channel=lambda _channel_id: None, fetch_channel=AsyncMock())
        cog = FusionCog(bot)
        ctx = SimpleNamespace(reply=AsyncMock())

        async def _fake_get_publishable():
            return _fusion_row()

        ensure = AsyncMock(side_effect=RuntimeError("discord outage"))
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", _fake_get_publishable)
        monkeypatch.setattr(cog, "_ensure_fusion_announcement", ensure)
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[]))
        monkeypatch.setattr(fusion_cog_module, "build_fusion_announcement_embed", lambda *_args: emergency_embed)

        await cog.fusion.callback(cog, ctx)

        ctx.reply.assert_awaited_once_with(
            embed=emergency_embed,
            mention_author=False,
        )

    asyncio.run(_run())


def test_fusion_command_uses_generic_message_when_emergency_embed_fails(monkeypatch):
    async def _run() -> None:
        bot = SimpleNamespace(get_channel=lambda _channel_id: None, fetch_channel=AsyncMock())
        cog = FusionCog(bot)
        ctx = SimpleNamespace(reply=AsyncMock())

        async def _fake_get_publishable():
            return _fusion_row()

        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", _fake_get_publishable)
        monkeypatch.setattr(cog, "_ensure_fusion_announcement", AsyncMock(return_value=None))
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(side_effect=RuntimeError("sheet fail")))

        await cog.fusion.callback(cog, ctx)

        ctx.reply.assert_awaited_once_with(
            "Couldn’t check the fusion right now. Try again in a moment.",
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


def test_fusion_publish_blocks_duplicate_when_existing_message_resolves(monkeypatch):
    async def _run() -> None:
        channel = FakeMessageable(123)
        channel.fetch_message = AsyncMock(return_value=SimpleNamespace(id=456))
        bot = SimpleNamespace(get_channel=lambda _channel_id: channel, fetch_channel=AsyncMock())
        cog = FusionCog(bot)
        ctx = SimpleNamespace(reply=AsyncMock())

        async def _fake_get_publishable():
            return _fusion_row(announcement_channel_id=123, announcement_message_id=456, status="published")

        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", _fake_get_publishable)

        await cog.fusion_publish.callback(cog, ctx)

        channel.send.assert_not_awaited()
        ctx.reply.assert_awaited_once_with(
            "This fusion already has an announcement post. Clear the message id or use a future republish flow.",
            mention_author=False,
        )

    asyncio.run(_run())


def test_fusion_publish_recreates_when_existing_metadata_is_stale(monkeypatch):
    async def _run() -> None:
        channel = FakeMessageable(123)
        channel.fetch_message = AsyncMock(side_effect=discord.NotFound(response=SimpleNamespace(status=404, reason="Not Found"), message="missing"))
        channel.send = AsyncMock(return_value=SimpleNamespace(id=1002))
        bot = SimpleNamespace(get_channel=lambda _channel_id: channel, fetch_channel=AsyncMock())
        cog = FusionCog(bot)
        ctx = SimpleNamespace(reply=AsyncMock())

        async def _fake_get_publishable():
            return _fusion_row(announcement_channel_id=123, announcement_message_id=456, status="published")

        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", _fake_get_publishable)
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[]))
        monkeypatch.setattr(fusion_cog_module, "build_fusion_announcement_embed", lambda *_args: object())
        monkeypatch.setattr(fusion_sheets, "update_fusion_publication", AsyncMock())

        await cog.fusion_publish.callback(cog, ctx)

        channel.send.assert_awaited_once()
        ctx.reply.assert_awaited_once_with(
            "Fusion announcement published to configured channel for **Mavara**.",
            mention_author=False,
        )

    asyncio.run(_run())


def test_fusion_command_recreates_when_status_is_draft_even_with_existing_message(monkeypatch):
    async def _run() -> None:
        channel = FakeMessageable(123)
        channel.fetch_message = AsyncMock(return_value=SimpleNamespace(id=456, jump_url="https://discord.com/channels/1/123/456"))
        channel.send = AsyncMock(return_value=SimpleNamespace(id=1003, jump_url="https://discord.com/channels/1/123/1003"))
        bot = SimpleNamespace(
            get_channel=lambda _channel_id: channel,
            fetch_channel=AsyncMock(return_value=channel),
        )
        cog = FusionCog(bot)
        ctx = SimpleNamespace(reply=AsyncMock())

        async def _fake_get_publishable():
            return _fusion_row(announcement_channel_id=123, announcement_message_id=456, status="draft")

        update = AsyncMock()
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", _fake_get_publishable)
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[]))
        monkeypatch.setattr(fusion_cog_module, "build_fusion_announcement_embed", lambda *_args: object())
        monkeypatch.setattr(fusion_sheets, "update_fusion_publication", update)

        await cog.fusion.callback(cog, ctx)

        channel.send.assert_awaited_once()
        assert update.await_count == 1
        ctx.reply.assert_awaited_once_with(
            "🔗 Fusion’s up. Don’t get lost:\nhttps://discord.com/channels/1/123/1003",
            mention_author=False,
        )

    asyncio.run(_run())


def test_fusion_publish_load_failure_still_replies_when_internal_log_delivery_fails(monkeypatch):
    async def _run() -> None:
        bot = SimpleNamespace(get_channel=lambda _channel_id: None, fetch_channel=AsyncMock())
        cog = FusionCog(bot)
        ctx = SimpleNamespace(reply=AsyncMock())

        async def _fake_get_publishable():
            raise RuntimeError("sheet unavailable")

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("log channel missing")

        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", _fake_get_publishable)
        monkeypatch.setattr(fusion_logs.rt, "send_log_message", _boom)

        await cog.fusion_publish.callback(cog, ctx)

        ctx.reply.assert_awaited_once_with("Could not load fusion data right now.", mention_author=False)

    asyncio.run(_run())


def test_fusion_publish_announce_failure_still_replies_when_internal_log_delivery_fails(monkeypatch):
    async def _run() -> None:
        channel = FakeMessageable(123)
        bot = SimpleNamespace(get_channel=lambda _channel_id: channel, fetch_channel=AsyncMock())
        cog = FusionCog(bot)
        ctx = SimpleNamespace(reply=AsyncMock())

        async def _fake_get_publishable():
            return _fusion_row(announcement_channel_id=123, announcement_message_id=None, status="draft")

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("log channel missing")

        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", _fake_get_publishable)
        monkeypatch.setattr(fusion_cog_module, "publish_fusion_announcement", AsyncMock(side_effect=RuntimeError("discord outage")))
        monkeypatch.setattr(fusion_logs.rt, "send_log_message", _boom)

        await cog.fusion_publish.callback(cog, ctx)

        ctx.reply.assert_awaited_once_with("Failed to publish announcement right now.", mention_author=False)

    asyncio.run(_run())


def test_fusion_debug_event_failure_still_replies_when_internal_log_delivery_fails(monkeypatch):
    async def _run() -> None:
        bot = SimpleNamespace(get_channel=lambda _channel_id: None, fetch_channel=AsyncMock())
        cog = FusionCog(bot)
        ctx = SimpleNamespace(reply=AsyncMock())

        async def _fake_get_publishable():
            return _fusion_row()

        async def _boom(*_args, **_kwargs):
            raise RuntimeError("log channel missing")

        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", _fake_get_publishable)
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(side_effect=RuntimeError("sheets outage")))
        monkeypatch.setattr(fusion_logs.rt, "send_log_message", _boom)

        await cog.fusion_debug.callback(cog, ctx)

        ctx.reply.assert_awaited_once_with("Fusion events are temporarily unavailable.", mention_author=False)

    asyncio.run(_run())
