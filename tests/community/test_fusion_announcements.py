import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
from modules.community.fusion import announcements
from shared.sheets import fusion as fusion_sheets


def _fusion_row(*, opt_in_role_id: int | None) -> fusion_sheets.FusionRow:
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
        announcement_channel_id=123,
        opt_in_role_id=opt_in_role_id,
        announcement_message_id=None,
        published_at=None,
        status="draft",
    )


class _Channel:
    def __init__(self) -> None:
        self.id = 123
        self.send = AsyncMock(return_value=SimpleNamespace(id=999))


def test_publish_announcement_attaches_buttons_when_role_configured(monkeypatch):
    async def _run() -> None:
        channel = _Channel()
        bot = SimpleNamespace(get_channel=lambda _id: channel, fetch_channel=AsyncMock(return_value=channel))
        target = _fusion_row(opt_in_role_id=777)
        monkeypatch.setattr(announcements, "resolve_announcement_channel", AsyncMock(return_value=channel))
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[]))
        monkeypatch.setattr(announcements, "build_fusion_announcement_embed", lambda *_args: object())
        monkeypatch.setattr(announcements, "build_fusion_opt_in_view", lambda _target: "view")
        monkeypatch.setattr(fusion_sheets, "update_fusion_publication", AsyncMock())

        await announcements.publish_fusion_announcement(bot, target)

        _, kwargs = channel.send.await_args
        assert kwargs["view"] == "view"

    asyncio.run(_run())


def test_publish_announcement_omits_buttons_when_role_not_configured(monkeypatch):
    async def _run() -> None:
        channel = _Channel()
        bot = SimpleNamespace(get_channel=lambda _id: channel, fetch_channel=AsyncMock(return_value=channel))
        target = _fusion_row(opt_in_role_id=None)
        monkeypatch.setattr(announcements, "resolve_announcement_channel", AsyncMock(return_value=channel))
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[]))
        monkeypatch.setattr(announcements, "build_fusion_announcement_embed", lambda *_args: object())
        monkeypatch.setattr(announcements, "build_fusion_opt_in_view", lambda _target: None)
        monkeypatch.setattr(fusion_sheets, "update_fusion_publication", AsyncMock())

        await announcements.publish_fusion_announcement(bot, target)

        _, kwargs = channel.send.await_args
        assert kwargs["view"] is None

    asyncio.run(_run())


def test_publish_announcement_retries_without_image_on_http_exception(monkeypatch):
    async def _run() -> None:
        channel = _Channel()
        channel.send = AsyncMock(
            side_effect=[
                discord.HTTPException(response=SimpleNamespace(status=400, reason="Bad Request"), message="invalid image"),
                SimpleNamespace(id=1001),
            ]
        )
        bot = SimpleNamespace(get_channel=lambda _id: channel, fetch_channel=AsyncMock(return_value=channel))
        target = _fusion_row(opt_in_role_id=777)
        monkeypatch.setattr(announcements, "resolve_announcement_channel", AsyncMock(return_value=channel))
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[]))
        monkeypatch.setattr(announcements, "build_fusion_opt_in_view", lambda _target: "view")
        monkeypatch.setattr(fusion_sheets, "update_fusion_publication", AsyncMock())

        embed = discord.Embed(title="fusion")
        embed.set_image(url="https://cdn.discordapp.com/champion.png")
        monkeypatch.setattr(announcements, "build_fusion_announcement_embed", lambda *_args: embed)

        await announcements.publish_fusion_announcement(bot, target)

        assert channel.send.await_count == 2
        first_call = channel.send.await_args_list[0].kwargs["embed"]
        second_call = channel.send.await_args_list[1].kwargs["embed"]
        assert str(first_call.image.url) == "https://cdn.discordapp.com/champion.png"
        assert not str(second_call.image.url or "").strip()

    asyncio.run(_run())
