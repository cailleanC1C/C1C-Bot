from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.community.live_arena import competition_resolution, round_overview, runtime_hooks


def run(awaitable):
    return asyncio.run(awaitable)


@pytest.fixture(autouse=True)
def clear_round_overview_copy():
    round_overview.clear_copy_cache()
    yield
    round_overview.clear_copy_cache()


def _prime_copy(sheet_id="sheet-round-overview"):
    values = {
        "round_overview_general": (
            "{round_name}",
            "**{tournament_name}**\n**State:** {state_label}\n**Round deadline:** {round_deadline}\n**Completed:** **{completed} / {total_matches}**",
        ),
        "round_overview_general_closed": (
            "{round_name} · Final Outcome",
            "**{tournament_name}**\n**Status:** Final outcome\n**Finalized:** {finalized}\n**Completed:** **{completed} / {total_matches}**",
        ),
        "round_overview_matchups": ("⚔️ Matchups", ""),
        "round_overview_match": (
            "Match {match_number}",
            "{player_a_mention} vs {player_b_mention}\n{result_line}\n{match_thread_link}",
        ),
        "round_overview_standings": (
            "🏆 Qualification standings",
            "{standings_lines}",
        ),
        "round_overview_standing_line": (
            "",
            "**#{rank}** {player_mention} · **{record}**",
        ),
        "round_overview_standing_player": (
            "",
            "**#{rank}** {player_mention}",
        ),
        "round_overview_standing_record": ("", "**{record}**"),
        "round_overview_bye": (
            "Qualification bye",
            "{player_mention} receives the bye.\nFinal: **bye** · +1 match win · +2 game differential.",
        ),
        "round_state_open": ("Round is open", ""),
        "round_state_ready_to_close": (
            "All matchups are final · ready for organizer closure",
            "",
        ),
        "round_state_correction": (
            "⚠️ Correction in progress · next-round publication blocked",
            "",
        ),
        "round_result_pending": ("Result pending", ""),
        "round_result_pending_confirmation": (
            "Result reported · objection window open",
            "",
        ),
        "round_result_disputed": ("⚠️ Result disputed · organizer review", ""),
        "round_result_late_review": ("⏰ Late result · organizer review", ""),
        "round_result_finalized": ("Final: **{score_a}-{score_b}**", ""),
        "round_result_forfeit_with_winner": (
            "Final: **forfeit** · winner {winner_mention}",
            "",
        ),
        "round_result_forfeit": ("Final: **forfeit**", ""),
        "round_result_double_forfeit": ("Final: **double forfeit**", ""),
        "round_result_bye": ("Final: **bye**", ""),
        "round_thread_link": ("Open match thread", ""),
        "round_thread_pending": ("Forum post pending", ""),
        "round_standings_empty": ("No finalized results yet.", ""),
    }
    round_overview._COPY_CACHE[sheet_id] = {
        key: round_overview.CopyTemplate(key, title, description, 0x1A73E8)
        for key, (title, description) in values.items()
    }
    return sheet_id


def _round(status="active", overview_message_id=""):
    return {
        "round_name": "Qualification Round 2",
        "round_stage": "qualification",
        "round_number": "2",
        "status": status,
        "deadline_at_utc": "2026-08-21T12:22:00Z",
        "completed_at_utc": "2026-08-21T12:30:00Z" if status == "closed" else "",
        "overview_message_id": overview_message_id,
        "round_id": "Q2",
    }


def _matches():
    return [
        {
            "match_number": "1",
            "status": "published",
            "player_a_discord_user_id": "10",
            "player_b_discord_user_id": "20",
            "thread_id": "222",
        },
        {
            "match_number": "2",
            "status": "finalized",
            "player_a_discord_user_id": "30",
            "player_b_discord_user_id": "40",
            "final_score_a": "2",
            "final_score_b": "1",
            "thread_id": "223",
        },
    ]


def _standings():
    return [
        SimpleNamespace(rank=1, discord_user_id="10", match_record="1-0"),
        SimpleNamespace(rank=2, discord_user_id="20", match_record="0-1"),
    ]


