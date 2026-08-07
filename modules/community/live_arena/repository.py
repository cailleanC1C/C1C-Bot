"""Async Google Sheets persistence for Live Arena registration state."""

from __future__ import annotations

from collections.abc import Sequence

from shared.sheets.async_core import acall_with_backoff, afetch_values, aget_worksheet

from modules.community.live_arena.service import (
    LiveArenaConfigError,
    _rows,
    load_config,
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

    async def replace_participants(self, rows: Sequence[dict[str, object]]) -> None:
        await self._replace("PARTICIPANTS_TAB", PARTICIPANT_HEADERS, rows)

    async def replace_availability(self, rows: Sequence[dict[str, object]]) -> None:
        await self._replace(
            "PARTICIPANT_AVAILABILITY_TAB", PARTICIPANT_AVAILABILITY_HEADERS, rows
        )

    async def _replace(
        self, key: str, headers: tuple[str, ...], rows: Sequence[dict[str, object]]
    ) -> None:
        worksheet = await aget_worksheet(self.sheet_id, self.config[key])
        await acall_with_backoff(worksheet.batch_clear, [f"A2:{_column(len(headers))}"])
        if rows:
            values = [
                [str(row.get(header, "") or "") for header in headers] for row in rows
            ]
            await acall_with_backoff(
                worksheet.update,
                f"A2:{_column(len(headers))}{len(values) + 1}",
                values,
                value_input_option="RAW",
            )

    async def append_audit(self, row: dict[str, object]) -> None:
        worksheet = await aget_worksheet(self.sheet_id, self.config["AUDIT_LOG_TAB"])
        await acall_with_backoff(
            worksheet.append_row,
            [str(row.get(header, "") or "") for header in AUDIT_LOG_HEADERS],
            value_input_option="RAW",
        )


def _column(number: int) -> str:
    if not 1 <= number <= 26:
        raise LiveArenaConfigError("Live Arena table has unsupported width")
    return chr(64 + number)
