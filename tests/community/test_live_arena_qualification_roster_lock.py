from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from modules.community.live_arena import organizer_panel
from modules.community.live_arena import qualification_lock as lock_module
from modules.community.live_arena.organizer import OrganizerService
from modules.community.live_arena.organizer_panel import OrganizerPanelManager
from modules.community.live_arena.registration import (
    RegistrationError,
    RegistrationService,
    RegistrationSnapshot,
)


def run(awaitable):
    return asyncio.run(awaitable)


def _registration_snapshot(*, can_withdraw=True):
    return RegistrationSnapshot(
        config={},
        tournament={"tournament_name": "Trial Cup"},
        participant={"status": "confirmed"},
        status="confirmed",
        timezone="Europe/Vienna",
        slots=(),
        selected_slot_ids=(),
        localized_slots=(),
        tournament_status="signup_closed",
        can_update=False,
        can_withdraw=can_withdraw,
    )


@pytest.mark.parametrize(("status", "expected"), [("active", True), ("completed", True), ("proposed", False)])
def test_qualification_roster_locked_uses_live_q1_status(monkeypatch, status, expected):
    class Repo:
        def __init__(self, _sheet_id):
            self.config = {}

        async def rounds(self):
            return [
                {
                    "tournament_id": "cup",
                    "round_id": "cup-Q1",
                    "status": status,
                }
            ]

    monkeypatch.setattr(
        lock_module,
        "load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": "cup"}),
    )
    monkeypatch.setattr(
        lock_module,
        "load_qualification_config",
        AsyncMock(return_value={"ROUNDS_TAB": "ROUNDS"}),
    )
    monkeypatch.setattr(lock_module, "QualificationRepository", Repo)

    assert run(lock_module.qualification_roster_locked("sheet")) is expected


def test_locked_registration_hides_and_blocks_withdrawal(monkeypatch):
    original_get = AsyncMock(return_value=_registration_snapshot())
    original_withdraw = AsyncMock()
    monkeypatch.setattr(RegistrationService, "get_registration", original_get)
    monkeypatch.setattr(RegistrationService, "withdraw", original_withdraw)
    monkeypatch.setattr(
        lock_module,
        "qualification_roster_locked",
        AsyncMock(return_value=True),
    )
    service = lock_module.LockedRegistrationService("sheet")

    snapshot = run(service.get_registration("7"))
    assert snapshot.can_withdraw is False

    with pytest.raises(RegistrationError, match="roster is locked"):
        run(service.withdraw("7", "changed mind"))
    original_withdraw.assert_not_awaited()


def test_locked_organizer_blocks_reopen_remove_and_restore(monkeypatch):
    original_transition = AsyncMock(return_value=123)
    original_change = AsyncMock()
    monkeypatch.setattr(OrganizerService, "transition", original_transition)
    monkeypatch.setattr(OrganizerService, "_participant_change", original_change)
    monkeypatch.setattr(
        lock_module,
        "qualification_roster_locked",
        AsyncMock(return_value=True),
    )
    service = lock_module.LockedOrganizerService("sheet", repository=SimpleNamespace())

    with pytest.raises(RegistrationError, match="roster is locked"):
        run(service.transition("reopen", "99"))
    with pytest.raises(RegistrationError, match="roster is locked"):
        run(service._participant_change("99", "7", restore=False))
    with pytest.raises(RegistrationError, match="roster is locked"):
        run(service._participant_change("99", "7", restore=True, member=SimpleNamespace()))

    original_transition.assert_not_awaited()
    original_change.assert_not_awaited()


def test_install_lock_disables_reopen_and_roster_mutation_controls():
    public_manager = SimpleNamespace(service_factory=None)
    manager = OrganizerPanelManager(SimpleNamespace(), "sheet", public_manager)
    manager._qualification_installed = True
    manager._qualification_q1_status = "active"

    original_service = organizer_panel.OrganizerService
    original_actions = organizer_panel.RosterActions
    try:
        assert lock_module.install_qualification_roster_lock(manager) is True

        view = manager.view("signup_closed")
        reopen = next(
            child
            for child in view.children
            if getattr(child, "custom_id", None) == "live_arena:organizer:reopen"
        )
        assert reopen.disabled is True
        assert public_manager.service_factory is lock_module.LockedRegistrationService
        assert organizer_panel.OrganizerService is lock_module.LockedOrganizerService
        assert organizer_panel.RosterActions is lock_module.LockedRosterActions

        roster_actions = lock_module.LockedRosterActions(manager)
        selectors = [
            child for child in roster_actions.children if isinstance(child, discord.ui.UserSelect)
        ]
        assert selectors
        assert all(child.disabled for child in selectors)
    finally:
        organizer_panel.OrganizerService = original_service
        organizer_panel.RosterActions = original_actions


def test_proposed_q1_does_not_freeze_reopen_button():
    public_manager = SimpleNamespace(service_factory=None)
    manager = OrganizerPanelManager(SimpleNamespace(), "sheet", public_manager)
    manager._qualification_installed = True
    manager._qualification_q1_status = "proposed"

    original_service = organizer_panel.OrganizerService
    original_actions = organizer_panel.RosterActions
    try:
        lock_module.install_qualification_roster_lock(manager)
        view = manager.view("signup_closed")
        reopen = next(
            child
            for child in view.children
            if getattr(child, "custom_id", None) == "live_arena:organizer:reopen"
        )
        assert reopen.disabled is False
    finally:
        organizer_panel.OrganizerService = original_service
        organizer_panel.RosterActions = original_actions
