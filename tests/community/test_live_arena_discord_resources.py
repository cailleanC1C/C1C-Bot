from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from modules.community.live_arena.repository import (
    DISCORD_RESOURCE_HEADERS,
    LiveArenaRepository,
)
from modules.community.live_arena.service import LiveArenaConfigError


def run(awaitable):
    return asyncio.run(awaitable)


def _matrix(*rows):
    return [list(DISCORD_RESOURCE_HEADERS), *rows]


def test_resource_lookup_is_scoped_by_tournament_type_and_key():
    repository = LiveArenaRepository("sheet")
    repository.config = {"TOURNAMENT_DISCORD_RESOURCES_TAB": "RESOURCES"}
    matrix = _matrix(
        ["arena-1", "signup_panel", "main", "10", "100", "", "", "", "active", ""],
        ["arena-2", "signup_panel", "main", "10", "200", "", "", "", "active", ""],
        ["arena-1", "organizer_panel", "main", "20", "300", "", "", "", "active", ""],
    )
    with patch(
        "modules.community.live_arena.repository.afetch_values",
        AsyncMock(return_value=matrix),
    ):
        row = run(repository.discord_resource("arena-1", "signup_panel", "main"))
    assert row["message_id"] == "100"


def test_duplicate_resource_identity_is_rejected():
    repository = LiveArenaRepository("sheet")
    repository.config = {"TOURNAMENT_DISCORD_RESOURCES_TAB": "RESOURCES"}
    duplicate = ["arena-1", "signup_panel", "main", "10", "100", "", "", "", "active", ""]
    with patch(
        "modules.community.live_arena.repository.afetch_values",
        AsyncMock(return_value=_matrix(duplicate, duplicate)),
    ):
        with pytest.raises(LiveArenaConfigError, match="at most once"):
            run(repository.discord_resource("arena-1", "signup_panel", "main"))
