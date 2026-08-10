"""Regression coverage for direct refresh of the saved Live Arena organizer panel."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from modules.community.live_arena.messages import MessageTemplate
from modules.community.live_arena.organizer_panel import OrganizerPanelManager
from modules.community.live_arena.panel import PanelSyncResult


def run(awaitable):
    return asyncio.run(awaitable)


def _templates():
    return {
        "organizer_panel": MessageTemplate(
            "organizer_panel",
            "Tournament registration controls",
            (
                "Manage registration for {tournament_name}.\n\n"
                "Status: {status}.\n"
                "Confirmed: {confirmed_count}/{max_participants}."
            ),
            0x5F6368,
        ),
        "organizer_roster_open": MessageTemplate(
            "organizer_roster_open",
            "Roster readiness",
            "{confirmed_count} {player_word} minimum {min_participants}",
            0x5F6368,
        ),
        "organizer_statuses": MessageTemplate(
            "organizer_statuses",
            "Participant statuses",
            "{confirmed_count}/{withdrawn_count}/{removed_count}/{disqualified_count}",
            0x5F6368,
        ),
        "organizer_roles_ok": MessageTemplate(
            "organizer_roles_ok",
            "Tournament roles",
            "Roles correct",
            0x5F6368,
        ),
    }


def _not_found():
    return discord.NotFound(
        SimpleNamespace(status=404, reason="missing"), "missing"
    )


def _manager(*, partial_edit_error=None):
    partial = SimpleNamespace(edit=AsyncMock())
    if partial_edit_error is not None:
        partial.edit.side_effect = partial_edit_error

    created = SimpleNamespace(id=999, delete=AsyncMock())
    channel = SimpleNamespace(
        guild=SimpleNamespace(),
        get_partial_message=Mock(return_value=partial),
        fetch_message=AsyncMock(side_effect=RuntimeError("history fetch must not run")),
        send=AsyncMock(return_value=created),
    )
    bot = SimpleNamespace(
        get_channel=lambda _channel_id: channel,
        fetch_channel=AsyncMock(),
    )
    manager = OrganizerPanelManager(bot, "sheet-direct-organizer", SimpleNamespace())
    manager.data = AsyncMock(
        return_value=(
            {
                "ORGANIZER_CHANNEL_ID": "10",
                "ORGANIZER_PANEL_MESSAGE_ID": "77",
                "MESSAGES_TAB": "MESSAGES",
            },
            SimpleNamespace(
                tournament_id="cup",
                tournament_name="Trial Cup",
                status="signup_open",
                min_participants=4,
                max_participants=16,
            ),
            [],
            {"confirmed": 5, "withdrawn": 0, "removed": 1, "disqualified": 0},
            {"missing": [], "extra": [], "unresolved": [], "role_missing": False},
        )
    )
    manager._persist = AsyncMock()
    return manager, channel, partial


def _config():
    return {
        "ORGANIZER_CHANNEL_ID": "10",
        "ORGANIZER_PANEL_MESSAGE_ID": "77",
        "MESSAGES_TAB": "MESSAGES",
    }


def test_saved_organizer_panel_edits_by_id_without_fetching_message_history():
    manager, channel, partial = _manager()

    with (
        patch(
            "modules.community.live_arena.organizer_panel.load_pr5_config",
            AsyncMock(return_value=(_config(), [])),
        ),
        patch(
            "modules.community.live_arena.organizer_panel.load_messages",
            AsyncMock(return_value=_templates()),
        ),
    ):
        result = run(manager.sync())

    assert result == PanelSyncResult(True)
    channel.get_partial_message.assert_called_once_with(77)
    channel.fetch_message.assert_not_awaited()
    channel.send.assert_not_awaited()
    manager._persist.assert_not_awaited()
    partial.edit.assert_awaited_once()
    embed = partial.edit.await_args.kwargs["embed"]
    assert "Confirmed: 5/16" in embed.description
    assert "Registration open" in embed.description


def test_direct_edit_not_found_recreates_and_persists_one_replacement():
    manager, channel, partial = _manager(partial_edit_error=_not_found())

    with (
        patch(
            "modules.community.live_arena.organizer_panel.load_pr5_config",
            AsyncMock(return_value=(_config(), [])),
        ),
        patch(
            "modules.community.live_arena.organizer_panel.load_messages",
            AsyncMock(return_value=_templates()),
        ),
    ):
        result = run(manager.sync())

    assert result == PanelSyncResult(True)
    partial.edit.assert_awaited_once()
    channel.fetch_message.assert_not_awaited()
    channel.send.assert_awaited_once()
    manager._persist.assert_awaited_once_with(_config(), "999")


def test_direct_edit_transient_failure_never_creates_duplicate_panel():
    manager, channel, partial = _manager(
        partial_edit_error=RuntimeError("temporary Discord edit failure")
    )

    with (
        patch(
            "modules.community.live_arena.organizer_panel.load_pr5_config",
            AsyncMock(return_value=(_config(), [])),
        ),
        patch(
            "modules.community.live_arena.organizer_panel.load_messages",
            AsyncMock(return_value=_templates()),
        ),
    ):
        result = run(manager.sync())

    assert result == PanelSyncResult(False, "edit")
    partial.edit.assert_awaited_once()
    channel.fetch_message.assert_not_awaited()
    channel.send.assert_not_awaited()
    manager._persist.assert_not_awaited()
