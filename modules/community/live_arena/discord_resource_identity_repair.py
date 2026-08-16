"""Self-heal duplicate Live Arena preview resource registrations.

Preview publication is already race-safe at the Discord-message boundary, but a
previous race can leave more than one Sheet row for the same preview identity.
The core repository is intentionally strict for ordinary resources; this final
layer only repairs organizer preview identities so stale registry corruption can
never block Swiss or knockout progression before the Discord cleanup gets a turn.
"""

from __future__ import annotations

import logging

from shared.sheets.async_core import acall_with_backoff, afetch_values, aget_worksheet

from modules.community.live_arena.repository import (
    DISCORD_RESOURCE_HEADERS,
    LiveArenaRepository,
    _column,
)
from modules.community.live_arena.service import LiveArenaConfigError, _rows, _text

log = logging.getLogger("c1c.community.live_arena.discord_resource_identity_repair")
_installed = False
_PREVIEW_RESOURCE_TYPES = {"swiss_preview", "knockout_preview"}
_repaired_rows: dict[tuple[str, str, str, str], dict[str, object]] = {}


def _identity(repository, tournament_id: str, resource_type: str, resource_key: str):
    return (
        str(repository.sheet_id or "").strip(),
        _text(tournament_id),
        _text(resource_type),
        _text(resource_key),
    )


def _matching_rows(matrix, tab: str, tournament_id: str, resource_type: str, resource_key: str):
    # Preserve the repository's normal table-contract validation before attempting
    # any repair. We only relax duplicate identity, never malformed headers.
    _rows(matrix, DISCORD_RESOURCE_HEADERS, tab)
    headers = tuple(_text(value) for value in matrix[0])
    columns = {header: headers.index(header) for header in DISCORD_RESOURCE_HEADERS}
    matches: list[tuple[int, dict[str, object]]] = []
    for row_number, raw_row in enumerate(matrix[1:], 2):
        padded = list(raw_row) + [""] * max(0, len(headers) - len(raw_row))
        if (
            _text(padded[columns["tournament_id"]]) == _text(tournament_id)
            and _text(padded[columns["resource_type"]]) == _text(resource_type)
            and _text(padded[columns["resource_key"]]) == _text(resource_key)
        ):
            matches.append(
                (
                    row_number,
                    {header: padded[index] for index, header in enumerate(headers)},
                )
            )
    return headers, matches


def _canonical_match(matches):
    """Prefer an active row, then the oldest physical registration."""
    active = [item for item in matches if _text(item[1].get("state")) == "active"]
    return min(active or matches, key=lambda item: item[0])


async def _write_rows(repository, tab: str, data) -> None:
    worksheet = await aget_worksheet(repository.sheet_id, tab)
    await acall_with_backoff(
        worksheet.spreadsheet.values_batch_update,
        body={"valueInputOption": "RAW", "data": data},
    )


async def _repair_preview_lookup(
    repository,
    tournament_id: str,
    resource_type: str,
    resource_key: str,
):
    identity = _identity(repository, tournament_id, resource_type, resource_key)
    cached = _repaired_rows.get(identity)
    if cached is not None:
        return dict(cached)

    tab = repository._resource_tab()
    matrix = await afetch_values(repository.sheet_id, tab) or []
    headers, matches = _matching_rows(
        matrix, tab, tournament_id, resource_type, resource_key
    )
    if not matches:
        return None
    if len(matches) == 1:
        return dict(matches[0][1])

    canonical_row_number, canonical = _canonical_match(matches)
    duplicate_rows = [row_number for row_number, _ in matches if row_number != canonical_row_number]
    escaped_tab = tab.replace("'", "''")
    blank = [""] * len(headers)
    data = [
        {
            "range": f"'{escaped_tab}'!A{row_number}:{_column(len(headers))}{row_number}",
            "values": [blank],
        }
        for row_number in duplicate_rows
    ]
    await _write_rows(repository, tab, data)
    _repaired_rows[identity] = dict(canonical)
    log.warning(
        "Live Arena duplicate preview resource rows repaired • tournament=%s • type=%s • key=%s • kept_row=%s • cleared_rows=%s",
        _text(tournament_id),
        _text(resource_type),
        _text(resource_key),
        canonical_row_number,
        ",".join(str(value) for value in duplicate_rows),
    )
    return dict(canonical)


