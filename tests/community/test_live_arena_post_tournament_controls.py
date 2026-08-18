from __future__ import annotations

from types import SimpleNamespace

import pytest

from modules.community.live_arena import next_tournament
from modules.community.live_arena import organizer_panel
from modules.community.live_arena import post_tournament_controls as boundary


class _Response:
    def __init__(self):
        self.deferred = False

    def is_done(self):
        return self.deferred

    async def defer(self, *, ephemeral=False):
        assert ephemeral is True
        self.deferred = True


class _Followup:
    def __init__(self):
        self.calls = []

    async def send(self, **kwargs):
        self.calls.append(kwargs)


class _Interaction:
    def __init__(self):
        self.response = _Response()
        self.followup = _Followup()
        self.user = SimpleNamespace(id=42, roles=[])
        self.guild = None


class _MessageTemplate:
    def embed(self):
        return "intro-embed"


@pytest.mark.asyncio
async def test_create_next_acknowledges_before_sheet_backed_authorization(monkeypatch):
    manager = SimpleNamespace(sheet_id="sheet-1")
    interaction = _Interaction()
    authorization_saw_defer = []

    async def authorized(self, candidate):
        authorization_saw_defer.append(candidate.response.deferred)
        return True

    async def load_messages(sheet_id, keys):
        assert sheet_id == "sheet-1"
        assert keys == {"next_tournament_intro"}
        return {"next_tournament_intro": _MessageTemplate()}

    sent = []

    async def send_ephemeral(candidate, *, embed, view=None):
        assert candidate.response.deferred is True
        sent.append((embed, view))

    monkeypatch.setattr(organizer_panel.OrganizerView, "authorized", authorized)
    monkeypatch.setattr(next_tournament, "_load_next_messages", load_messages)
    monkeypatch.setattr(organizer_panel, "_send_ephemeral", send_ephemeral)

    button = next_tournament.CreateNextTournamentButton(manager)
    await button.callback(interaction)

    assert authorization_saw_defer == [True]
    assert interaction.response.deferred is True
    assert sent and sent[0][0] == "intro-embed"
    assert isinstance(sent[0][1], next_tournament.NextTournamentStartView)


@pytest.mark.asyncio
async def test_create_next_has_independent_persistent_callback_registry():
    manager = SimpleNamespace(sheet_id="sheet-1")
    view = boundary.CreateNextTournamentPersistentView(manager)

    assert view.timeout is None
    assert len(view.children) == 1
    button = view.children[0]
    assert button.custom_id == "live_arena:organizer:tournament:create_next"


@pytest.mark.asyncio
async def test_persistent_callback_registration_is_restart_safe():
    registered = []

    class Bot:
        def add_view(self, view):
            registered.append(view)

    manager = SimpleNamespace(sheet_id="sheet-1")
    boundary._register_create_next_persistent_view(Bot(), manager)

    assert len(registered) == 1
    assert registered[0].timeout is None
    assert registered[0].children[0].custom_id == "live_arena:organizer:tournament:create_next"


def test_archived_captains_table_copy_is_terminal_and_points_to_new_tournament():
    assert boundary._archived_stage_summary() == (
        "Tournament archived",
        "Nothing. This tournament is complete and archived.",
        "Create a new tournament",
    )
