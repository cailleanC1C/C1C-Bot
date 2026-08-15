"""Persistence bridge for Q2/Q3 Victory Ledger overview messages.

The shared Discord round renderer expects its qualification service to expose
``record_overview_message_id``. Q1's ``QualificationService`` provides that
method, while ``SwissQualificationService`` historically did not. As a result,
Q2/Q3 could send an overview and then delete it again when persistence failed.
"""

from __future__ import annotations

from modules.community.live_arena.qualification import QualificationService
from modules.community.live_arena.swiss import SwissQualificationService

_installed = False


def install() -> None:
    """Give Swiss qualification the same overview-ID persistence contract as Q1."""
    global _installed
    if _installed:
        return
    _installed = True

    if callable(getattr(SwissQualificationService, "record_overview_message_id", None)):
        return

    async def record_overview_message_id(self, round_id: str, message_id: str):
        helper = QualificationService(
            self.sheet_id,
            registration_repository=self.registration_repository,
            qualification_repository=self.repository,
            clock=self.clock,
        )
        return await helper.record_overview_message_id(round_id, message_id)

    SwissQualificationService.record_overview_message_id = record_overview_message_id
