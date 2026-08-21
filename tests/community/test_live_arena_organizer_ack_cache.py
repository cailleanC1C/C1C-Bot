from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from modules.community.live_arena import next_tournament, organizer_panel, sheets_read_hardening


def run(awaitable):
    return asyncio.run(awaitable)


class _Response:
    def __init__(self) -> None:
        self._done = False
        self.defer = AsyncMock(side_effect=self._mark_done)
        self.send_message = AsyncMock(side_effect=self._mark_done)
        self.edit_message = AsyncMock(side_effect=self._mark_done)

    def is_done(self) -> bool:
        return self._done

    async def _mark_done(self, *_args, **_kwargs) -> None:
        self._done = True


def _interaction(*, role_id: int = 5):
    response = _Response()
    return SimpleNamespace(
        user=SimpleNamespace(id=10, roles=[SimpleNamespace(id=role_id)]),
        guild=SimpleNamespace(),
        response=response,
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def _seed_role(sheet_id: str = "sheet", role_id: str = "5") -> None:
    sheets_read_hardening._auth_cache[sheet_id] = (role_id, time.monotonic())


def test_cached_organizer_authorization_has_no_sheet_read():
    sheets_read_hardening._auth_cache.clear()
    _seed_role()
    interaction = _interaction()
    manager = SimpleNamespace(sheet_id="sheet")

    with patch(
        "modules.community.live_arena.messages.load_pr5_config",
        AsyncMock(side_effect=AssertionError("authorization must not read Sheets")),
    ):
        allowed = run(organizer_panel.OrganizerView(manager).authorized(interaction))

    assert allowed is True
    interaction.response.send_message.assert_not_awaited()
    interaction.response.defer.assert_not_awaited()


def test_missing_authorization_cache_fails_closed_without_waiting_for_sheets():
    sheets_read_hardening._auth_cache.clear()
    interaction = _interaction()
    manager = SimpleNamespace(sheet_id="sheet")

    with patch.object(
        sheets_read_hardening, "_schedule_organizer_role_refresh", Mock()
    ) as schedule:
        allowed = run(organizer_panel.OrganizerView(manager).authorized(interaction))

    assert allowed is False
    schedule.assert_called_once_with(manager)
    interaction.response.send_message.assert_awaited_once()
    assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True


def test_refresh_roster_acknowledges_before_sheet_backed_render():
    sheets_read_hardening._auth_cache.clear()
    _seed_role()
    interaction = _interaction()
    manager = SimpleNamespace(sheet_id="sheet")
    button = organizer_panel.RefreshRoster(manager)

    async def roster_embed(_manager, _guild):
        assert interaction.response.is_done() is True
        return discord.Embed(title="Roster")

    with patch.object(organizer_panel, "roster_embed", AsyncMock(side_effect=roster_embed)):
        run(button.callback(interaction))

    interaction.response.defer.assert_awaited_once()
    interaction.edit_original_response.assert_awaited_once()


def test_effective_create_next_callback_acknowledges_before_loading_sheet_copy():
    sheets_read_hardening._auth_cache.clear()
    _seed_role()
    interaction = _interaction()
    manager = SimpleNamespace(sheet_id="sheet")
    button = next_tournament.CreateNextTournamentButton(manager)

    template = SimpleNamespace(embed=Mock(return_value=discord.Embed(title="Next")))

    async def load_messages(_sheet_id, _keys):
        assert interaction.response.is_done() is True
        return {"next_tournament_intro": template}

    with patch.object(
        next_tournament,
        "_load_next_messages",
        AsyncMock(side_effect=load_messages),
    ):
        run(button.callback(interaction))

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    interaction.followup.send.assert_awaited_once()
    assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
