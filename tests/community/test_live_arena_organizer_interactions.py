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
from modules.community.live_arena.organizer_registration_hardening import (
    FastCloseConfirmView,
    _build_view_with_overflow,
    _overflow_persistent_views,
    _prune_registration_controls,
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


def test_live_close_button_callback_defers_before_captured_handler_work():
    events = []

    async def data(_guild):
        assert events == ["defer"]
        events.append("data")
        return (
            {},
            SimpleNamespace(tournament_name="Trial Cup"),
            [],
            {"confirmed": 8},
            {},
        )

    manager = SimpleNamespace(sheet_id="sheet", data=AsyncMock(side_effect=data))
    view = OrganizerView(manager, "signup_open")
    close_button = next(
        child
        for child in view.children
        if getattr(child, "custom_id", None) == "live_arena:organizer:close"
    )
    interaction = _interaction(events)

    with patch.object(OrganizerView, "authorized", AsyncMock(return_value=True)):
        run(close_button.callback(interaction))

    assert events == ["defer", "data"]
    kwargs = interaction.followup.send.await_args.kwargs
    assert isinstance(kwargs["view"], FastCloseConfirmView)
    assert kwargs["ephemeral"] is True


def test_registration_panel_pruning_removes_future_stage_controls():
    manager = SimpleNamespace(_qualification_q1_status="")
    view = discord.ui.View(timeout=None)
    for label in (
        "Close Registration",
        "View Roster",
        "Reconcile Roles",
        "Generate Q1 Draw",
        "Approve & Publish Swiss",
        "Freeze Top 8",
        "Create Next Tournament",
    ):
        view.add_item(discord.ui.Button(label=label))

    pruned = _prune_registration_controls(view, manager, "signup_open")
    labels = {getattr(child, "label", None) for child in pruned.children}

    assert labels == {"Close Registration", "View Roster", "Reconcile Roles"}


def test_decorated_view_captures_26th_control_instead_of_raising():
    def builder(_status=None):
        view = discord.ui.View(timeout=None)
        for index in range(26):
            view.add_item(
                discord.ui.Button(
                    label=f"Control {index + 1}",
                    custom_id=f"live_arena:test:{index + 1}",
                )
            )
        return view

    primary, overflow = _build_view_with_overflow(builder)

    assert len(primary.children) == 25
    assert len(overflow) == 1
    assert overflow[0].label == "Control 26"

    overflow_views = _overflow_persistent_views(overflow)
    assert len(overflow_views) == 1
    assert len(overflow_views[0].children) == 1
    assert overflow_views[0].children[0].custom_id == "live_arena:test:26"


def test_registration_pruning_readds_allowed_overflow_control_after_shrinking():
    manager = SimpleNamespace(_qualification_q1_status="")
    view = discord.ui.View(timeout=None)
    for index in range(25):
        label = "Close Registration" if index == 0 else f"Future {index}"
        view.add_item(discord.ui.Button(label=label, custom_id=f"live_arena:test:{index}"))
    overflow = [
        discord.ui.Button(
            label="Player History",
            custom_id="live_arena:organizer:player_history",
        )
    ]

    pruned = _prune_registration_controls(
        view,
        manager,
        "signup_open",
        overflow=overflow,
    )
    labels = {getattr(child, "label", None) for child in pruned.children}

    assert labels == {"Close Registration", "Player History"}


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
