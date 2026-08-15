from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from modules.community.live_arena.qualification import QualificationService
from modules.community.live_arena.swiss import SwissQualificationService


def run(awaitable):
    return asyncio.run(awaitable)


def test_swiss_service_persists_victory_ledger_overview_id_via_q1_contract(monkeypatch):
    registration_repository = object()
    qualification_repository = object()
    clock = lambda: datetime(2026, 8, 15, tzinfo=UTC)
    captured = {}

    async def fake_record(self, round_id: str, message_id: str):
        captured.update(
            sheet_id=self.sheet_id,
            registration_repository=self.registration_repository,
            qualification_repository=self.repository,
            clock=self.clock,
            round_id=round_id,
            message_id=message_id,
        )
        return {"round_id": round_id, "overview_message_id": message_id}

    monkeypatch.setattr(
        QualificationService,
        "record_overview_message_id",
        fake_record,
    )

    service = SwissQualificationService(
        "sheet-live-arena",
        registration_repository=registration_repository,
        qualification_repository=qualification_repository,
        clock=clock,
    )

    assert callable(getattr(service, "record_overview_message_id", None))

    result = run(service.record_overview_message_id("C1C-Q2", "123456789"))

    assert result == {
        "round_id": "C1C-Q2",
        "overview_message_id": "123456789",
    }
    assert captured == {
        "sheet_id": "sheet-live-arena",
        "registration_repository": registration_repository,
        "qualification_repository": qualification_repository,
        "clock": clock,
        "round_id": "C1C-Q2",
        "message_id": "123456789",
    }
