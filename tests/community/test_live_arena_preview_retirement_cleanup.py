from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from modules.community.live_arena import preview_message_guard
from modules.community.live_arena import preview_retirement_cleanup as cleanup


class FakeMessage:
    def __init__(self, message_id, *, embed=None, author_id=42):
        self.id = message_id
        self.deleted = False
        self.embeds = [embed] if embed is not None else []
        self.author = SimpleNamespace(id=author_id)

    async def delete(self):
        self.deleted = True


@pytest.mark.asyncio
async def test_retirement_removes_exact_duplicates_but_leaves_canonical(monkeypatch):
    embed = discord.Embed(
        title="Qualification Round 3 · Organizer Preview",
        description="same preview",
    )
    channel = SimpleNamespace()
    manager = SimpleNamespace(
        sheet_id="sheet",
        bot=SimpleNamespace(user=SimpleNamespace(id=42)),
    )
    canonical = FakeMessage(100, embed=embed)
    duplicate = FakeMessage(101, embed=embed)
    monkeypatch.setattr(
        preview_message_guard,
        "_matching_bot_messages",
        AsyncMock(return_value=[canonical, duplicate]),
    )

    deleted = await cleanup._delete_exact_preview_copies(
        manager,
        embed,
        channel=channel,
        canonical=canonical,
    )

    assert deleted == 1
    assert canonical.deleted is False
    assert duplicate.deleted is True
    preview_message_guard._matching_bot_messages.assert_awaited_once_with(
        channel,
        manager.bot,
        embed,
        current=canonical,
    )


@pytest.mark.asyncio
async def test_knockout_cleanup_uses_registered_discord_preview_not_round_snapshot(
    monkeypatch,
):
    embed = discord.Embed(
        title="Quarterfinals · Organizer Preview",
        description="organizer only",
    )
    canonical = FakeMessage(200, embed=embed)
    duplicate = FakeMessage(201, embed=embed)
    channel = SimpleNamespace(fetch_message=AsyncMock(return_value=canonical))
    bot = SimpleNamespace(
        user=SimpleNamespace(id=42),
        get_channel=lambda channel_id: channel,
        fetch_channel=AsyncMock(),
    )
    manager = SimpleNamespace(sheet_id="sheet", bot=bot)
    registration_repository = SimpleNamespace(
        discord_resource=AsyncMock(
            return_value={
                "state": "active",
                "channel_id": "10",
                "message_id": "200",
            }
        )
    )
    service = SimpleNamespace(
        repository=SimpleNamespace(config={"ACTIVE_TOURNAMENT_ID": "LA-TEST"}),
        registration_repository=registration_repository,
        snapshot=AsyncMock(side_effect=AssertionError("snapshot must not be called")),
    )
    monkeypatch.setattr(
        preview_message_guard,
        "_matching_bot_messages",
        AsyncMock(return_value=[canonical, duplicate]),
    )

    deleted = await cleanup._cleanup_knockout_preview(
        manager,
        service,
        "quarterfinal",
    )

    assert deleted == 1
    assert duplicate.deleted is True
    assert canonical.deleted is False
    service.snapshot.assert_not_called()
    registration_repository.discord_resource.assert_awaited_once_with(
        "LA-TEST",
        "knockout_preview",
        "quarterfinal",
    )


@pytest.mark.asyncio
async def test_defensive_cleanup_failure_is_warning_with_concrete_error(caplog):
    manager = SimpleNamespace(
        sheet_id="sheet",
        bot=SimpleNamespace(user=SimpleNamespace(id=42)),
    )
    service = SimpleNamespace(
        repository=SimpleNamespace(config={"ACTIVE_TOURNAMENT_ID": "LA-TEST"}),
        registration_repository=SimpleNamespace(
            discord_resource=AsyncMock(side_effect=RuntimeError("history unavailable"))
        ),
    )

    with caplog.at_level(
        logging.WARNING,
        logger="c1c.community.live_arena.preview_retirement_cleanup",
    ):
        deleted = await cleanup._cleanup_swiss_preview(manager, service, 3)

    assert deleted == 0
    assert "retired-preview duplicate scan skipped" in caplog.text
    assert "RuntimeError: history unavailable" in caplog.text
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


@pytest.mark.asyncio
async def test_missing_registered_preview_is_quiet_noop():
    manager = SimpleNamespace(sheet_id="sheet", bot=SimpleNamespace())
    service = SimpleNamespace(
        repository=SimpleNamespace(config={"ACTIVE_TOURNAMENT_ID": "LA-TEST"}),
        registration_repository=SimpleNamespace(
            discord_resource=AsyncMock(return_value=None)
        ),
    )

    deleted = await cleanup._cleanup_knockout_preview(
        manager,
        service,
        "semifinal",
    )

    assert deleted == 0
