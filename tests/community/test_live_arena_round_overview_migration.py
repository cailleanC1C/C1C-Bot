from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from modules.community.live_arena import competition_resolution
from modules.community.live_arena import round_overview
from modules.community.live_arena import round_overview_migration as migration


def run(awaitable):
    return asyncio.run(awaitable)


def _snapshot():
    return SimpleNamespace(
        status="open",
        round_row={
            "round_id": "LA-2026-TRIAL-01-Q2",
            "round_name": "Qualification Round 2",
            "round_stage": "qualification",
            "round_number": "2",
            "status": "open",
            "deadline_at_utc": "2026-08-21T10:22:11Z",
            "overview_message_id": "1538166405499715656",
        },
        matches=(
            {
                "match_number": "1",
                "status": "published",
                "player_a_discord_user_id": "10",
                "player_b_discord_user_id": "20",
                "thread_id": "222",
            },
        ),
    )


def test_existing_q2_single_embed_is_rewritten_in_place_with_production_config_shape(
    monkeypatch,
):
    existing_message = SimpleNamespace(
        embeds=[discord.Embed(title="Qualification Round 2")],
        edit=AsyncMock(),
    )
    channel = SimpleNamespace(
        guild=SimpleNamespace(id=111),
        fetch_message=AsyncMock(return_value=existing_message),
    )
    bot = SimpleNamespace(
        get_channel=lambda channel_id: channel if channel_id == 333 else None,
        fetch_channel=AsyncMock(return_value=channel),
    )
    service = SimpleNamespace(
        sheet_id="sheet-live",
        # This is the real QualificationRepository config shape. In particular,
        # it deliberately does NOT contain MESSAGES_TAB.
        repository=SimpleNamespace(
            config={
                "ROUNDS_TAB": "ROUNDS",
                "MATCHES_TAB": "MATCHES",
                "MATCH_FORUM_CHANNEL_ID": "444",
                "ROUND_OVERVIEW_CHANNEL_ID": "333",
            }
        ),
        context=AsyncMock(
            return_value=(
                None,
                (None, {"tournament_name": "C1C Live Arena Trial Cup"}),
                None,
                [],
            )
        ),
    )

    standings = [SimpleNamespace(rank=1, discord_user_id="10", match_record="1-0")]

    class FakeCompetitionResolutionService:
        def __init__(self, sheet_id):
            assert sheet_id == "sheet-live"

        async def initialize(self):
            return None

        async def standings(self):
            return standings

    render = AsyncMock(
        return_value=[
            discord.Embed(title="Qualification Round 2"),
            discord.Embed(title="⚔️ Matchups"),
            discord.Embed(title="🏆 Qualification standings"),
        ]
    )
    monkeypatch.setattr(
        competition_resolution,
        "CompetitionResolutionService",
        FakeCompetitionResolutionService,
    )
    monkeypatch.setattr(round_overview, "render_round_overview_embeds", render)

    verified = run(
        migration.ensure_existing_overview_payload(bot, service, _snapshot())
    )

    assert verified is True
    channel.fetch_message.assert_awaited_once_with(1538166405499715656)
    existing_message.edit.assert_awaited_once()
    kwargs = existing_message.edit.await_args.kwargs
    assert "embed" not in kwargs
    assert [embed.title for embed in kwargs["embeds"]] == [
        "Qualification Round 2",
        "⚔️ Matchups",
        "🏆 Qualification standings",
    ]
    render.assert_awaited_once()
    render_kwargs = render.await_args.kwargs
    assert render_kwargs["sheet_id"] == "sheet-live"
    assert render_kwargs["guild_id"] == "111"
    assert render_kwargs["standings"] == standings


def test_existing_q2_already_three_embeds_is_verified_without_rewrite(monkeypatch):
    existing_message = SimpleNamespace(
        embeds=[
            discord.Embed(title="Qualification Round 2"),
            discord.Embed(title="⚔️ Matchups"),
            discord.Embed(title="🏆 Qualification standings"),
        ],
        edit=AsyncMock(),
    )
    channel = SimpleNamespace(
        guild=SimpleNamespace(id=111),
        fetch_message=AsyncMock(return_value=existing_message),
    )
    bot = SimpleNamespace(
        get_channel=lambda _channel_id: channel,
        fetch_channel=AsyncMock(return_value=channel),
    )
    service = SimpleNamespace(
        sheet_id="sheet-live",
        repository=SimpleNamespace(
            config={"ROUND_OVERVIEW_CHANNEL_ID": "333"}
        ),
        context=AsyncMock(),
    )
    render = AsyncMock()
    monkeypatch.setattr(round_overview, "render_round_overview_embeds", render)

    verified = run(
        migration.ensure_existing_overview_payload(bot, service, _snapshot())
    )

    assert verified is True
    existing_message.edit.assert_not_awaited()
    service.context.assert_not_awaited()
    render.assert_not_awaited()
