"""Sheet adapter for clan seat reservations."""

from __future__ import annotations

import datetime as dt
import inspect
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, List, Optional, Protocol, Sequence

from shared.sheets import async_core
from shared.sheets import recruitment

log = logging.getLogger(__name__)


class SupportsMemberLookup(Protocol):
    """Minimal protocol for Discord guild lookups."""

    def get_member(self, member_id: int) -> Any:
        ...


ResolveUserFn = Callable[[int], Awaitable[str | None] | str | None]


RESERVATIONS_HEADERS: list[str] = [
    "thread_id",
    "ticket_user_id",
    "recruiter_id",
    "clan_tag",
    "reserved_until",
    "created_at",
    "status",
    "notes",
    "username_snapshot",
]

(
    THREAD_ID_COL,
    TICKET_USER_ID_COL,
    RECRUITER_ID_COL,
    CLAN_TAG_COL,
    RESERVED_UNTIL_COL,
    CREATED_AT_COL,
    STATUS_COL,
    NOTES_COL,
    USERNAME_SNAPSHOT_COL,
) = range(len(RESERVATIONS_HEADERS))

STATUS_COLUMN_INDEX = STATUS_COL


class ReservationSchemaError(RuntimeError):
    """Raised when the reservations worksheet header does not match the schema."""


@dataclass(slots=True)
class ReservationRow:
    """Normalized representation of a reservation ledger row."""

    row_number: int
    thread_id: str
    ticket_user_id: Optional[int]
    recruiter_id: Optional[int]
    clan_tag: str
    reserved_until: Optional[dt.date]
    created_at: Optional[dt.datetime]
    status: str
    notes: str
    username_snapshot: Optional[str]
    raw: Sequence[str]

    @property
    def normalized_clan_tag(self) -> str:
        return _normalize_tag(self.clan_tag)

    @property
    def is_active(self) -> bool:
        return _normalize_status(self.status) == "active"


@dataclass(slots=True)
class ReservationLedger:
    """Container for parsed reservation rows and header metadata."""

    rows: list[ReservationRow]
    status_index: int

    def status_column(self) -> int | None:
        return self.status_index


async def append_reservation_row(row_values: Sequence[Any]) -> None:
    """Append ``row_values`` to the reservations worksheet."""

    if len(row_values) != len(RESERVATIONS_HEADERS):
        raise ValueError(
            "reservation row must contain exactly"
            f" {len(RESERVATIONS_HEADERS)} values"
        )

    recruitment.ensure_service_account_credentials()
    sheet_id = recruitment.get_recruitment_sheet_id()
    tab_name = await recruitment.get_reservations_tab_name_async()
    worksheet = await async_core.aget_worksheet(sheet_id, tab_name)
    payload = [str(value) if value is not None else "" for value in row_values]
    await async_core.acall_with_backoff(
        worksheet.append_row,
        payload,
        value_input_option="RAW",
    )


async def load_reservation_ledger() -> ReservationLedger:
    """Return the full reservations ledger with header metadata."""

    matrix = await _fetch_reservations_matrix()
    if not matrix:
        return ReservationLedger(rows=[], status_index=STATUS_COLUMN_INDEX)

    header = [_normalize_schema_cell(cell) for cell in matrix[0]]
    if header != RESERVATIONS_HEADERS:
        tab_name = await recruitment.get_reservations_tab_name_async()
        log.error(
            "reservations header mismatch",
            extra={
                "tab": tab_name,
                "expected": RESERVATIONS_HEADERS,
                "actual": header,
            },
        )
        raise ReservationSchemaError(
            "RESERVATIONS_TAB header mismatch."
            " Expected exact match to RESERVATIONS_HEADERS."
        )

    records: list[ReservationRow] = []
    for offset, raw in enumerate(matrix[1:], start=2):
        if not _row_has_content(raw):
            continue
        record = ReservationRow(
            row_number=offset,
            **_parse_reservation_row(raw),
            raw=list(raw),
        )
        records.append(record)

    return ReservationLedger(rows=records, status_index=STATUS_COLUMN_INDEX)


async def get_active_reservations_for_clan(clan_tag: str) -> List[ReservationRow]:
    """Return active reservations matching ``clan_tag``."""

    normalized = _normalize_tag(clan_tag)
    if not normalized:
        return []

    rows = await _load_reservations()
    return [row for row in rows if row.is_active and row.normalized_clan_tag == normalized]


