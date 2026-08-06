"""Async, header-addressed Sheets repository with compensating writes."""

from __future__ import annotations
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Iterable
from shared.sheets.async_core import (
    acall_with_backoff,
    afetch_records,
    asheets_read,
    aget_worksheet,
)
from .config import FeatureConfig, parse_system_config
from .models import SchemaError, norm


class LiveArenaRepository:
    def __init__(self, sheet_id: str):
        self.sheet_id = sheet_id
        self.config: FeatureConfig | None = None

    async def load_config(self):
        self.config = parse_system_config(
            await asheets_read(self.sheet_id, "'System_Config'!A:Z"), self.sheet_id
        )
        return self.config

    async def rows(self, table: str, required: Iterable[str] = ()):
        if not self.config:
            await self.load_config()
        rows = await afetch_records(self.sheet_id, self.config.tabs[table])
        normalized = [{norm(k): v for k, v in r.items()} for r in rows]
        for index, row in enumerate(normalized, 2):
            row.setdefault("_row_number", index)
        present = {norm(k) for r in normalized for k in r}
        missing = {norm(x) for x in required} - present
        if missing:
            raise SchemaError(
                f"{table} is missing required headers: {', '.join(sorted(missing))}"
            )
        return normalized

    async def _values(self, table):
        ws = await aget_worksheet(self.sheet_id, self.config.tabs[table])
        values = await acall_with_backoff(ws.get_all_values)
        return ws, values

    async def replace_row(self, table: str, row_number: int, changes: dict[str, Any]):
        ws, values = await self._values(table)
        headers = [norm(x) for x in values[0]]
        missing = set(map(norm, changes)) - set(headers)
        if missing:
            raise SchemaError(
                f"{table} is missing required headers: {', '.join(sorted(missing))}"
            )
        original = list(values[row_number - 1])
        original += [""] * (len(headers) - len(original))
        updated = list(original)
        for k, v in changes.items():
            updated[headers.index(norm(k))] = v
        await acall_with_backoff(
            ws.update,
            f"A{row_number}:{self._column(len(headers))}{row_number}",
            [updated],
            value_input_option="RAW",
        )
        return original

    async def append(self, table, changes):
        ws, values = await self._values(table)
        headers = [norm(x) for x in values[0]]
        missing = set(map(norm, changes)) - set(headers)
        if missing:
            raise SchemaError(
                f"{table} is missing required headers: {', '.join(sorted(missing))}"
            )
        await acall_with_backoff(
            ws.append_row,
            [changes.get(h, "") for h in headers],
            value_input_option="RAW",
        )

    async def audit(
        self,
        event_type,
        tournament_id,
        actor,
        entity_type,
        entity_id,
        old,
        new,
        notes="",
    ):
        await self.append(
            "audit_log",
            {
                "event_id": str(uuid.uuid4()),
                "tournament_id": tournament_id,
                "event_type": event_type,
                "actor_discord_user_id": str(actor),
                "entity_type": entity_type,
                "entity_id": str(entity_id),
                "old_value_json": json.dumps(old, default=str),
                "new_value_json": json.dumps(new, default=str),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "notes": notes,
            },
        )

    @staticmethod
    def _column(number):
        out = ""
        while number:
            number, rem = divmod(number - 1, 26)
            out = chr(65 + rem) + out
        return out

    async def replace_availability(self, tournament_id, user_id, slot_ids, now):
        """Atomically-ish replace rows: update old rows in place, blank surplus, append remainder; restore snapshot on failure."""
        ws, values = await self._values("participant_availability")
        headers = [norm(x) for x in values[0]]
        required = {
            "tournament_id",
            "discord_user_id",
            "slot_id",
            "preference",
            "created_at",
            "updated_at",
        }
        if required - set(headers):
            raise SchemaError(
                "participant_availability is missing required headers: "
                + ", ".join(sorted(required - set(headers)))
            )
        matches = []
        for index, row in enumerate(values[1:], 2):
            record = {
                headers[i]: row[i] if i < len(row) else "" for i in range(len(headers))
            }
            if (
                str(record["tournament_id"]) == tournament_id
                and str(record["discord_user_id"]) == user_id
            ):
                matches.append((index, list(row)))
        try:
            for pos, slot in enumerate(slot_ids):
                record = {
                    "tournament_id": tournament_id,
                    "discord_user_id": user_id,
                    "slot_id": slot,
                    "preference": "available",
                    "created_at": now,
                    "updated_at": now,
                }
                row = [record.get(h, "") for h in headers]
                if pos < len(matches):
                    await acall_with_backoff(
                        ws.update,
                        f"A{matches[pos][0]}:{self._column(len(headers))}{matches[pos][0]}",
                        [row],
                        value_input_option="RAW",
                    )
                else:
                    await acall_with_backoff(
                        ws.append_row, row, value_input_option="RAW"
                    )
            for row_number, _ in matches[len(slot_ids) :]:
                await acall_with_backoff(
                    ws.update,
                    f"A{row_number}:{self._column(len(headers))}{row_number}",
                    [[""] * len(headers)],
                    value_input_option="RAW",
                )
        except Exception:
            for row_number, old in matches:
                await acall_with_backoff(
                    ws.update,
                    f"A{row_number}:{self._column(len(headers))}{row_number}",
                    [old + [""] * (len(headers) - len(old))],
                    value_input_option="RAW",
                )
            appended = max(0, len(slot_ids) - len(matches))
            for row_number in range(len(values) + 1, len(values) + appended + 1):
                await acall_with_backoff(
                    ws.update,
                    f"A{row_number}:{self._column(len(headers))}{row_number}",
                    [[""] * len(headers)],
                    value_input_option="RAW",
                )
            raise
