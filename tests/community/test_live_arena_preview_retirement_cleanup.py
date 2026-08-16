from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from modules.community.live_arena import preview_message_guard
from modules.community.live_arena import preview_retirement_cleanup as cleanup


def run(awaitable):
    return asyncio.run(awaitable)


class FakeMessage:
    def __init__(self, message_id):
        self.id = message_id
        self.deleted = False

    async def delete(self):
        self.deleted = True


@pytest.mark.asyncio
async def test_retirement_removes_every_exact_bot_authored_preview_copy(monkeypatch):
    channel = SimpleNamespace()
    manager = SimpleNamespace(
        sheet_id="sheet",
        bot=SimpleNamespace(user=SimpleNamespace(id=42)),
    )
    messages = [FakeMessage(100), FakeMessage(101)]
    monkeypatch.setattr(cleanup, "_organizer_channel", AsyncMock(return_value=channel))
    monkeypatch.setattr(
        preview_message_guard,
        "_matching_bot_messages",
        AsyncMock(return_value=messages),
    )

    deleted = await cleanup._delete_exact_preview_copies(
        manager,
        discord.Embed(
            title="Qualification Round 3 · Organizer Preview",
            description="same preview",
        ),
    )

    assert deleted == 2
    assert all(message.deleted for message in messages)


@pytest.mark.asyncio
async def test_cleanup_failure_does_not_replace_authoritative_retirement(monkeypatch):
    manager = SimpleNamespace(sheet_id="sheet", bot=SimpleNamespace())
    service = SimpleNamespace(snapshot=AsyncMock(side_effect=RuntimeError("broken preview")))

    deleted = await cleanup._cleanup_swiss_preview(manager, service, 3)

    assert deleted == 0
