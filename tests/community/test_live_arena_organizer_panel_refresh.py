"""Regression coverage for the Live Arena Captains Table refresh and Sheet copy."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from modules.community.live_arena.messages import (
    MESSAGE_HEADERS,
    MessageTemplate,
    load_messages,
)
from modules.community.live_arena.organizer_panel import (
    OrganizerPanelManager,
    _role_message_key,
    _role_template_values,
    _roster_message_key,
)


def run(awaitable):
    return asyncio.run(awaitable)


def test_organizer_message_contract_no_longer_contains_readiness_placeholder():
    matrix = [
        list(MESSAGE_HEADERS),
        [
            "organizer_panel",
            "Tournament registration controls",
            (
                "Manage registration for {tournament_name}.\n\n"
                "Status: {status}.\nConfirmed: {confirmed_count}/{max_participants}."
            ),
            "#5F6368",
            "TRUE",
            "Private organizer registration panel.",
        ],
    ]
    with patch(
        "modules.community.live_arena.messages.afetch_values",
        AsyncMock(return_value=matrix),
    ):
        template = run(load_messages("sheet", "MESSAGES", {"organizer_panel"}))[
            "organizer_panel"
        ]

    embed = template.embed(
        tournament_name="Cup",
        status="Registration open",
        confirmed_count=5,
        max_participants=16,
    )
    assert "Confirmed: 5/16" in embed.description
    assert "parity_summary" not in template.description


def test_roster_logic_selects_sheet_message_key_instead_of_owning_copy():
    manager = SimpleNamespace(_qualification_q1_status="")
    tournament = SimpleNamespace(status="signup_closed", min_participants=4)

    assert (
        _roster_message_key(manager, tournament, {"confirmed": 5})
        == "organizer_roster_odd"
    )
    assert (
        _roster_message_key(manager, tournament, {"confirmed": 6})
        == "organizer_roster_ready"
    )


def test_role_logic_selects_sheet_message_key_and_supplies_participant_names():
    role_state = {
        "missing": [SimpleNamespace(display_name="Alice", id=1)],
        "extra": [SimpleNamespace(display_name="Bob", id=2)],
        "unresolved": ["3"],
        "role_missing": False,
    }
    participants = [
        {
            "discord_user_id": "3",
            "display_name_at_signup": "Cara",
            "status": "confirmed",
        }
    ]

    assert (
        _role_message_key(role_state)
        == "organizer_roles_missing_extra_unresolved"
    )
    values = _role_template_values(role_state, participants)
    assert values["missing_participants"] == "Alice"
    assert values["extra_participants"] == "Bob"
    assert values["unresolved_participants"] == "Cara"


def test_organizer_sync_uses_sheet_templates_for_all_visible_sections():
    config = {
        "ORGANIZER_CHANNEL_ID": "123",
        "MESSAGES_TAB": "MESSAGES",
        "ORGANIZER_PANEL_MESSAGE_ID": "456",
    }
    tournament = SimpleNamespace(
        tournament_id="cup",
        tournament_name="Friendly Live Arena",
        status="signup_open",
        min_participants=4,
        max_participants=16,
    )
    participants = [
        {
            "tournament_id": "cup",
            "discord_user_id": str(user_id),
            "display_name_at_signup": f"Player {user_id}",
            "status": "confirmed",
        }
        for user_id in range(1, 6)
    ]
    counts = {"confirmed": 5, "withdrawn": 0, "removed": 1, "disqualified": 0}
    roles = {"missing": [], "extra": [], "unresolved": [], "role_missing": False}

    message = SimpleNamespace(edit=AsyncMock())
    channel = SimpleNamespace(
        guild=SimpleNamespace(),
        fetch_message=AsyncMock(return_value=message),
    )
    bot = SimpleNamespace(
        get_channel=lambda _channel_id: channel,
        fetch_channel=AsyncMock(),
    )
    manager = OrganizerPanelManager(bot, "sheet-refresh-regression", SimpleNamespace())
    manager.data = AsyncMock(
        return_value=(config, tournament, participants, counts, roles)
    )
    templates = {
        "organizer_panel": MessageTemplate(
            "organizer_panel",
            "SHEET BASE TITLE",
            "SHEET BASE {tournament_name} {status} {confirmed_count}/{max_participants}",
            0x5F6368,
        ),
        "organizer_roster_open": MessageTemplate(
            "organizer_roster_open",
            "SHEET ROSTER TITLE",
            "SHEET ROSTER {confirmed_count} {player_word} {min_participants}",
            0x5F6368,
        ),
        "organizer_statuses": MessageTemplate(
            "organizer_statuses",
            "SHEET STATUS TITLE",
            "SHEET STATUS {confirmed_count}/{withdrawn_count}/{removed_count}/{disqualified_count}",
            0x5F6368,
        ),
        "organizer_roles_ok": MessageTemplate(
            "organizer_roles_ok",
            "SHEET ROLE TITLE",
            "SHEET ROLE OK",
            0x5F6368,
        ),
    }

    with (
        patch(
            "modules.community.live_arena.organizer_panel.load_pr5_config",
            AsyncMock(return_value=(config, [])),
        ),
        patch(
            "modules.community.live_arena.organizer_panel.load_messages",
            AsyncMock(return_value=templates),
        ) as load,
    ):
        result = run(manager.sync())

    assert result.ok is True
    message.edit.assert_awaited_once()
    embed = message.edit.await_args.kwargs["embed"]
    assert embed.title == "SHEET BASE TITLE"
    assert "Registration open" in embed.description

    fields = {field.name: field.value for field in embed.fields}
    assert fields == {
        "SHEET ROSTER TITLE": "SHEET ROSTER 5 players 4",
        "SHEET STATUS TITLE": "SHEET STATUS 5/0/1/0",
        "SHEET ROLE TITLE": "SHEET ROLE OK",
    }
    requested = load.await_args.args[2]
    assert requested == {
        "organizer_panel",
        "organizer_roster_open",
        "organizer_statuses",
        "organizer_roles_ok",
    }
