from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from modules.community.live_arena.competition_followup import (
    _post_result_thread_embed,
    _post_ruling_embed,
    _thread_has_result_screenshot,
    organizer_standings_lines,
)


def run(awaitable):
    return asyncio.run(awaitable)


def standing(uid, *, rank=1, wins=2, losses=1, gd=1, sos=4):
    return SimpleNamespace(
        discord_user_id=str(uid),
        rank=rank,
        match_wins=wins,
        match_losses=losses,
        match_record=f"{wins}-{losses}",
        game_differential=gd,
        strength_of_opponents=sos,
    )


def test_organizer_standings_show_full_tiebreak_and_clean_h2h():
    standings = [
        standing("10", rank=2),
        standing("20", rank=3),
        standing("30", rank=4, gd=0, sos=3),
    ]
    matches = [
        {
            "round_id": "LA-2026-TRIAL-01-Q2",
            "status": "finalized",
            "player_a_discord_user_id": "10",
            "player_b_discord_user_id": "20",
            "final_result_type": "played",
            "final_winner_discord_user_id": "10",
        }
    ]

    lines = organizer_standings_lines(standings, matches)

    assert "GD **+1**" in lines[0]
    assert "SoS **4**" in lines[0]
    assert "H2H won vs <@20>" in lines[0]
    assert "H2H lost vs <@10>" in lines[1]
    assert "H2H —" in lines[2]


def test_h2h_not_shown_for_three_player_primary_tie():
    standings = [standing("10"), standing("20"), standing("30")]
    matches = [
        {
            "round_id": "LA-2026-TRIAL-01-Q1",
            "status": "finalized",
            "player_a_discord_user_id": "10",
            "player_b_discord_user_id": "20",
            "final_result_type": "played",
            "final_winner_discord_user_id": "10",
        }
    ]

    lines = organizer_standings_lines(standings, matches)

    assert all("H2H —" in line for line in lines)


class Attachment:
    def __init__(self, filename="note.txt", content_type="text/plain"):
        self.filename = filename
        self.content_type = content_type


class Message:
    def __init__(self, author_id, attachments=()):
        self.author = SimpleNamespace(id=author_id)
        self.attachments = attachments


class HistoryChannel:
    def __init__(self, messages):
        self.messages = messages
        self.requested_limit = "unset"

    def history(self, *, limit):
        self.requested_limit = limit

        async def iterator():
            for message in self.messages:
                yield message

        return iterator()


def test_screenshot_search_scans_complete_thread_not_latest_100_only():
    messages = [Message("1") for _ in range(150)]
    messages.append(Message("2", [Attachment("result.png", "image/png")]))
    channel = HistoryChannel(messages)

    found = run(_thread_has_result_screenshot(channel, {"1", "2"}))

    assert found is True
    assert channel.requested_limit is None


def test_result_thread_notice_is_embed_only():
    channel = SimpleNamespace(send=AsyncMock())

    run(_post_result_thread_embed(channel, "Result reported by <@123>."))

    kwargs = channel.send.await_args.kwargs
    assert "embed" in kwargs
    assert "content" not in kwargs
    assert "Result reported" in kwargs["embed"].description


def test_organizer_ruling_notice_is_embed_only():
    thread = SimpleNamespace(send=AsyncMock())
    bot = SimpleNamespace(get_channel=lambda _id: thread, fetch_channel=AsyncMock())
    manager = SimpleNamespace(bot=bot)
    match = {"thread_id": "456"}

    run(_post_ruling_embed(manager, match, "replay", "Screenshot conflict"))

    kwargs = thread.send.await_args.kwargs
    assert "embed" in kwargs
    assert "content" not in kwargs
    assert kwargs["embed"].title == "Organizer ruling"
    assert "Screenshot conflict" in kwargs["embed"].description