def test_qualification_overview_is_one_payload_with_three_structured_embeds():
    sheet_id = _prime_copy()

    embeds = run(
        round_overview.render_round_overview_embeds(
            sheet_id=sheet_id,
            tournament={"tournament_name": "C1C Live Arena Trial Cup"},
            round_row=_round(),
            matches=_matches(),
            standings=_standings(),
            guild_id="111",
        )
    )

    assert len(embeds) == 3
    assert embeds[0].title == "Qualification Round 2"
    assert "Round is open" in embeds[0].description
    assert embeds[0].fields == []

    assert embeds[1].title == "⚔️ Matchups"
    assert [field.name for field in embeds[1].fields] == ["Match 1", "Match 2"]
    assert "Result pending" in embeds[1].fields[0].value
    assert "💬 [Open match thread](https://discord.com/channels/111/222)" in embeds[1].fields[0].value
    assert "Final: **2-1**" in embeds[1].fields[1].value

    assert embeds[2].title == "🏆 Qualification standings"
    assert embeds[2].description in (None, "")
    assert len(embeds[2].fields) == 2
    assert all(field.inline for field in embeds[2].fields)
    assert embeds[2].fields[0].name == "\u200b"
    assert embeds[2].fields[1].name == "\u200b"
    assert embeds[2].fields[0].value == "**#1** <@10>\n**#2** <@20>"
    assert embeds[2].fields[1].value == "**1-0**\n**0-1**"


def test_closed_round_keeps_same_three_embed_structure_with_final_outcome_header():
    sheet_id = _prime_copy()

    embeds = run(
        round_overview.render_round_overview_embeds(
            sheet_id=sheet_id,
            tournament={"tournament_name": "C1C Live Arena Trial Cup"},
            round_row=_round(status="closed"),
            matches=_matches(),
            standings=_standings(),
            guild_id="111",
        )
    )

    assert len(embeds) == 3
    assert embeds[0].title == "Qualification Round 2 · Final Outcome"
    assert "**Status:** Final outcome" in embeds[0].description
    assert embeds[1].title == "⚔️ Matchups"
    assert embeds[2].title == "🏆 Qualification standings"
    assert len(embeds[2].fields) == 2


def _sync_context(sheet_id, channel):
    bot = SimpleNamespace(
        get_channel=lambda _channel_id: channel,
        fetch_channel=AsyncMock(return_value=channel),
    )
    service = SimpleNamespace(
        sheet_id=sheet_id,
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
    return bot, service


def _install_fake_standings(monkeypatch):
    class FakeCompetitionResolutionService:
        def __init__(self, _sheet_id):
            pass

        async def initialize(self):
            return None

        async def standings(self):
            return _standings()

    monkeypatch.setattr(
        runtime_hooks,
        "CompetitionResolutionService",
        FakeCompetitionResolutionService,
    )
    monkeypatch.setattr(
        competition_resolution,
        "CompetitionResolutionService",
        FakeCompetitionResolutionService,
    )


def test_round_sync_sends_all_embeds_in_one_discord_message(monkeypatch):
    sheet_id = _prime_copy()
    created = SimpleNamespace(id=999, delete=AsyncMock())
    channel = SimpleNamespace(
        guild=SimpleNamespace(id=111),
        send=AsyncMock(return_value=created),
        fetch_message=AsyncMock(),
    )
    bot, service = _sync_context(sheet_id, channel)
    snapshot = SimpleNamespace(
        status="active",
        round_row=_round(),
        matches=(),
    )
    _install_fake_standings(monkeypatch)

    warnings = run(runtime_hooks._sync_round_discord(bot, service, snapshot))

    assert warnings == []
    channel.send.assert_awaited_once()
    kwargs = channel.send.await_args.kwargs
    assert "embed" not in kwargs
    assert len(kwargs["embeds"]) == 3
    assert kwargs["embeds"][0].title == "Qualification Round 2"
    assert kwargs["embeds"][1].title == "⚔️ Matchups"
    assert kwargs["embeds"][2].title == "🏆 Qualification standings"
    assert kwargs["embeds"][2].fields[1].value == "**1-0**\n**0-1**"
    service.record_overview_message_id.assert_awaited_once_with("Q2", "999")


def test_round_sync_updates_existing_message_with_aligned_standings(monkeypatch):
    sheet_id = _prime_copy()
    existing = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(
        guild=SimpleNamespace(id=111),
        send=AsyncMock(),
        fetch_message=AsyncMock(return_value=existing),
    )
    bot, service = _sync_context(sheet_id, channel)
    snapshot = SimpleNamespace(
        status="active",
        round_row=_round(overview_message_id="1538166405499715656"),
        matches=(),
    )
    _install_fake_standings(monkeypatch)

    warnings = run(runtime_hooks._sync_round_discord(bot, service, snapshot))

    assert warnings == []
    channel.send.assert_not_awaited()
    assert existing.edit.await_count >= 1
    embed_edits = [
        call.kwargs for call in existing.edit.await_args_list if "embeds" in call.kwargs
    ]
    assert embed_edits
    kwargs = embed_edits[-1]
    assert "embed" not in kwargs
    assert len(kwargs["embeds"]) == 3
    assert kwargs["embeds"][2].title == "🏆 Qualification standings"
    assert kwargs["embeds"][2].fields[0].value == "**#1** <@10>\n**#2** <@20>"
    assert kwargs["embeds"][2].fields[1].value == "**1-0**\n**0-1**"
