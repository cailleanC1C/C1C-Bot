from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from modules.community.live_arena import victory_ledger_final_refresh as refresh


def run(awaitable):
    return asyncio.run(awaitable)


def _prime_alert_copy(sheet_id="sheet"):
    refresh._round_alert_copy[sheet_id] = {
        "round_ready_to_close_alert": refresh._AlertTemplate(
            "round_ready_to_close_alert",
            "✅ Round ready to close",
            "**{round_name}** has all **{completed}/{total_matches}** matchups final.\n"
            "Use **Close Current Round** in Captain’s Table to finalize the round.",
            0x1A73E8,
        ),
        "round_ready_to_close_closed": refresh._AlertTemplate(
            "round_ready_to_close_closed",
            "✅ Round closed",
            "**{round_name}** has been closed. No further closure action is required.",
            0x34A853,
        ),
    }


class FakeChannel:
    def __init__(self, messages=()):
        self._messages = list(messages)
        self.send = AsyncMock()

    def history(self, *, limit=100):
        async def iterator():
            for message in self._messages[:limit]:
                yield message

        return iterator()


def _bot(channel):
    return SimpleNamespace(
        user=SimpleNamespace(id=999),
        get_channel=lambda _channel_id: channel,
        fetch_channel=AsyncMock(return_value=channel),
    )


def _round(status="ready_to_close"):
    return {
        "round_id": "LA-2026-TRIAL-01-Q2",
        "round_name": "Qualification Round 2",
        "status": status,
    }


def _matches():
    return [
        {"status": "finalized"},
        {"status": "finalized"},
        {"status": "forfeit"},
        {"status": "finalized"},
    ]


def test_live_standings_context_is_forced_onto_its_own_paragraph():
    embed = discord.Embed(
        title="🏆 Current qualification standings",
        description=(
            "*Includes finalized results through Round 2.***#1 · 2-0** <@1>\n"
            "**#2 · 1-0** <@2>"
        ),
    )

    refresh._normalize_standings_spacing([embed])

    assert embed.description == (
        "*Includes finalized results through Round 2.*\n\n"
        "**#1 · 2-0** <@1>\n**#2 · 1-0** <@2>"
    )


def test_round_ready_pings_organizer_role_once(monkeypatch):
    refresh._round_alert_copy.clear()
    _prime_alert_copy()
    channel = FakeChannel()
    channel.send.return_value = SimpleNamespace(id=123)
    monkeypatch.setattr(
        refresh,
        "load_pr5_config",
        AsyncMock(
            return_value=(
                {"ORGANIZER_CHANNEL_ID": "555", "ORGANIZER_ROLE_ID": "777"},
                [],
            )
        ),
    )

    run(refresh._sync_round_ready_alert(_bot(channel), "sheet", _round(), _matches()))

    channel.send.assert_awaited_once()
    kwargs = channel.send.await_args.kwargs
    assert kwargs["content"] == "<@&777>"
    assert kwargs["embed"].title == "✅ Round ready to close"
    assert "4/4" in kwargs["embed"].description
    assert kwargs["embed"].footer.text == (
        "live_arena:round_ready:LA-2026-TRIAL-01-Q2"
    )


def test_existing_round_ready_alert_is_updated_without_second_ping(monkeypatch):
    refresh._round_alert_copy.clear()
    _prime_alert_copy()
    marker = "live_arena:round_ready:LA-2026-TRIAL-01-Q2"
    old_embed = discord.Embed(title="✅ Round ready to close")
    old_embed.set_footer(text=marker)
    existing = SimpleNamespace(
        author=SimpleNamespace(id=999),
        embeds=[old_embed],
        edit=AsyncMock(),
    )
    channel = FakeChannel([existing])
    monkeypatch.setattr(
        refresh,
        "load_pr5_config",
        AsyncMock(
            return_value=(
                {"ORGANIZER_CHANNEL_ID": "555", "ORGANIZER_ROLE_ID": "777"},
                [],
            )
        ),
    )

    run(refresh._sync_round_ready_alert(_bot(channel), "sheet", _round(), _matches()))

    channel.send.assert_not_awaited()
    existing.edit.assert_awaited_once()
    assert "content" not in existing.edit.await_args.kwargs


def test_closed_round_resolves_existing_round_ready_alert(monkeypatch):
    refresh._round_alert_copy.clear()
    _prime_alert_copy()
    marker = "live_arena:round_ready:LA-2026-TRIAL-01-Q2"
    old_embed = discord.Embed(title="✅ Round ready to close")
    old_embed.set_footer(text=marker)
    existing = SimpleNamespace(
        author=SimpleNamespace(id=999),
        embeds=[old_embed],
        edit=AsyncMock(),
    )
    channel = FakeChannel([existing])
    monkeypatch.setattr(
        refresh,
        "load_pr5_config",
        AsyncMock(
            return_value=(
                {"ORGANIZER_CHANNEL_ID": "555", "ORGANIZER_ROLE_ID": "777"},
                [],
            )
        ),
    )

    run(
        refresh._sync_round_ready_alert(
            _bot(channel), "sheet", _round("closed"), _matches()
        )
    )

    channel.send.assert_not_awaited()
    existing.edit.assert_awaited_once()
    kwargs = existing.edit.await_args.kwargs
    assert kwargs["content"] == ""
    assert kwargs["embed"].title == "✅ Round closed"
