from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from modules.community.live_arena.organizer import OrganizerService
from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.repository import DISCORD_RESOURCE_HEADERS
from modules.community.live_arena.service import TOURNAMENT_HEADERS
from modules.community.live_arena.tournament_lifecycle import install_tournament_lifecycle


def run(awaitable):
    return asyncio.run(awaitable)


def test_tournament_contract_has_human_identity_and_lifecycle_fields():
    assert TOURNAMENT_HEADERS == (
        "tournament_id",
        "tournament_name",
        "status",
        "eligibility_scope",
        "min_participants",
        "max_participants",
        "signup_opens_at_utc",
        "signup_closes_at_utc",
        "notes",
        "tournament_short_name",
        "created_at_utc",
        "completed_at_utc",
        "archived_at_utc",
        "timezone",
    )
    assert DISCORD_RESOURCE_HEADERS == (
        "tournament_id",
        "resource_type",
        "resource_key",
        "channel_id",
        "message_id",
        "thread_id",
        "created_at_utc",
        "updated_at_utc",
        "state",
        "notes",
    )


class _Repository:
    def __init__(self):
        self.config = {
            "TOURNAMENTS_TAB": "TOURNAMENTS",
            "ELIGIBLE_CLANS_TAB": "ELIGIBLE_CLANS",
            "AVAILABILITY_SLOTS_TAB": "AVAILABILITY_SLOTS",
        }
        self.updated = []
        self.audits = []

    async def initialize(self):
        return None

    async def participants(self):
        return [
            {"tournament_id": "cup", "status": "confirmed"},
            {"tournament_id": "cup", "status": "confirmed"},
        ]

    async def update_tournament_cells(self, row_number, values):
        self.updated.append((row_number, values))

    async def append_audit(self, row):
        self.audits.append(row)


def _service(status):
    repository = _Repository()
    service = OrganizerService(
        "sheet",
        repository=repository,
        clock=lambda: datetime(2026, 8, 10, 10, 0, tzinfo=UTC),
    )
    tournament = {
        "tournament_id": "cup",
        "status": status,
        "signup_closes_at_utc": "2026-08-20T10:00:00Z",
    }
    service.context = AsyncMock(return_value=({}, (7, tournament), [], []))
    return service, repository


def test_complete_and_archive_write_timestamps_without_deleting_history():
    complete, repository = _service("active")
    with patch(
        "modules.community.live_arena.organizer.load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": "cup"}),
    ):
        run(complete.transition("complete", "42"))
    assert repository.updated == [
        (
            7,
            {
                "status": "completed",
                "completed_at_utc": "2026-08-10T10:00:00Z",
            },
        )
    ]
    assert repository.audits[-1]["event_type"] == "tournament_completed"

    archive, repository = _service("completed")
    with patch(
        "modules.community.live_arena.organizer.load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": "cup"}),
    ):
        run(archive.transition("archive", "42"))
    assert repository.updated == [
        (
            7,
            {
                "status": "archived",
                "archived_at_utc": "2026-08-10T10:00:00Z",
            },
        )
    ]
    assert repository.audits[-1]["event_type"] == "tournament_archived"


def test_duplicate_completion_is_rejected():
    service, _ = _service("completed")
    with patch(
        "modules.community.live_arena.organizer.load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": "cup"}),
    ):
        with pytest.raises(RegistrationError, match="already been completed"):
            run(service.transition("complete", "42"))


def test_lifecycle_controls_follow_tournament_status():
    class Manager:
        def __init__(self):
            self.sheet_id = "sheet"
            self._lock = asyncio.Lock()

        def view(self, _status=None):
            return discord.ui.View(timeout=None)

    manager = Manager()
    assert install_tournament_lifecycle(manager) is True

    active = {item.label: item for item in manager.view("active").children}
    assert active["Complete Tournament"].disabled is False
    assert active["Archive Tournament"].disabled is True

    completed = {item.label: item for item in manager.view("completed").children}
    assert completed["Complete Tournament"].disabled is True
    assert completed["Archive Tournament"].disabled is False

    archived = {item.label: item for item in manager.view("archived").children}
    assert all(item.disabled for item in archived.values())
