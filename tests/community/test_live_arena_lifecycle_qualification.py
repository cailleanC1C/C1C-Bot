from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from modules.community.live_arena.tournament_lifecycle import LifecycleQualificationService


def run(awaitable):
    return asyncio.run(awaitable)


def test_approved_q1_promotes_tournament_instance_to_active():
    service = object.__new__(LifecycleQualificationService)
    service.sheet_id = "sheet"
    service.registration_repository = object()
    service.clock = lambda: None

    with (
        patch(
            "modules.community.live_arena.qualification.QualificationService.approve_draw",
            AsyncMock(return_value="snapshot"),
        ) as approve,
        patch(
            "modules.community.live_arena.tournament_lifecycle.OrganizerService"
        ) as organizer_cls,
    ):
        lifecycle = organizer_cls.return_value
        lifecycle.transition = AsyncMock()
        result = run(service.approve_draw("42"))

    assert result == "snapshot"
    approve.assert_awaited_once_with("42")
    organizer_cls.assert_called_once_with(
        "sheet", repository=service.registration_repository, clock=service.clock
    )
    lifecycle.transition.assert_awaited_once_with("activate", "42")
