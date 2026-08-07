"""Async Google Sheets persistence for Live Arena registration state."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from shared.sheets.async_core import acall_with_backoff, afetch_values, aget_worksheet

from modules.community.live_arena.service import (
    LiveArenaConfigError,
    _rows,
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
        """Atomically batch-update only named cells in the frozen TOURNAMENTS row."""
        worksheet = await aget_worksheet(self.sheet_id, self.config["TOURNAMENTS_TAB"])
        tab = self.config["TOURNAMENTS_TAB"].replace("'", "''")
        data = []
        for header, value in values.items():
            column = TOURNAMENT_HEADERS.index(header) + 1
            cell = f"{_column(column)}{row_number}"
            data.append({"range": f"'{tab}'!{cell}", "values": [[str(value)]]})
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
