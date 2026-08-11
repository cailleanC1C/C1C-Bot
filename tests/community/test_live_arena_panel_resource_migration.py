from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from modules.community.live_arena.panel import LiveArenaPanelManager, PanelSyncResult


def run(awaitable):
    return asyncio.run(awaitable)


class _Template:
    def embed(self, **_values):
        return object()


def test_public_panel_prefers_tournament_resource_over_legacy_config_id():
    partial = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(
        id=10,
        get_partial_message=Mock(return_value=partial),
        send=AsyncMock(),
    )
    bot = SimpleNamespace(get_channel=lambda _id: channel, fetch_channel=AsyncMock())
    manager = LiveArenaPanelManager(bot, "sheet")

    tournament = SimpleNamespace(
        tournament_id="arena-1",
        tournament_name="Arena One",
        status="signup_closed",
        signup_closes_at_utc="2026-08-20T10:00:00Z",
        max_participants=16,
    )
    repository = SimpleNamespace(
        initialize=AsyncMock(),
        participants=AsyncMock(return_value=[]),
        discord_resource=AsyncMock(
            return_value={
                "message_id": "222",
                "state": "active",
                "created_at_utc": "",
                "notes": "",
            }
        ),
        upsert_discord_resource=AsyncMock(),
    )

    with (
        patch(
            "modules.community.live_arena.panel.load_pr3_config",
            AsyncMock(
                return_value=(
                    {
                        "MESSAGES_TAB": "MESSAGES",
                        "SIGNUP_CHANNEL_ID": "10",
                        "PUBLIC_PANEL_MESSAGE_ID": "111",
                    },
                    [],
                )
            ),
        ),
        patch(
            "modules.community.live_arena.panel.load_tournament_snapshot",
            AsyncMock(return_value=tournament),
        ),
        patch(
            "modules.community.live_arena.panel.load_messages",
            AsyncMock(return_value={"signup_closed": _Template()}),
        ),
        patch(
            "modules.community.live_arena.panel.LiveArenaRepository",
            return_value=repository,
        ),
    ):
        result = run(manager.sync())

    assert result == PanelSyncResult(True)
    channel.get_partial_message.assert_called_once_with(222)
    partial.edit.assert_awaited_once()
    channel.send.assert_not_awaited()
