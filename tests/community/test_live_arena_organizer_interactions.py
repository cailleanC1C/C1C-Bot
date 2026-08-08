"""Regression coverage for Live Arena organizer interaction acknowledgement."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from modules.community.live_arena import organizer_panel
from modules.community.live_arena.organizer_panel import (
    ConfirmTransition,
    OrganizerView,
    RosterActions,
)


def run(awaitable):
    return asyncio.run(awaitable)


class _Response:
    def __init__(self, events):
        self.events = events
        self._done = False
        self.send_message = AsyncMock()

    async def defer(self, *, ephemeral=False):
        self.events.append("defer")
        self._done = True
        assert ephemeral is True

    def is_done(self):
        return self._done


def _interaction(events, *, roles=()):
    return SimpleNamespace(
        user=SimpleNamespace(id=7, roles=list(roles)),
        guild=SimpleNamespace(),
        response=_Response(events),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def test_view_roster_defers_before_loading_and_uses_followup():
    events = []
    manager = SimpleNamespace(sheet_id="sheet")
    interaction = _interaction(events)
    embed = discord.Embed(title="Roster")

    async def render(_manager, _guild):
        assert events == ["defer"]
        events.append("roster")
        return embed

    with (
        patch.object(OrganizerView, "authorized", AsyncMock(return_value=True)),
        patch.object(organizer_panel, "roster_embed", AsyncMock(side_effect=render)),
    ):
        run(OrganizerView(manager).roster(interaction, "roster"))

    assert events == ["defer", "roster"]
    kwargs = interaction.followup.send.await_args.kwargs
    assert kwargs["embed"] is embed
    assert isinstance(kwargs["view"], RosterActions)
    assert kwargs["ephemeral"] is True
    interaction.response.send_message.assert_not_awaited()


def test_close_registration_defers_before_roster_preflight_and_uses_followup():
    events = []

    async def data(_guild):
        assert events == ["defer"]
        events.append("data")
        return (
            {},
            SimpleNamespace(tournament_name="Trial Cup"),
            [],
            {"confirmed": 5},
            {},
        )

    manager = SimpleNamespace(sheet_id="sheet", data=AsyncMock(side_effect=data))
    interaction = _interaction(events)

    with patch.object(OrganizerView, "authorized", AsyncMock(return_value=True)):
        run(OrganizerView(manager, "signup_open").transition(interaction, "close"))

    assert events == ["defer", "data"]
    kwargs = interaction.followup.send.await_args.kwargs
    assert isinstance(kwargs["embed"], discord.Embed)
    assert "odd" in kwargs["embed"].description.lower()
    assert isinstance(kwargs["view"], ConfirmTransition)
    assert kwargs["ephemeral"] is True
    interaction.response.send_message.assert_not_awaited()


def test_deferred_authorization_denial_uses_followup_without_loading_roster():
    events = []
    manager = SimpleNamespace(sheet_id="sheet")
    interaction = _interaction(events, roles=[SimpleNamespace(id=4)])

    with (
        patch.object(
            organizer_panel,
            "load_pr5_config",
            AsyncMock(return_value=({"ORGANIZER_ROLE_ID": "5"}, [])),
        ),
        patch.object(organizer_panel, "roster_embed", AsyncMock()) as render,
    ):
        run(OrganizerView(manager).roster(interaction, "roster"))

    assert events == ["defer"]
    render.assert_not_awaited()
    kwargs = interaction.followup.send.await_args.kwargs
    assert "organizer role" in kwargs["embed"].description.lower()
    assert kwargs["ephemeral"] is True
    interaction.response.send_message.assert_not_awaited()


def test_roster_load_failure_after_defer_is_logged_and_reported(caplog):
    events = []
    manager = SimpleNamespace(sheet_id="sheet")
    interaction = _interaction(events)

    with (
        patch.object(OrganizerView, "authorized", AsyncMock(return_value=True)),
        patch.object(
            organizer_panel,
            "roster_embed",
            AsyncMock(side_effect=RuntimeError("sheet timeout")),
        ),
        caplog.at_level(
            logging.ERROR,
            logger="c1c.community.live_arena.organizer_panel",
        ),
    ):
        run(OrganizerView(manager).roster(interaction, "roster"))

    assert events == ["defer"]
    assert "organizer roster load failed" in caplog.text
    kwargs = interaction.followup.send.await_args.kwargs
    assert "sheet timeout" in kwargs["embed"].description
    assert kwargs["ephemeral"] is True
