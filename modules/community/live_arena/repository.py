"""Async Google Sheets persistence for Live Arena state."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from shared.sheets.async_core import acall_with_backoff, afetch_values, aget_worksheet

from modules.community.live_arena.service import (
    LiveArenaConfigError,
    _rows,
    _text,
    load_config,
    TOURNAMENT_HEADERS,
)

PARTICIPANT_HEADERS = (
    "tournament_id",
    "discord_user_id",
    "display_name_at_signup",
    "clan_tag_at_signup",
    "timezone",
    "status",
    "signed_up_at_utc",
    "confirmed_at_utc",
    "withdrawn_at_utc",
    "withdrawal_reason",
    "updated_at_utc",
    "notes",
)
PARTICIPANT_AVAILABILITY_HEADERS = (
    "tournament_id",
    "discord_user_id",
    "slot_id",
    "created_at_utc",
    "updated_at_utc",
    "notes",
)
AUDIT_LOG_HEADERS = (
    "event_id",
    "tournament_id",
    "event_type",
    "actor_discord_user_id",
    "target_discord_user_id",
    "details",
    "created_at_utc",
)
DISCORD_RESOURCE_HEADERS = (
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


class LiveArenaRepository:
    """Workbook adapter whose table names always come from CONFIG."""

    def __init__(self, sheet_id: str) -> None:
        self.sheet_id = sheet_id
        self.config: dict[str, str] = {}

    async def initialize(self) -> None:
        self.config = await load_config(self.sheet_id)
        await self._read("PARTICIPANTS_TAB", PARTICIPANT_HEADERS)
        await self._read(
            "PARTICIPANT_AVAILABILITY_TAB", PARTICIPANT_AVAILABILITY_HEADERS
        )
        await self._read("AUDIT_LOG_TAB", AUDIT_LOG_HEADERS)
        await self._read("TOURNAMENT_DISCORD_RESOURCES_TAB", DISCORD_RESOURCE_HEADERS)

    async def _read(
        self, key: str, headers: tuple[str, ...]
    ) -> list[dict[str, object]]:
        tab = self.config[key]
        return _rows(await afetch_values(self.sheet_id, tab) or [], headers, tab)

    async def participants(self) -> list[dict[str, object]]:
        return await self._read("PARTICIPANTS_TAB", PARTICIPANT_HEADERS)

    async def availability(self) -> list[dict[str, object]]:
        return await self._read(
            "PARTICIPANT_AVAILABILITY_TAB", PARTICIPANT_AVAILABILITY_HEADERS
        )

    async def discord_resources(self) -> list[dict[str, object]]:
        return await self._read(
            "TOURNAMENT_DISCORD_RESOURCES_TAB", DISCORD_RESOURCE_HEADERS
        )

    async def discord_resource(
        self, tournament_id: str, resource_type: str, resource_key: str = "main"
    ) -> dict[str, object] | None:
        matches = [
            row
            for row in await self.discord_resources()
            if _text(row["tournament_id"]) == _text(tournament_id)
            and _text(row["resource_type"]) == _text(resource_type)
            and _text(row["resource_key"]) == _text(resource_key)
        ]
        if len(matches) > 1:
            raise LiveArenaConfigError(
                "TOURNAMENT_DISCORD_RESOURCES: resource must occur at most once for "
                f"{tournament_id}/{resource_type}/{resource_key}"
            )
        return matches[0] if matches else None

    async def upsert_discord_resource(
        self,
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
        """Insert or update one tournament-owned Discord resource row."""
        if state not in {"active", "retired"}:
            raise LiveArenaConfigError(
                "TOURNAMENT_DISCORD_RESOURCES.state must be active or retired"
            )
        tab = self.config["TOURNAMENT_DISCORD_RESOURCES_TAB"]
        matrix = await afetch_values(self.sheet_id, tab) or []
        rows = _rows(matrix, DISCORD_RESOURCE_HEADERS, tab)
        matching_indexes = [
            index
            for index, row in enumerate(rows, 2)
            if _text(row["tournament_id"]) == _text(tournament_id)
            and _text(row["resource_type"]) == _text(resource_type)
            and _text(row["resource_key"]) == _text(resource_key)
        ]
        if len(matching_indexes) > 1:
            raise LiveArenaConfigError(
                "TOURNAMENT_DISCORD_RESOURCES: duplicate resource identity"
            )

        worksheet = await aget_worksheet(self.sheet_id, tab)
        values = {
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
        if matching_indexes:
            row_number = matching_indexes[0]
            data = []
            for header, value in values.items():
                column = DISCORD_RESOURCE_HEADERS.index(header) + 1
                data.append(
                    {
                        "range": f"'{tab.replace(chr(39), chr(39) * 2)}'!{_column(column)}{row_number}",
                        "values": [[value]],
                    }
                )
            await acall_with_backoff(
                worksheet.spreadsheet.values_batch_update,
                body={"valueInputOption": "RAW", "data": data},
            )
            return

        await acall_with_backoff(
            worksheet.append_row,
            [values[header] for header in DISCORD_RESOURCE_HEADERS],
            value_input_option="RAW",
        )

    async def retire_discord_resources(
        self, tournament_id: str, *, updated_at_utc: str
    ) -> None:
        """Retire all active tournament-level resource rows without deleting history."""
        resources = await self.discord_resources()
        for row in resources:
            if (
                _text(row["tournament_id"]) == _text(tournament_id)
                and _text(row["state"]) == "active"
            ):
                await self.upsert_discord_resource(
                    tournament_id=_text(row["tournament_id"]),
                    resource_type=_text(row["resource_type"]),
                    resource_key=_text(row["resource_key"]) or "main",
                    channel_id=_text(row["channel_id"]),
                    message_id=_text(row["message_id"]),
                    thread_id=_text(row["thread_id"]),
                    created_at_utc=_text(row["created_at_utc"]),
                    updated_at_utc=updated_at_utc,
                    state="retired",
                    notes=_text(row["notes"]),
                )

    async def persist_core_state(
        self,
        participants: Sequence[dict[str, object]],
        availability: Sequence[dict[str, object]],
        *,
        previous_participants: Sequence[dict[str, object]],
        previous_availability: Sequence[dict[str, object]],
    ) -> None:
        """Persist both core tables in one values batch, compensating ambiguity locally."""
        await self._persist_tables(
            (
                (
                    "PARTICIPANTS_TAB",
                    PARTICIPANT_HEADERS,
                    participants,
                    previous_participants,
                ),
                (
                    "PARTICIPANT_AVAILABILITY_TAB",
                    PARTICIPANT_AVAILABILITY_HEADERS,
                    availability,
                    previous_availability,
                ),
            )
        )

    async def persist_participants(
        self,
        participants: Sequence[dict[str, object]],
        *,
        previous_participants: Sequence[dict[str, object]],
    ) -> None:
        """Persist a participant-only mutation, including withdrawal."""
        await self._persist_tables(
            (
                (
                    "PARTICIPANTS_TAB",
                    PARTICIPANT_HEADERS,
                    participants,
                    previous_participants,
                ),
            )
        )

    async def _persist_tables(self, tables) -> None:
        worksheet = await aget_worksheet(self.sheet_id, self.config["PARTICIPANTS_TAB"])
        spreadsheet = worksheet.spreadsheet
        update = self._batch_body(tables, use_previous=False)
        rollback = self._batch_body(tables, use_previous=True)
        try:
            await acall_with_backoff(spreadsheet.values_batch_update, body=update)
        except Exception as write_error:
            try:
                await acall_with_backoff(spreadsheet.values_batch_update, body=rollback)
            except Exception as rollback_error:
                raise CoreStatePersistenceError(
                    write_error=write_error, rollback_error=rollback_error
                ) from rollback_error
            raise

    def _batch_body(self, tables, *, use_previous: bool) -> dict[str, object]:
        data = []
        for key, headers, current, previous in tables:
            rows = previous if use_previous else current
            row_count = max(len(current), len(previous), 1)
            values = [
                [str(row.get(header, "") or "") for header in headers] for row in rows
            ]
            values.extend([[""] * len(headers) for _ in range(row_count - len(values))])
            tab = self.config[key].replace("'", "''")
            data.append(
                {
                    "range": f"'{tab}'!A2:{_column(len(headers))}{row_count + 1}",
                    "values": values,
                }
            )
        return {"valueInputOption": "RAW", "data": data}

    async def append_audit(self, row: dict[str, object]) -> None:
        worksheet = await aget_worksheet(self.sheet_id, self.config["AUDIT_LOG_TAB"])
        await acall_with_backoff(
            worksheet.append_row,
            [str(row.get(header, "") or "") for header in AUDIT_LOG_HEADERS],
            value_input_option="RAW",
        )

    async def update_tournament_cells(
        self, row_number: int, values: dict[str, object]
    ) -> None:
        """Atomically batch-update only named cells in the selected TOURNAMENTS row."""
        worksheet = await aget_worksheet(self.sheet_id, self.config["TOURNAMENTS_TAB"])
        tab = self.config["TOURNAMENTS_TAB"].replace("'", "''")
        data = []
        for header, value in values.items():
            if header not in TOURNAMENT_HEADERS:
                raise LiveArenaConfigError(
                    f"TOURNAMENTS: unsupported update header {header}"
                )
            column = TOURNAMENT_HEADERS.index(header) + 1
            cell = f"{_column(column)}{row_number}"
            data.append({"range": f"'{tab}'!{cell}", "values": [[str(value)] ]})
        await acall_with_backoff(
            worksheet.spreadsheet.values_batch_update,
            body={"valueInputOption": "RAW", "data": data},
        )


def _column(number: int) -> str:
    if not 1 <= number <= 26:
        raise LiveArenaConfigError("Live Arena table has unsupported width")
    return chr(64 + number)


@dataclass
class CoreStatePersistenceError(RuntimeError):
    """A core write failed and its repository-local compensation also failed."""

    write_error: Exception
    rollback_error: Exception

    def __str__(self) -> str:
        return (
            "Live Arena core persistence and compensation both failed: "
            f"write={self.write_error!r}; rollback={self.rollback_error!r}"
        )
