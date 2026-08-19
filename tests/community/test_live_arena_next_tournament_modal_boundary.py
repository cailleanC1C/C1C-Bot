from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from modules.community.live_arena import next_tournament
from modules.community.live_arena import next_tournament_modal_boundary as boundary


class _Followup:
    def __init__(self):
        self.calls = []

    async def send(self, **kwargs):
        self.calls.append(kwargs)


class _Response:
    def __init__(self, *, fail_after_ack=False, fail_before_ack=False):
        self.done = False
        self.modal = None
        self.messages = []
        self.fail_after_ack = fail_after_ack
        self.fail_before_ack = fail_before_ack

    def is_done(self):
        return self.done

    async def send_modal(self, modal):
        self.modal = modal
        if self.fail_before_ack:
            raise RuntimeError("modal transport failed before acknowledgement")
        self.done = True
        if self.fail_after_ack:
            raise RuntimeError("modal handoff failed after acknowledgement")

    async def send_message(self, **kwargs):
        self.done = True
        self.messages.append(kwargs)


class _Interaction:
    def __init__(self, response):
        self.response = response
        self.followup = _Followup()
        self.user = SimpleNamespace(id=42)


@pytest.mark.asyncio
async def test_details_button_opens_modal_without_second_sheet_authorization(monkeypatch):
    manager = SimpleNamespace(sheet_id="sheet-1")
    response = _Response()
    interaction = _Interaction(response)

    async def should_not_authorize(*_args, **_kwargs):
        raise AssertionError("ephemeral details view must not repeat Sheet-backed authorization")

    from modules.community.live_arena import organizer_panel

    monkeypatch.setattr(organizer_panel.OrganizerView, "authorized", should_not_authorize)

    view = next_tournament.NextTournamentStartView(manager)
    await view.children[0].callback(interaction)

    assert response.done is True
    assert isinstance(response.modal, next_tournament.NextTournamentBasicsModal)
    assert response.messages == []
    assert interaction.followup.calls == []


@pytest.mark.asyncio
async def test_post_ack_modal_handoff_exception_does_not_escape_view(caplog):
    manager = SimpleNamespace(sheet_id="sheet-1")
    response = _Response(fail_after_ack=True)
    interaction = _Interaction(response)
    view = next_tournament.NextTournamentStartView(manager)

    with caplog.at_level(logging.WARNING, logger=boundary.log.name):
        await view.children[0].callback(interaction)

    assert response.done is True
    assert isinstance(response.modal, next_tournament.NextTournamentBasicsModal)
    assert interaction.followup.calls == []
    assert "modal acknowledged but response handoff raised" in caplog.text
    assert "RuntimeError: modal handoff failed after acknowledgement" in caplog.text


@pytest.mark.asyncio
async def test_pre_ack_modal_failure_returns_ephemeral_error(caplog):
    manager = SimpleNamespace(sheet_id="sheet-1")
    response = _Response(fail_before_ack=True)
    interaction = _Interaction(response)
    view = next_tournament.NextTournamentStartView(manager)

    with caplog.at_level(logging.ERROR, logger=boundary.log.name):
        await view.children[0].callback(interaction)

    assert response.done is True
    assert response.messages
    assert response.messages[0]["ephemeral"] is True
    assert "modal launch failed" in caplog.text
    assert "RuntimeError: modal transport failed before acknowledgement" in caplog.text