def _resource_values(
    *,
    tournament_id: str,
    resource_type: str,
    resource_key: str,
    channel_id: str,
    message_id: str,
    thread_id: str,
    created_at_utc: str,
    updated_at_utc: str,
    state: str,
    notes: str,
):
    return {
        "tournament_id": _text(tournament_id),
        "resource_type": _text(resource_type),
        "resource_key": _text(resource_key),
        "channel_id": _text(channel_id),
        "message_id": _text(message_id),
        "thread_id": _text(thread_id),
        "created_at_utc": _text(created_at_utc),
        "updated_at_utc": _text(updated_at_utc),
        "state": _text(state),
        "notes": _text(notes),
    }


async def _repairing_upsert(
    repository,
    original_upsert,
    *,
    tournament_id: str,
    resource_type: str,
    resource_key: str = "main",
    channel_id: str = "",
    message_id: str = "",
    thread_id: str = "",
    created_at_utc: str = "",
    updated_at_utc: str = "",
    state: str = "active",
    notes: str = "",
) -> None:
    if _text(resource_type) not in _PREVIEW_RESOURCE_TYPES:
        return await original_upsert(
            repository,
            tournament_id=tournament_id,
            resource_type=resource_type,
            resource_key=resource_key,
            channel_id=channel_id,
            message_id=message_id,
            thread_id=thread_id,
            created_at_utc=created_at_utc,
            updated_at_utc=updated_at_utc,
            state=state,
            notes=notes,
        )
    if state not in {"active", "retired"}:
        raise LiveArenaConfigError(
            "TOURNAMENT_DISCORD_RESOURCES.state must be active or retired"
        )

    tab = repository._resource_tab()
    matrix = await afetch_values(repository.sheet_id, tab) or []
    headers, matches = _matching_rows(
        matrix, tab, tournament_id, resource_type, resource_key
    )
    identity = _identity(repository, tournament_id, resource_type, resource_key)
    values = _resource_values(
        tournament_id=tournament_id,
        resource_type=resource_type,
        resource_key=resource_key,
        channel_id=channel_id,
        message_id=message_id,
        thread_id=thread_id,
        created_at_utc=created_at_utc,
        updated_at_utc=updated_at_utc,
        state=state,
        notes=notes,
    )

    if len(matches) <= 1:
        await original_upsert(
            repository,
            tournament_id=tournament_id,
            resource_type=resource_type,
            resource_key=resource_key,
            channel_id=channel_id,
            message_id=message_id,
            thread_id=thread_id,
            created_at_utc=created_at_utc,
            updated_at_utc=updated_at_utc,
            state=state,
            notes=notes,
        )
        if identity in _repaired_rows:
            _repaired_rows[identity] = dict(values)
        return

    # A duplicate preview must not reach the original strict upsert: a surrounding
    # sheet_read_scope may keep returning the same stale duplicate matrix after the
    # repair write. Collapse and update the canonical row in one batch instead.
    canonical_row_number, _canonical = _canonical_match(matches)
    duplicate_rows = [row_number for row_number, _ in matches if row_number != canonical_row_number]
    escaped_tab = tab.replace("'", "''")
    data = [
        {
            "range": f"'{escaped_tab}'!A{canonical_row_number}:{_column(len(headers))}{canonical_row_number}",
            "values": [[values.get(header, "") for header in headers]],
        }
    ]
    blank = [""] * len(headers)
    data.extend(
        {
            "range": f"'{escaped_tab}'!A{row_number}:{_column(len(headers))}{row_number}",
            "values": [blank],
        }
        for row_number in duplicate_rows
    )
    await _write_rows(repository, tab, data)
    _repaired_rows[identity] = dict(values)
    log.warning(
        "Live Arena duplicate preview resource rows collapsed during upsert • tournament=%s • type=%s • key=%s • kept_row=%s • cleared_rows=%s",
        _text(tournament_id),
        _text(resource_type),
        _text(resource_key),
        canonical_row_number,
        ",".join(str(value) for value in duplicate_rows),
    )


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    original_resource = LiveArenaRepository.discord_resource
    original_upsert = LiveArenaRepository.upsert_discord_resource

    async def discord_resource(self, tournament_id: str, resource_type: str, resource_key: str = "main"):
        if _text(resource_type) not in _PREVIEW_RESOURCE_TYPES:
            return await original_resource(self, tournament_id, resource_type, resource_key)
        return await _repair_preview_lookup(
            self, tournament_id, resource_type, resource_key
        )

    async def upsert_discord_resource(self, **kwargs):
        return await _repairing_upsert(self, original_upsert, **kwargs)

    LiveArenaRepository.discord_resource = discord_resource
    LiveArenaRepository.upsert_discord_resource = upsert_discord_resource
