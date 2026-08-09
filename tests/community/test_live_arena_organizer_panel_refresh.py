"""Regression coverage for the Live Arena Captains Table refresh."""

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
    _roster_readiness,
    _tournament_roles_text,
)


def run(awaitable):
    return asyncio.run(awaitable)


def test_organizer_message_no_longer_requires_readiness_placeholder():
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
        parity_summary="legacy value is optional",
    )
    assert "Confirmed: 5/16" in embed.description


def test_roster_readiness_uses_plain_tournament_language():
    manager = SimpleNamespace(_qualification_q1_status="")
    tournament = SimpleNamespace(
        status="signup_closed",
        min_participants=4,
    )

    text = _roster_readiness(manager, tournament, {"confirmed": 5})

    assert "even number of confirmed players" in text
    assert "first qualification round" in text
    assert "Q1" not in text
    assert "parity" not in text.lower()


def test_tournament_role_issue_names_every_affected_participant():
    text = _tournament_roles_text(
        {
            "missing": [SimpleNamespace(display_name="Alice", id=1)],
            "extra": [SimpleNamespace(display_name="Bob", id=2)],
            "unresolved": ["3"],
            "role_missing": False,
        },
        [
            {
                "discord_user_id": "3",
                "display_name_at_signup": "Cara",
                "status": "confirmed",
            }
        ],
    )

    assert "Alice" in text
    assert "Bob" in text
    assert "Cara" in text
    assert "Reconcile Roles" in text
    assert "cannot fix unresolved participants automatically" in text


def test_organizer_sync_edits_existing_panel_with_current_confirmed_count():
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
    template = MessageTemplate(
        "organizer_panel",
        "Tournament registration controls",
        (
            "Manage registration for {tournament_name}.\n\n"
            "Status: {status}.\nConfirmed: {confirmed_count}/{max_participants}."
        ),
        0x5F6368,
    )

    with (
        patch(
            "modules.community.live_arena.organizer_panel.load_pr5_config",
            AsyncMock(return_value=(config, [])),
        ),
        patch(
            "modules.community.live_arena.organizer_panel.load_messages",
            AsyncMock(return_value={"organizer_panel": template}),
        ),
    ):
        result = run(manager.sync())

    assert result.ok is True
    message.edit.assert_awaited_once()
    embed = message.edit.await_args.kwargs["embed"]
    assert "Status: Registration open" in embed.description
    assert "Confirmed: 5/16" in embed.description

    fields = {field.name: field.value for field in embed.fields}
    assert "Roster readiness" in fields
    assert "Tournament roles" in fields
    assert "Q1" not in fields["Roster readiness"]
    assert "parity" not in fields["Roster readiness"].lower()
    assert (
        fields["Tournament roles"]
        == "All confirmed players have the correct Tournament Participant role."
    )