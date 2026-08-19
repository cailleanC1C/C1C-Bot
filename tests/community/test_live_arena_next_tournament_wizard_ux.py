from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.community.live_arena import next_tournament
from modules.community.live_arena import next_tournament_wizard_ux as wizard


class _Response:
    def __init__(self):
        self.done = False
        self.modal = None

    def is_done(self):
        return self.done

    async def defer(self, **_kwargs):
        self.done = True

    async def send_modal(self, modal):
        self.modal = modal
        self.done = True

    async def send_message(self, **_kwargs):
        self.done = True


class _Interaction:
    def __init__(self):
        self.response = _Response()
        self.followup = SimpleNamespace(send=AsyncMock())
        self.edit_original_response = AsyncMock()
        self.user = SimpleNamespace(id=42)


class _Template:
    def __init__(self):
        self.calls = []

    def embed(self, **values):
        self.calls.append(values)
        return ("embed", values)


def _set_text(input_item, value: str):
    input_item._value = value


def test_wizard_install_replaces_legacy_start_and_timezone_form():
    assert next_tournament.NextTournamentStartView is wizard.TournamentSetupStartView
    assert next_tournament.NextTournamentBasicsModal is wizard.TournamentDetailsModal

    manager = SimpleNamespace(sheet_id="sheet-1")
    start = next_tournament.NextTournamentStartView(manager)
    assert start.children[0].label == "Start Tournament Setup"

    modal = next_tournament.NextTournamentBasicsModal(manager)
    labels = [item.label for item in modal.children]
    assert labels == [
        "Tournament name",
        "Short name",
        "Minimum players",
        "Maximum players",
        "Motto or tagline (optional)",
    ]
    assert "Tournament timezone" not in labels


def test_wizard_message_contract_exposes_all_four_progress_states():
    assert wizard._WIZARD_KEYS == {
        "next_tournament_wizard_intro": set(),
        "next_tournament_wizard_details": {
            "tournament_name",
            "short_name",
            "min_participants",
            "max_participants",
            "tournament_motto",
        },
        "next_tournament_wizard_schedule": {
            "tournament_name",
            "short_name",
            "min_participants",
            "max_participants",
            "tournament_motto",
            "signup_opens",
            "signup_closes",
        },
        "next_tournament_wizard_review": {
            "tournament_name",
            "short_name",
            "min_participants",
            "max_participants",
            "tournament_motto",
            "signup_opens",
            "signup_closes",
            "eligible_clans",
        },
    }


@pytest.mark.asyncio
async def test_start_opens_five_field_details_modal_without_second_authorization():
    manager = SimpleNamespace(sheet_id="sheet-1")
    view = wizard.TournamentSetupStartView(manager)
    interaction = _Interaction()

    await view.children[0].callback(interaction)

    assert isinstance(interaction.response.modal, wizard.TournamentDetailsModal)
    assert len(interaction.response.modal.children) == 5


@pytest.mark.asyncio
async def test_details_step_fixes_timezone_to_utc_and_keeps_motto_in_same_wizard(monkeypatch):
    manager = SimpleNamespace(sheet_id="sheet-1")
    modal = wizard.TournamentDetailsModal(manager)
    _set_text(modal.tournament_name, "Cutlass & Chaos Cup")
    _set_text(modal.short_name, "Cutlass & Chaos")
    _set_text(modal.minimum, "8")
    _set_text(modal.maximum, "16")
    _set_text(modal.motto, "No maps. No mercy. Just glorious bad decisions.")
    interaction = _Interaction()
    template = _Template()

    async def load_messages(sheet_id, keys):
        assert sheet_id == "sheet-1"
        assert keys == {"next_tournament_wizard_details"}
        return {"next_tournament_wizard_details": template}

    monkeypatch.setattr(next_tournament, "_load_next_messages", load_messages)

    await modal.on_submit(interaction)

    interaction.edit_original_response.assert_awaited_once()
    kwargs = interaction.edit_original_response.await_args.kwargs
    assert isinstance(kwargs["view"], wizard.TournamentSignupWindowView)
    draft = kwargs["view"].draft
    assert draft.timezone == "UTC"
    assert draft.tournament_motto == "No maps. No mercy. Just glorious bad decisions."
    assert template.calls[0]["tournament_name"] == "Cutlass & Chaos Cup"
    assert template.calls[0]["tournament_motto"] == draft.tournament_motto


@pytest.mark.asyncio
async def test_signup_window_is_utc_and_advances_to_clan_selection(monkeypatch):
    manager = SimpleNamespace(sheet_id="sheet-1")
    draft = next_tournament.NextTournamentDraft(
        tournament_name="Cutlass & Chaos Cup",
        short_name="Cutlass & Chaos",
        min_participants=8,
        max_participants=16,
        timezone="UTC",
        tournament_motto="Chaos wins.",
    )
    modal = wizard.TournamentSignupWindowModal(manager, draft)
    _set_text(modal.opens, "2099-09-01 18:00")
    _set_text(modal.closes, "2099-09-06 18:00")
    interaction = _Interaction()
    template = _Template()
    options = [
        next_tournament.ClanOption("C1C", "Pirate Crew", "111", active_current=True)
    ]

    class FakeService:
        def __init__(self, sheet_id):
            assert sheet_id == "sheet-1"

        async def clan_options(self):
            return options

    async def load_messages(_sheet_id, keys):
        assert keys == {"next_tournament_wizard_schedule"}
        return {"next_tournament_wizard_schedule": template}

    monkeypatch.setattr(next_tournament, "NextTournamentService", FakeService)
    monkeypatch.setattr(next_tournament, "_load_next_messages", load_messages)

    await modal.on_submit(interaction)

    kwargs = interaction.edit_original_response.await_args.kwargs
    assert isinstance(kwargs["view"], wizard.TournamentEligibilityView)
    assert kwargs["view"].draft.timezone == "UTC"
    assert kwargs["view"].draft.signup_opens_at_utc.endswith("Z")
    assert kwargs["view"].draft.signup_closes_at_utc.endswith("Z")
    assert template.calls[0]["signup_opens"]
    assert template.calls[0]["signup_closes"]


@pytest.mark.asyncio
async def test_clan_selection_is_final_review_gate(monkeypatch):
    manager = SimpleNamespace(sheet_id="sheet-1")
    draft = next_tournament.NextTournamentDraft(
        tournament_name="Cutlass & Chaos Cup",
        short_name="Cutlass & Chaos",
        min_participants=8,
        max_participants=16,
        timezone="UTC",
        signup_opens_at_utc="2099-09-01T18:00:00Z",
        signup_closes_at_utc="2099-09-06T18:00:00Z",
        tournament_motto="Chaos wins.",
    )
    options = [next_tournament.ClanOption("C1C", "Pirate Crew", "111")]
    view = wizard.TournamentEligibilityView(manager, draft, options)
    select = view.children[0]
    select._values = ["111"]
    interaction = _Interaction()
    template = _Template()

    async def load_messages(_sheet_id, keys):
        assert keys == {"next_tournament_wizard_review"}
        return {"next_tournament_wizard_review": template}

    monkeypatch.setattr(next_tournament, "_load_next_messages", load_messages)

    await select.callback(interaction)

    kwargs = interaction.edit_original_response.await_args.kwargs
    assert isinstance(kwargs["view"], next_tournament.ConfirmCreateNextTournamentView)
    assert kwargs["view"].draft.eligible_role_ids == ("111",)
    assert kwargs["view"].draft.timezone == "UTC"
    assert template.calls[0]["eligible_clans"] == "C1C · Pirate Crew"
