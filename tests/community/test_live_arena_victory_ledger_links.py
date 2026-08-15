from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from modules.community.live_arena import runtime_hooks
from modules.community.live_arena.overview_links import match_thread_link
from modules.community.live_arena.victory_ledger_link_guard import _normalize_overview_links


def run(awaitable):
    return asyncio.run(awaitable)


def test_match_thread_link_uses_named_jump_link_not_raw_mention():
    link = match_thread_link("222", "111")

    assert link == "💬 [Open match thread](https://discord.com/channels/111/222)"
    assert "<#222>" not in link


def test_q2_overview_first_render_already_uses_named_match_link():
    embed = runtime_hooks._competition_overview_embed(
        {"tournament_name": "C1C Live Arena Trial Cup"},
        {
            "round_name": "Qualification Round 2",
            "round_stage": "qualification",
            "round_number": "2",
            "status": "open",
            "deadline_at_utc": "2026-08-21T10:22:00Z",
        },
        [
            {
                "match_number": "1",
                "player_a_discord_user_id": "10",
                "player_b_discord_user_id": "20",
                "thread_id": "222",
                "status": "published",
            }
        ],
        [],
        guild_id="111",
    )

    match_value = embed.fields[0].value
    assert "💬 [Open match thread](https://discord.com/channels/111/222)" in match_value
    assert "<#222>" not in match_value


def test_post_result_overview_refresh_replaces_raw_thread_title_link_and_preserves_standings():
    embed = discord.Embed(title="Qualification Round 1")
    embed.add_field(
        name="Match 1",
        value="<@10> vs <@20>\nFinal: **2-1**\n<#222>",
        inline=False,
    )
    embed.add_field(
        name="Qualification standings",
        value="**#1** <@10> · **1-0**",
        inline=False,
    )
    message = SimpleNamespace(
        embeds=[embed],
        edit=AsyncMock(),
    )
    channel = SimpleNamespace(
        guild=SimpleNamespace(id=111),
        fetch_message=AsyncMock(return_value=message),
    )
    bot = SimpleNamespace(
        get_channel=lambda channel_id: channel if channel_id == 333 else None,
        fetch_channel=AsyncMock(),
    )
    service = SimpleNamespace(
        repository=SimpleNamespace(config={"ROUND_OVERVIEW_CHANNEL_ID": "333"}),
    )
    snapshot = SimpleNamespace(
        round_row={"overview_message_id": "444"},
        matches=(
            {
                "match_number": "1",
                "thread_id": "222",
            },
        ),
    )

    run(_normalize_overview_links(bot, service, snapshot))

    sent = message.edit.await_args.kwargs["embed"]
    match_value = sent.fields[0].value
    assert "Open match thread" in match_value
    assert "https://discord.com/channels/111/222" in match_value
    assert "<#222>" not in match_value
    assert "26-08 Trial Cup" not in match_value
    assert sent.fields[1].name == "Qualification standings"
    assert sent.fields[1].value == "**#1** <@10> · **1-0**"
