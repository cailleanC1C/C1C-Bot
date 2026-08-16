from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from modules.community.live_arena import (
    competition_resolution,
    qualification_panel,
    round_overview,
    victory_ledger_final_refresh as final_refresh,
)


def run(awaitable):
    return asyncio.run(awaitable)


def test_last_updated_label_is_sheet_driven(monkeypatch):
    final_refresh._last_updated_labels.clear()
    monkeypatch.setattr(
        final_refresh,
        "load_pr5_config",
        AsyncMock(return_value=({"MESSAGES_TAB": "MESSAGES"}, [])),
    )
    monkeypatch.setattr(
        final_refresh,
        "afetch_values",
        AsyncMock(
            return_value=[
                [
                    "message_key",
                    "title",
                    "description",
                    "color_hex",
                    "active",
                    "notes",
                ],
                [
                    "round_overview_last_updated",
                    "Last updated",
                    "",
                    "#1A73E8",
                    "TRUE",
                    "footer",
                ],
            ]
        ),
    )

    label = run(final_refresh._load_last_updated_label("sheet"))

    assert label == "Last updated"
    assert final_refresh._last_updated_labels["sheet"] == "Last updated"


def test_victory_ledger_render_gets_visible_last_updated_timestamp():
    final_refresh._last_updated_labels["sheet"] = "Last updated"
    now = datetime(2026, 8, 16, 15, 0, tzinfo=UTC)
    embeds = [discord.Embed(title="Round"), discord.Embed(title="Standings")]

    rendered = final_refresh._stamp_last_updated(embeds, "sheet", now=now)

    assert rendered[-1].footer.text == "Last updated"
    assert rendered[-1].timestamp == now


def test_final_refresh_rewrites_existing_three_embed_overview_from_current_state(monkeypatch):
    existing = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(
        guild=SimpleNamespace(id=111),
        fetch_message=AsyncMock(return_value=existing),
        send=AsyncMock(),
    )
    monkeypatch.setattr(
        qualification_panel,
        "_resolve_channel",
        AsyncMock(return_value=channel),
    )

    expected_embeds = [
        discord.Embed(title="Qualification Round 2"),
        discord.Embed(title="⚔️ Matchups"),
        discord.Embed(title="🏆 Current qualification standings"),
    ]
    render = AsyncMock(return_value=expected_embeds)
    monkeypatch.setattr(round_overview, "render_round_overview_embeds", render)

    standings = [SimpleNamespace(rank=1, discord_user_id="2", match_record="2-0")]

    class FakeCompetitionResolutionService:
        def __init__(self, sheet_id):
            assert sheet_id == "sheet"

        async def initialize(self):
            return None

        async def standings(self):
            return standings

    monkeypatch.setattr(
        competition_resolution,
        "CompetitionResolutionService",
        FakeCompetitionResolutionService,
    )

    service = SimpleNamespace(
        sheet_id="sheet",
        repository=SimpleNamespace(config={"ROUND_OVERVIEW_CHANNEL_ID": "333"}),
        context=AsyncMock(
            return_value=(
                None,
                (None, {"tournament_name": "C1C Live Arena Trial Cup"}),
                None,
                [],
            )
        ),
        record_overview_message_id=AsyncMock(),
    )
    snapshot = SimpleNamespace(
        round_row={
            "round_id": "Q2",
            "round_stage": "qualification",
            "round_number": "2",
            "status": "active",
            "overview_message_id": "999",
        },
        matches=(
            {
                "match_id": "Q2-M01",
                "match_number": "1",
                "status": "finalized",
                "final_score_a": "0",
                "final_score_b": "3",
            },
            {
                "match_id": "Q2-M02",
                "match_number": "2",
                "status": "pending_confirmation",
            },
        ),
    )
    bot = SimpleNamespace()

    refreshed = run(final_refresh._force_overview_refresh(bot, service, snapshot))

    assert refreshed is True
    existing.edit.assert_awaited_once_with(embeds=expected_embeds)
    channel.send.assert_not_awaited()
    render.assert_awaited_once()
    kwargs = render.await_args.kwargs
    assert kwargs["matches"][0]["status"] == "finalized"
    assert kwargs["standings"] == standings


def test_final_refresh_recreates_missing_overview_and_persists_new_message_id(monkeypatch):
    created = SimpleNamespace(id=12345, delete=AsyncMock())
    channel = SimpleNamespace(
        guild=SimpleNamespace(id=111),
        fetch_message=AsyncMock(),
        send=AsyncMock(return_value=created),
    )
    monkeypatch.setattr(
        qualification_panel,
        "_resolve_channel",
        AsyncMock(return_value=channel),
    )
    monkeypatch.setattr(
        round_overview,
        "render_round_overview_embeds",
        AsyncMock(return_value=[discord.Embed(title="Round")]),
    )

    class FakeCompetitionResolutionService:
        def __init__(self, _sheet_id):
            pass

        async def initialize(self):
            return None

        async def standings(self):
            return []

    monkeypatch.setattr(
        competition_resolution,
        "CompetitionResolutionService",
        FakeCompetitionResolutionService,
    )

    service = SimpleNamespace(
        sheet_id="sheet",
        repository=SimpleNamespace(config={"ROUND_OVERVIEW_CHANNEL_ID": "333"}),
        context=AsyncMock(
            return_value=(None, (None, {"tournament_name": "Cup"}), None, [])
        ),
        record_overview_message_id=AsyncMock(),
    )
    snapshot = SimpleNamespace(
        round_row={
            "round_id": "Q2",
            "round_stage": "qualification",
            "status": "active",
            "overview_message_id": "",
        },
        matches=(),
    )

    refreshed = run(final_refresh._force_overview_refresh(SimpleNamespace(), service, snapshot))

    assert refreshed is True
    channel.fetch_message.assert_not_awaited()
    channel.send.assert_awaited_once()
    service.record_overview_message_id.assert_awaited_once_with("Q2", "12345")