async def find_active_reservations_for_recruit(
    ticket_user_id: Optional[int] = None,
    username: str | None = None,
) -> List[ReservationRow]:
    """Return active reservations for the recruit identified by ``ticket_user_id`` or ``username``."""

    rows = await _load_reservations()

    matches: List[ReservationRow] = []
    if ticket_user_id is not None:
        matches = [
            row
            for row in rows
            if row.is_active and row.ticket_user_id is not None and row.ticket_user_id == ticket_user_id
        ]

    if not matches:
        normalized_name = _normalize_username(username)
        if normalized_name:
            matches = [
                row
                for row in rows
                if row.is_active and _normalize_username(row.username_snapshot) == normalized_name
            ]

    if not matches:
        return []

    def _sort_key(row: ReservationRow) -> tuple[dt.datetime, int]:
        created = row.created_at or dt.datetime.min.replace(tzinfo=dt.timezone.utc)
        return (created, row.row_number)

    return sorted(matches, key=_sort_key, reverse=True)


async def count_active_reservations_for_clan(clan_tag: str) -> int:
    """Return the number of active reservations for ``clan_tag``."""

    reservations = await get_active_reservations_for_clan(clan_tag)
    return len(reservations)


async def get_active_reservations_by_clan() -> dict[str, List[ReservationRow]]:
    """Return a mapping of clan tag → active reservation rows."""

    rows = await _load_reservations()
    grouped: dict[str, List[ReservationRow]] = {}
    for row in rows:
        if not row.is_active:
            continue
        tag = row.normalized_clan_tag
        if not tag:
            continue
        grouped.setdefault(tag, []).append(row)
    return grouped


async def get_active_reservation_names_for_clan(
    clan_tag: str,
    guild: SupportsMemberLookup | None = None,
    *,
    resolver: ResolveUserFn | None = None,
) -> List[str]:
    """Resolve a friendly list of names for active reservations."""

    reservations = await get_active_reservations_for_clan(clan_tag)
    return await resolve_reservation_names(reservations, guild=guild, resolver=resolver)


async def resolve_reservation_names(
    reservations: Sequence[ReservationRow],
    *,
    guild: SupportsMemberLookup | None = None,
    resolver: ResolveUserFn | None = None,
) -> List[str]:
    """Resolve reservation holders to display names using the provided context."""

    names: List[str] = []
    seen: set[str] = set()
    for row in reservations:
        candidate = await _resolve_reservation_name(row, guild=guild, resolver=resolver)
        if not candidate:
            continue
        display = candidate.strip()
        if not display or display in seen:
            continue
        seen.add(display)
        names.append(display)
    return names


async def _resolve_reservation_name(
    row: ReservationRow,
    *,
    guild: SupportsMemberLookup | None,
    resolver: ResolveUserFn | None,
) -> str | None:
    if resolver and row.ticket_user_id is not None:
        try:
            resolved = resolver(row.ticket_user_id)
        except Exception:  # pragma: no cover - defensive guard around callbacks
            log.exception("reservation resolver callback raised", extra={"clan": row.clan_tag})
        else:
            if inspect.isawaitable(resolved):
                resolved = await resolved
            if resolved:
                text = str(resolved).strip()
                if text:
                    return text
    if guild and row.ticket_user_id is not None:
        try:
            member = guild.get_member(row.ticket_user_id)
        except Exception:  # pragma: no cover - guild lookup errors should not break flow
            log.exception(
                "guild member lookup failed",
                extra={"clan": row.clan_tag, "ticket_user_id": row.ticket_user_id},
            )
        else:
            if member is not None:
                for attr in ("display_name", "nick", "name"):
                    value = getattr(member, attr, None)
                    if value:
                        text = str(value).strip()
                        if text:
                            return text
    if row.username_snapshot:
        text = row.username_snapshot.strip()
        if text:
            return text
    if row.ticket_user_id is not None:
        return str(row.ticket_user_id)
    if row.thread_id:
        return row.thread_id
    return None


async def _load_reservations() -> List[ReservationRow]:
    ledger = await load_reservation_ledger()
    return ledger.rows


