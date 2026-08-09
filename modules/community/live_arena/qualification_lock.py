"""Roster freeze guards once Live Arena Qualification Round 1 has started."""

from __future__ import annotations

from dataclasses import replace

import discord

from modules.community.live_arena import organizer_panel as organizer_panel_module
from modules.community.live_arena.organizer import OrganizerService
from modules.community.live_arena.qualification import (
    QualificationRepository,
    _single_q1_round,
    load_qualification_config,
)
from modules.community.live_arena.registration import RegistrationError, RegistrationService
from modules.community.live_arena.service import _text, load_config

_LOCKED_Q1_STATUSES = {"active", "completed"}
_LOCK_MESSAGE = "Qualification Round 1 has started; the tournament roster is locked."
_ORIGINAL_ROSTER_ACTIONS = organizer_panel_module.RosterActions


async def qualification_roster_locked(sheet_id: str) -> bool:
    """Return whether the active tournament's Q1 has started and freezes its roster."""
    base = await load_config(sheet_id)
    repository = QualificationRepository(sheet_id)
    repository.config = await load_qualification_config(sheet_id)
    round_row = _single_q1_round(
        await repository.rounds(), base["ACTIVE_TOURNAMENT_ID"]
    )
    return bool(
        round_row and _text(round_row.get("status")) in _LOCKED_Q1_STATUSES
    )


class LockedRegistrationService(RegistrationService):
    """Player registration service that respects the active qualification roster."""

    async def get_registration(self, user_id: str):
        snapshot = await super().get_registration(user_id)
        if (
            snapshot.can_withdraw
            and snapshot.tournament_status == "signup_closed"
            and await qualification_roster_locked(self.sheet_id)
        ):
            return replace(snapshot, can_withdraw=False)
        return snapshot

    async def withdraw(self, user_id: str, reason: str = "") -> None:
        if await qualification_roster_locked(self.sheet_id):
            raise RegistrationError(_LOCK_MESSAGE)
        await super().withdraw(user_id, reason)


class LockedOrganizerService(OrganizerService):
    """Organizer mutations that cannot change a roster after Q1 publication."""

    async def transition(self, action, actor_id):
        if action == "reopen" and await qualification_roster_locked(self.sheet_id):
            raise RegistrationError(_LOCK_MESSAGE)
        return await super().transition(action, actor_id)

    async def _participant_change(self, actor_id, target_id, *, restore, member=None):
        if await qualification_roster_locked(self.sheet_id):
            raise RegistrationError(_LOCK_MESSAGE)
        return await super()._participant_change(
            actor_id, target_id, restore=restore, member=member
        )


class LockedRosterActions(_ORIGINAL_ROSTER_ACTIONS):
    """Keep roster inspection available while disabling remove/restore controls."""

    def __init__(self, manager):
        super().__init__(manager)
        if _cached_roster_locked(manager):
            for child in self.children:
                if isinstance(child, discord.ui.UserSelect):
                    child.disabled = True


def install_qualification_roster_lock(manager) -> bool:
    """Install UI and service guards without changing the established PR5 workflow."""
    if getattr(manager, "_qualification_roster_lock_installed", False):
        return True
    if not getattr(manager, "_qualification_installed", False):
        return False

    base_view = manager.view

    def view(status=None):
        result = base_view(status)
        if _cached_roster_locked(manager):
            for child in result.children:
                if getattr(child, "custom_id", None) == "live_arena:organizer:reopen":
                    child.disabled = True
        return result

    manager.view = view
    manager._qualification_roster_lock_installed = True

    # Organizer callbacks resolve these symbols from organizer_panel at runtime.
    # Replacing them here keeps the already-stable PR5 module untouched while
    # ensuring stale buttons/confirmation views still hit a live Sheet guard.
    organizer_panel_module.OrganizerService = LockedOrganizerService
    organizer_panel_module.RosterActions = LockedRosterActions

    # Player entry/self-service already supports a manager-scoped service factory.
    # Production uses the default service, so route it through the locked subclass.
    public_manager = getattr(manager, "public_manager", None)
    if public_manager is not None and getattr(public_manager, "service_factory", None) is None:
        public_manager.service_factory = LockedRegistrationService
    return True


def _cached_roster_locked(manager) -> bool:
    return _text(getattr(manager, "_qualification_q1_status", "")) in _LOCKED_Q1_STATUSES
