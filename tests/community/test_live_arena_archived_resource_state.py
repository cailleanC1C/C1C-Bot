from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from modules.community.live_arena.panel import LiveArenaPanelManager, PanelSyncResult


def run(awaitable):
    return asyncio.run(awaitable)


class _Template:
    def embed(self, **_values):
        return object()


def test_archived_public_panel_does_not_recreate_missing_message():
    channel = SimpleNamespace(id=10, fetch_message=AsyncMock(), send=AsyncMock())
    bot = SimpleNamespace(get_channel=lambda _id: channel, fetch_channel=AsyncMock())
    manager = LiveArenaPanelManager(bot, "sheet")
    tournament = SimpleNamespace(
        tournament_id="arena-1",
        tournament_name="Arena One",
        status="archived",
        signup_closes_at_utc="2026-08-20T10:00:00Z",
        max_participants=16,
    )
    repository = SimpleNamespace(
        initialize=AsyncMock(),
        participants=AsyncMock(return_value=[]),
        discord_resource=AsyncMock(
            return_value={
                "message_id": "",
                "state": "active",
                "created_at_utc": "2026-08-01T00:00:00Z",
                "channel_id": "10",
                "thread_id": "",
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
                        "PUBLIC_PANEL_MESSAGE_ID": "",
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
    channel.send.assert_not_awaited()
    repository.upsert_discord_resource.assert_awaited_once()
    assert repository.upsert_discord_resource.await_args.kwargs["state"] == "retired"