async def update_reservation_status(
    row_number: int,
    status: str,
    *,
    status_column: int | None = None,
) -> None:
    """Update the ``status`` cell for the reservation at ``row_number``."""

    if row_number <= 1:
        raise ValueError("row_number must reference a data row")

    column_index = status_column
    if column_index is None or column_index < 0:
        ledger = await load_reservation_ledger()
        column_index = ledger.status_column()
        if column_index is None:
            raise ValueError("Reservations sheet missing a 'status' column")

    recruitment.ensure_service_account_credentials()
    sheet_id = recruitment.get_recruitment_sheet_id()
    tab_name = await recruitment.get_reservations_tab_name_async()
    worksheet = await async_core.aget_worksheet(sheet_id, tab_name)

    cell = f"{_column_label(column_index)}{row_number}"
    await async_core.acall_with_backoff(
        worksheet.update,
        cell,
        [[str(status)]],
        value_input_option="RAW",
    )


async def update_reservation_expiry(row_number: int, reserved_until: dt.date) -> None:
    """Update the ``reserved_until`` cell for the reservation at ``row_number``."""

    if row_number <= 1:
        raise ValueError("row_number must reference a data row")

    recruitment.ensure_service_account_credentials()
    sheet_id = recruitment.get_recruitment_sheet_id()
    tab_name = await recruitment.get_reservations_tab_name_async()
    worksheet = await async_core.aget_worksheet(sheet_id, tab_name)

    cell = f"{_column_label(RESERVED_UNTIL_COL)}{row_number}"
    await async_core.acall_with_backoff(
        worksheet.update,
        cell,
        [[reserved_until.isoformat()]],
        value_input_option="RAW",
    )


async def _fetch_reservations_matrix() -> List[List[str]]:
    recruitment.ensure_service_account_credentials()
    sheet_id = recruitment.get_recruitment_sheet_id()
    tab_name = await recruitment.get_reservations_tab_name_async()
    return await async_core.afetch_values(sheet_id, tab_name)


def _column_label(index: int) -> str:
    if index < 0:
        raise ValueError("column index must be non-negative")
    value = index + 1
    label = ""
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        label = chr(65 + remainder) + label
    return label or "A"


def _parse_reservation_row(row: Sequence[Any]) -> dict[str, Any]:
    return {
        "thread_id": _cell_text(row, THREAD_ID_COL),
        "ticket_user_id": _parse_int(_cell_text(row, TICKET_USER_ID_COL)),
        "recruiter_id": _parse_int(_cell_text(row, RECRUITER_ID_COL)),
        "clan_tag": _cell_text(row, CLAN_TAG_COL),
        "reserved_until": _parse_date(_cell_text(row, RESERVED_UNTIL_COL)),
        "created_at": _parse_datetime(_cell_text(row, CREATED_AT_COL)),
        "status": _cell_text(row, STATUS_COL),
        "notes": _cell_text(row, NOTES_COL),
        "username_snapshot": _cell_text(row, USERNAME_SNAPSHOT_COL) or None,
    }


def _normalize_schema_cell(value: Any) -> str:
    text = "" if value is None else str(value)
    return text.strip()


def _normalize_tag(tag: str | None) -> str:
    text = "" if tag is None else str(tag).strip().upper()
    return "".join(ch for ch in text if ch.isalnum())


def _normalize_status(status: str | None) -> str:
    return (status or "").strip().lower()


def _cell_text(row: Sequence[Any], index: int) -> str:
    if index < 0 or index >= len(row):
        return ""
    value = row[index]
    return "" if value is None else str(value).strip()


def _parse_int(value: str) -> Optional[int]:
    if not value:
        return None
    match = re.search(r"-?\d+", value)
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def _parse_date(value: str) -> Optional[dt.date]:
    text = value.strip()
    if not text:
        return None
    text = text.replace("Z", "")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    try:
        return dt.date.fromisoformat(text)
    except ValueError:
        return None


def _parse_datetime(value: str) -> Optional[dt.datetime]:
    text = value.strip()
    if not text:
        return None
    text = text.replace("Z", "+00:00")
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
    ):
        try:
            parsed = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _row_has_content(row: Sequence[Any]) -> bool:
    return any(str(cell or "").strip() for cell in row)


def _normalize_username(value: str | None) -> str:
    return (value or "").strip().lower()


__all__ = [
    "ReservationLedger",
    "ReservationRow",
    "ReservationSchemaError",
    "SupportsMemberLookup",
    "ResolveUserFn",
    "RESERVATIONS_HEADERS",
    "append_reservation_row",
    "load_reservation_ledger",
    "get_active_reservations_for_clan",
    "find_active_reservations_for_recruit",
    "count_active_reservations_for_clan",
    "get_active_reservations_by_clan",
    "get_active_reservation_names_for_clan",
    "resolve_reservation_names",
    "update_reservation_status",
    "update_reservation_expiry",
]
