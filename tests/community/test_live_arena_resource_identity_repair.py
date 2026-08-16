from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from modules.community.live_arena import discord_resource_identity_repair as repair
from modules.community.live_arena.repository import (
    DISCORD_RESOURCE_HEADERS,
    LiveArenaRepository,
)


def run(awaitable):
    return asyncio.run(awaitable)


def matrix(*rows):
    return [list(DISCORD_RESOURCE_HEADERS), *rows]


def resource_row(
    resource_type,
    resource_key,
    message_id,
    *,
    state="active",
    created="2026-08-16T15:57:15Z",
):
    return [
        "LA-TEST",
        resource_type,
        resource_key,
        "123",
        str(message_id),
        "",
        created,
        created,
        state,
        "preview",
    ]


def repository():
    repo = LiveArenaRepository("sheet")
    repo.config = {"TOURNAMENT_DISCORD_RESOURCES_TAB": "RESOURCES"}
    return repo


@pytest.fixture(autouse=True)
def reset_cache():
    repair._repaired_rows.clear()


@pytest.mark.parametrize(
    ("resource_type", "resource_key"),
    [
        ("swiss_preview", "q2"),
        ("swiss_preview", "q3"),
        ("knockout_preview", "quarterfinal"),
        ("knockout_preview", "semifinal"),
        ("knockout_preview", "final"),
    ],
)
def test_duplicate_preview_lookup_repairs_sheet_and_returns_oldest_active(
    monkeypatch, resource_type, resource_key
):
    rows = matrix(
        resource_row(resource_type, resource_key, "100", created="2026-08-16T15:57:15Z"),
        resource_row(resource_type, resource_key, "101", created="2026-08-16T15:57:41Z"),
    )
    monkeypatch.setattr(repair, "afetch_values", AsyncMock(return_value=rows))
    write_rows = AsyncMock()
    monkeypatch.setattr(repair, "_write_rows", write_rows)

    found = run(
        repair._repair_preview_lookup(
            repository(), "LA-TEST", resource_type, resource_key
        )
    )

    assert found["message_id"] == "100"
    write_rows.assert_awaited_once()
    data = write_rows.await_args.args[2]
    assert data == [
        {
            "range": "'RESOURCES'!A3:J3",
            "values": [[""] * len(DISCORD_RESOURCE_HEADERS)],
        }
    ]


def test_duplicate_preview_prefers_active_row_over_older_retired_row(monkeypatch):
    rows = matrix(
        resource_row("swiss_preview", "q3", "90", state="retired"),
        resource_row("swiss_preview", "q3", "100", state="active"),
    )
    monkeypatch.setattr(repair, "afetch_values", AsyncMock(return_value=rows))
    write_rows = AsyncMock()
    monkeypatch.setattr(repair, "_write_rows", write_rows)

    found = run(
        repair._repair_preview_lookup(
            repository(), "LA-TEST", "swiss_preview", "q3"
        )
    )

    assert found["message_id"] == "100"
    data = write_rows.await_args.args[2]
    assert data[0]["range"] == "'RESOURCES'!A2:J2"


def test_duplicate_preview_upsert_collapses_and_updates_without_calling_strict_upsert(
    monkeypatch,
):
    rows = matrix(
        resource_row("swiss_preview", "q3", "100"),
        resource_row("swiss_preview", "q3", "101"),
    )
    monkeypatch.setattr(repair, "afetch_values", AsyncMock(return_value=rows))
    write_rows = AsyncMock()
    monkeypatch.setattr(repair, "_write_rows", write_rows)
    strict_upsert = AsyncMock()

    run(
        repair._repairing_upsert(
            repository(),
            strict_upsert,
            tournament_id="LA-TEST",
            resource_type="swiss_preview",
            resource_key="q3",
            channel_id="123",
            message_id="100",
            created_at_utc="2026-08-16T15:57:15Z",
            updated_at_utc="2026-08-16T18:50:00Z",
            state="retired",
            notes="Preview retired after official round publication",
        )
    )

    strict_upsert.assert_not_awaited()
    write_rows.assert_awaited_once()
    data = write_rows.await_args.args[2]
    assert data[0]["range"] == "'RESOURCES'!A2:J2"
    assert data[0]["values"][0][4] == "100"
    assert data[0]["values"][0][8] == "retired"
    assert data[1] == {
        "range": "'RESOURCES'!A3:J3",
        "values": [[""] * len(DISCORD_RESOURCE_HEADERS)],
    }


def test_repaired_preview_lookup_is_stable_inside_stale_read_scope(monkeypatch):
    rows = matrix(
        resource_row("swiss_preview", "q3", "100"),
        resource_row("swiss_preview", "q3", "101"),
    )
    fetch = AsyncMock(return_value=rows)
    monkeypatch.setattr(repair, "afetch_values", fetch)
    write_rows = AsyncMock()
    monkeypatch.setattr(repair, "_write_rows", write_rows)
    repo = repository()

    first = run(repair._repair_preview_lookup(repo, "LA-TEST", "swiss_preview", "q3"))
    second = run(repair._repair_preview_lookup(repo, "LA-TEST", "swiss_preview", "q3"))

    assert first["message_id"] == second["message_id"] == "100"
    assert fetch.await_count == 1
    assert write_rows.await_count == 1
