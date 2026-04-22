"""Fusion and fusion-event sheet accessors backed by cache buckets."""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping

from shared.config import cfg, get_milestones_sheet_id
from shared.sheets.async_core import (
    acall_with_backoff,
    afetch_records,
    afetch_values,
    aget_worksheet,
)
from shared.sheets.cache_service import cache

log = logging.getLogger("c1c.sheets.fusion")

_CACHE_TTL = int(os.getenv("SHEETS_CACHE_TTL_SEC", "900"))

_FUSION_BUCKET = "fusion"
_FUSION_EVENTS_BUCKET = "fusion_events"
_FUSION_REMINDER_TAB_KEY = "FUSION_REMINDER_TAB"
_FUSION_PROGRESS_TAB_KEY = "FUSION_USER_EVENT_PROGRESS_TAB"
_PROGRESS_ALLOWED_STATUSES = {"not_started", "in_progress", "done", "done_bonus", "skipped"}
_FUSION_REMINDER_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "fusion_id": ("fusion_id",),
    "event_id": ("event_id",),
    "reminder_type": ("reminder_type",),
    "sent_at_utc": ("sent_at_utc",),
}
_FUSION_PROGRESS_COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "fusion_id": ("fusion_id", "fusion key", "fusionkey"),
    "user_id": ("user_id", "user key", "userkey"),
    "event_id": ("event_id", "event key", "eventkey"),
    "status": ("status",),
    "updated_at_utc": ("updated_at_utc", "updated at utc", "updatedat", "updated_at"),
}


@dataclass(frozen=True, slots=True)
class FusionRow:
    fusion_id: str
    fusion_name: str
    champion: str
    champion_image_url: str
    fusion_type: str
    fusion_structure: str
    reward_type: str
    needed: int
    available: int
    start_at_utc: dt.datetime
    end_at_utc: dt.datetime
    announcement_channel_id: int | None
    opt_in_role_id: int | None
    announcement_message_id: int | None
    published_at: dt.datetime | None
    last_announcement_refresh_at: dt.datetime | None
    last_announcement_status_hash: str
    status: str


@dataclass(frozen=True, slots=True)
class FusionEventRow:
    fusion_id: str
    event_id: str
    event_name: str
    event_type: str
    category: str
    start_at_utc: dt.datetime
    end_at_utc: dt.datetime
    reward_amount: float
    bonus: float | None
    reward_type: str
    points_needed: int | None
    is_estimated: bool
    sort_order: int


@dataclass(frozen=True, slots=True)
class FusionUserEventProgressRow:
    fusion_id: str
    user_id: str
    event_id: str
    status: str
    updated_at: dt.datetime


def _resolve_tab_name(key: str) -> str:
    value = cfg.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    raise RuntimeError(f"{key} missing in milestones Config tab")


def _normalize(row: Mapping[str, object]) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, value in row.items():
        out[str(key or "").strip().lower()] = value
    return out


def _pick(row: Mapping[str, object], *keys: str) -> object:
    for key in keys:
        if key in row:
            value = row[key]
            if str(value or "").strip() != "":
                return value
    return ""


def _parse_int(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    try:
        if any(ch in text for ch in (".", "e", "E")):
            return int(float(text))
        return int(text)
    except ValueError:
        return 0


def _parse_int_optional(value: object) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if any(ch in text for ch in (".", "e", "E")):
            return int(float(text))
        return int(text)
    except ValueError:
        return None


def _parse_float(value: object) -> float:
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _parse_float_optional(value: object) -> float | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_bool(value: object) -> bool:
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "y"}


def _parse_discord_id(value: object) -> int | None:
    # Discord snowflakes must never be parsed via float; float conversion can lose precision.
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except ValueError:
        return None
    if parsed <= 0:
        return None
    return parsed

def _column_label(index: int) -> str:
    if index < 0:
        raise ValueError("column index must be non-negative")
    value = index + 1
    label = ""
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        label = chr(65 + remainder) + label
    return label or "A"


def _resolve_reminder_sheet_schema(
    *,
    include_sent_at: bool,
) -> tuple[str, tuple[str, ...]]:
    tab_name = _resolve_tab_name(_FUSION_REMINDER_TAB_KEY)
    required_fields: list[str] = ["fusion_id", "event_id", "reminder_type"]
    if include_sent_at:
        required_fields.append("sent_at_utc")
    return tab_name, tuple(required_fields)


def _reminder_schema_debug() -> dict[str, str]:
    keys = (_FUSION_REMINDER_TAB_KEY,)
    debug: dict[str, str] = {}
    for key in keys:
        value = cfg.get(key)
        debug[key] = str(value if value is not None else "").strip()
    return debug


def _resolve_progress_sheet_schema() -> tuple[str, dict[str, str]]:
    tab_name = _resolve_tab_name(_FUSION_PROGRESS_TAB_KEY)
    return tab_name, dict(_FUSION_PROGRESS_COLUMN_ALIASES)


def _resolve_progress_header_indices(
    *,
    tab_name: str,
    header: list[str],
    include_updated_at: bool,
) -> dict[str, int]:
    required_fields = ["fusion_id", "user_id", "event_id", "status"]
    if include_updated_at:
        required_fields.append("updated_at_utc")
    return {
        field: _resolve_header_index(
            tab_name=tab_name,
            header=header,
            field=field,
            aliases_by_field=_FUSION_PROGRESS_COLUMN_ALIASES,
        )
        for field in required_fields
    }

def _parse_iso_utc(value: object) -> dt.datetime:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("missing timestamp")
    candidate = raw.replace("Z", "+00:00")
    parsed = dt.datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _parse_iso_utc_optional(value: object) -> dt.datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        return _parse_iso_utc(raw)
    except ValueError:
        return None


def _sheet_id() -> str:
    sheet_id = get_milestones_sheet_id().strip()
    if not sheet_id:
        raise RuntimeError("MILESTONES_SHEET_ID not set")
    return sheet_id


def _resolve_header_index(
    *,
    tab_name: str,
    header: list[str],
    field: str,
    aliases_by_field: Mapping[str, tuple[str, ...]] | None = None,
) -> int:
    alias_source = aliases_by_field or _FUSION_REMINDER_COLUMN_ALIASES
    aliases = alias_source.get(field, (field,))
    normalized_aliases = [alias.strip().lower() for alias in aliases if alias.strip()]
    for alias in normalized_aliases:
        if alias in header:
            return header.index(alias)

    available = [cell.strip() for cell in header]
    log.error(
        "fusion reminder schema mismatch; missing required header",
        extra={
            "tab": tab_name,
            "field": field,
            "expected_headers": list(aliases),
            "available_headers": available,
        },
    )
    raise RuntimeError(
        "Fusion reminder sheet missing required header "
        f"(tab={tab_name}, field={field}, "
        f"expected={list(aliases)}, available={available})"
    )


async def _load_fusion_sheet_matrix(tab_name: str) -> tuple[str, list[list[object]], list[str]]:
    sheet_id = _sheet_id()
    matrix = await afetch_values(sheet_id, tab_name)
    if not matrix:
        raise RuntimeError(f"Fusion sheet is empty (tab={tab_name})")
    header = [str(cell or "").strip().lower() for cell in matrix[0]]
    return sheet_id, matrix, header


def _resolve_fusion_row_index(*, fusion_id: str, header: list[str], matrix: list[list[object]], tab_name: str) -> int:
    if "fusion_id" not in header:
        log.error("fusion sheet schema mismatch; missing fusion_id header", extra={"tab": tab_name})
        raise RuntimeError("Fusion sheet missing fusion_id column")

    fusion_col = header.index("fusion_id")
    for idx, row in enumerate(matrix[1:], start=2):
        cell = str(row[fusion_col] if fusion_col < len(row) else "").strip()
        if cell == fusion_id:
            return idx

    raise RuntimeError(f"Fusion row not found for fusion_id={fusion_id}")


def _require_fusion_headers(*, tab_name: str, header: list[str], required: tuple[str, ...]) -> None:
    missing = [col for col in required if col not in header]
    if missing:
        log.error(
            "fusion sheet schema mismatch; missing required headers",
            extra={"tab": tab_name, "missing_headers": missing, "available_headers": header},
        )
        raise RuntimeError(f"Fusion sheet missing columns: {', '.join(missing)}")


async def _load_fusions() -> tuple[FusionRow, ...]:
    tab_name = _resolve_tab_name("FUSION_TAB")
    rows = await afetch_records(_sheet_id(), tab_name)
    parsed: list[FusionRow] = []
    for raw in rows or []:
        row = _normalize(raw)
        try:
            parsed.append(
                FusionRow(
                    fusion_id=str(row.get("fusion_id") or "").strip(),
                    fusion_name=str(row.get("fusion_name") or "").strip(),
                    champion=str(row.get("champion") or "").strip(),
                    champion_image_url=str(row.get("champion_image_url") or "").strip(),
                    fusion_type=str(row.get("fusion_type") or "").strip(),
                    fusion_structure=str(row.get("fusion_structure") or "").strip(),
                    reward_type=str(row.get("reward_type") or "").strip(),
                    needed=_parse_int(_pick(row, "fusion.needed", "needed")),
                    available=_parse_int(_pick(row, "fusion.available", "available")),
                    start_at_utc=_parse_iso_utc(row.get("start_at_utc")),
                    end_at_utc=_parse_iso_utc(row.get("end_at_utc")),
                    announcement_channel_id=_parse_discord_id(
                        row.get("announcement_channel_id")
                    ),
                    opt_in_role_id=_parse_discord_id(row.get("opt_in_role_id")),
                    announcement_message_id=_parse_discord_id(
                        row.get("announcement_message_id")
                    ),
                    published_at=_parse_iso_utc_optional(row.get("published_at")),
                    last_announcement_refresh_at=_parse_iso_utc_optional(
                        row.get("last_announcement_refresh_at")
                    ),
                    last_announcement_status_hash=str(
                        row.get("last_announcement_status_hash") or ""
                    ).strip(),
                    status=str(row.get("status") or "").strip().lower(),
                )
            )
        except Exception:
            log.warning(
                "fusion row skipped due to parse error",
                extra={
                    "fusion_id": str(row.get("fusion_id") or "").strip(),
                    "fusion_name": str(row.get("fusion_name") or "").strip(),
                    "status": str(row.get("status") or "").strip(),
                },
                exc_info=True,
            )
    return tuple(parsed)


async def _load_fusion_events() -> tuple[FusionEventRow, ...]:
    tab_name = _resolve_tab_name("FUSION_EVENT_TAB")
    rows = await afetch_records(_sheet_id(), tab_name)
    parsed: list[FusionEventRow] = []
    for raw in rows or []:
        row = _normalize(raw)
        try:
            parsed.append(
                FusionEventRow(
                    fusion_id=str(row.get("fusion_id") or "").strip(),
                    event_id=str(row.get("event_id") or "").strip(),
                    event_name=str(row.get("event_name") or "").strip(),
                    event_type=str(row.get("event_type") or "").strip(),
                    category=str(row.get("category") or "").strip(),
                    start_at_utc=_parse_iso_utc(
                        _pick(
                            row,
                            "start_at_utc",
                            "event_start_at_utc",
                            "start_time_utc",
                            "event_start_time_utc",
                            "start_at",
                            "start_time",
                        )
                    ),
                    end_at_utc=_parse_iso_utc(
                        _pick(
                            row,
                            "end_at_utc",
                            "event_end_at_utc",
                            "end_time_utc",
                            "event_end_time_utc",
                            "end_at",
                            "end_time",
                        )
                    ),
                    reward_amount=_parse_float(row.get("reward_amount")),
                    bonus=_parse_float_optional(row.get("bonus")),
                    reward_type=str(row.get("reward_type") or "").strip(),
                    points_needed=_parse_int_optional(row.get("points_needed")),
                    is_estimated=_parse_bool(row.get("is_estimated")),
                    sort_order=_parse_int(row.get("sort_order")),
                )
            )
        except Exception:
            log.warning(
                "fusion event row skipped due to parse error",
                extra={
                    "fusion_id": str(row.get("fusion_id") or "").strip(),
                    "event_id": str(row.get("event_id") or "").strip(),
                    "event_name": str(row.get("event_name") or "").strip(),
                },
                exc_info=True,
            )
    return tuple(parsed)


def register_cache_buckets() -> tuple[str, str]:
    if cache.get_bucket(_FUSION_BUCKET) is None:
        cache.register(_FUSION_BUCKET, _CACHE_TTL, _load_fusions)
    if cache.get_bucket(_FUSION_EVENTS_BUCKET) is None:
        cache.register(_FUSION_EVENTS_BUCKET, _CACHE_TTL, _load_fusion_events)
    return _FUSION_BUCKET, _FUSION_EVENTS_BUCKET


async def _cached_rows(bucket_name: str) -> tuple[Any, ...]:
    payload = await cache.get(bucket_name)
    if payload is None:
        await cache.refresh_now(bucket_name, actor="fusion")
        payload = await cache.get(bucket_name)
    if isinstance(payload, tuple):
        return payload
    if isinstance(payload, list):
        return tuple(payload)
    return tuple()


async def get_active_fusion() -> FusionRow | None:
    fusion_bucket, _ = register_cache_buckets()
    rows = await _cached_rows(fusion_bucket)
    candidates = [
        row
        for row in rows
        if isinstance(row, FusionRow) and row.status.casefold() == "published"
    ]
    if candidates:
        candidates.sort(key=lambda row: (row.start_at_utc, row.fusion_id), reverse=True)
        return candidates[0]
    return None


async def get_published_fusions() -> list[FusionRow]:
    """Return all fusions currently eligible for live announcement maintenance."""

    fusion_bucket, _ = register_cache_buckets()
    rows = [
        row
        for row in await _cached_rows(fusion_bucket)
        if isinstance(row, FusionRow) and row.status.casefold() in {"active", "published"}
    ]
    rows.sort(key=lambda row: (row.start_at_utc, row.fusion_id), reverse=True)
    return rows


async def get_fusion_events(fusion_id: str) -> list[FusionEventRow]:
    _, events_bucket = register_cache_buckets()
    rows = await _cached_rows(events_bucket)
    target = str(fusion_id or "").strip()
    filtered = [
        row
        for row in rows
        if isinstance(row, FusionEventRow) and row.fusion_id == target
    ]
    filtered.sort(key=lambda row: (row.start_at_utc, row.sort_order, row.event_id))
    return filtered


def _coerce_utc_now(now: dt.datetime | None) -> dt.datetime:
    if now is None:
        return dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(dt.timezone.utc)


def _valid_event_timing(
    event: FusionEventRow,
    *,
    for_helper: str,
) -> tuple[dt.datetime, dt.datetime | None] | None:
    start_at = getattr(event, "start_at_utc", None)
    end_at = getattr(event, "end_at_utc", None)

    if not isinstance(start_at, dt.datetime):
        log.warning(
            "fusion event skipped due to invalid start_at_utc",
            extra={
                "helper": for_helper,
                "fusion_id": getattr(event, "fusion_id", ""),
                "event_id": getattr(event, "event_id", ""),
                "event_name": getattr(event, "event_name", ""),
                "start_at_utc": start_at,
            },
        )
        return None

    if start_at.tzinfo is None:
        start_at = start_at.replace(tzinfo=dt.timezone.utc)
    else:
        start_at = start_at.astimezone(dt.timezone.utc)

    if end_at is None:
        return start_at, None

    if not isinstance(end_at, dt.datetime):
        log.warning(
            "fusion event skipped due to invalid end_at_utc",
            extra={
                "helper": for_helper,
                "fusion_id": getattr(event, "fusion_id", ""),
                "event_id": getattr(event, "event_id", ""),
                "event_name": getattr(event, "event_name", ""),
                "end_at_utc": end_at,
            },
        )
        return None

    if end_at.tzinfo is None:
        end_at = end_at.replace(tzinfo=dt.timezone.utc)
    else:
        end_at = end_at.astimezone(dt.timezone.utc)
    return start_at, end_at


async def get_upcoming_events(
    fusion_id: str,
    now: dt.datetime | None = None,
) -> list[FusionEventRow]:
    reference = _coerce_utc_now(now)
    events = await get_fusion_events(fusion_id)

    future: list[tuple[dt.datetime, FusionEventRow]] = []
    for event in events:
        timing = _valid_event_timing(event, for_helper="get_upcoming_events")
        if timing is None:
            continue
        start_at, _ = timing
        if start_at > reference:
            future.append((start_at, event))

    future.sort(key=lambda item: (item[0], item[1].sort_order, item[1].event_id))
    return [item[1] for item in future]


async def get_active_events(
    fusion_id: str,
    now: dt.datetime | None = None,
) -> list[FusionEventRow]:
    reference = _coerce_utc_now(now)
    events = await get_fusion_events(fusion_id)

    active: list[tuple[dt.datetime, FusionEventRow]] = []
    for event in events:
        timing = _valid_event_timing(event, for_helper="get_active_events")
        if timing is None:
            continue

        start_at, end_at = timing
        if start_at <= reference and (end_at is None or reference < end_at):
            active.append((start_at, event))

    active.sort(key=lambda item: (item[0], item[1].sort_order, item[1].event_id))
    return [item[1] for item in active]


def get_valid_event_timing(
    event: FusionEventRow,
    *,
    for_helper: str,
) -> tuple[dt.datetime, dt.datetime | None] | None:
    """Validate and coerce fusion event timing into UTC-aware datetimes."""

    return _valid_event_timing(event, for_helper=for_helper)


def derive_event_status(
    *,
    start_at_utc: dt.datetime,
    end_at_utc: dt.datetime | None,
    now: dt.datetime | None = None,
) -> str:
    """Return canonical event status from normalized UTC timestamps."""

    reference = _coerce_utc_now(now)
    if reference < start_at_utc:
        return "upcoming"
    if end_at_utc is None or reference < end_at_utc:
        return "live"
    return "ended"


async def get_sent_reminder_keys(fusion_id: str) -> set[tuple[str, str]]:
    """Return durable reminder keys previously sent for ``fusion_id``."""

    try:
        tab_name, required_fields = _resolve_reminder_sheet_schema(include_sent_at=False)
    except Exception as exc:
        debug_config = _reminder_schema_debug()
        raise RuntimeError(
            "Fusion reminder durable dedupe config invalid "
            f"(config={debug_config})"
        ) from exc
    matrix = await afetch_values(_sheet_id(), tab_name)
    if not matrix:
        return set()

    header = [str(cell or "").strip().lower() for cell in matrix[0]]
    index_by_field = {
        field: _resolve_header_index(tab_name=tab_name, header=header, field=field)
        for field in required_fields
    }
    fusion_idx = index_by_field["fusion_id"]
    event_idx = index_by_field["event_id"]
    reminder_idx = index_by_field["reminder_type"]
    target = str(fusion_id or "").strip()

    keys: set[tuple[str, str]] = set()
    for row in matrix[1:]:
        row_fusion = str(row[fusion_idx] if fusion_idx < len(row) else "").strip()
        if row_fusion != target:
            continue
        event_id = str(row[event_idx] if event_idx < len(row) else "").strip()
        reminder_type = str(row[reminder_idx] if reminder_idx < len(row) else "").strip()
        if event_id and reminder_type:
            keys.add((event_id, reminder_type))
    return keys


async def mark_reminder_sent(
    fusion_id: str,
    *,
    event_id: str,
    reminder_type: str,
    sent_at: dt.datetime,
) -> None:
    """Persist a sent reminder marker with a durable fusion/event/type key."""

    try:
        tab_name, required_fields = _resolve_reminder_sheet_schema(include_sent_at=True)
    except Exception as exc:
        debug_config = _reminder_schema_debug()
        raise RuntimeError(
            "Fusion reminder durable dedupe config invalid for write "
            f"(config={debug_config})"
        ) from exc
    matrix = await afetch_values(_sheet_id(), tab_name)
    if not matrix:
        raise RuntimeError(
            "Fusion reminder sheet is empty "
            f"(tab={tab_name}, key={_FUSION_REMINDER_TAB_KEY})"
        )

    header = [str(cell or "").strip().lower() for cell in matrix[0]]
    index_by_field = {
        field: _resolve_header_index(tab_name=tab_name, header=header, field=field)
        for field in required_fields
    }
    fusion_idx = index_by_field["fusion_id"]
    event_idx = index_by_field["event_id"]
    reminder_idx = index_by_field["reminder_type"]
    sent_at_idx = index_by_field["sent_at_utc"]
    target_fusion = str(fusion_id or "").strip()
    target_event = str(event_id or "").strip()
    target_type = str(reminder_type or "").strip()
    sent_token = sent_at.astimezone(dt.timezone.utc).isoformat()

    worksheet = await aget_worksheet(_sheet_id(), tab_name)
    for row_idx, row in enumerate(matrix[1:], start=2):
        row_fusion = str(row[fusion_idx] if fusion_idx < len(row) else "").strip()
        row_event = str(row[event_idx] if event_idx < len(row) else "").strip()
        row_type = str(row[reminder_idx] if reminder_idx < len(row) else "").strip()
        if (row_fusion, row_event, row_type) != (target_fusion, target_event, target_type):
            continue
        cell = f"{_column_label(sent_at_idx)}{row_idx}"
        await acall_with_backoff(
            worksheet.update,
            cell,
            [[sent_token]],
            value_input_option="RAW",
        )
        return

    row_values = [""] * len(header)
    row_values[fusion_idx] = target_fusion
    row_values[event_idx] = target_event
    row_values[reminder_idx] = target_type
    row_values[sent_at_idx] = sent_token
    await acall_with_backoff(
        worksheet.append_row,
        row_values,
        value_input_option="RAW",
    )


def _normalize_progress_status(value: object) -> str:
    status = str(value or "").strip().lower()
    if status in _PROGRESS_ALLOWED_STATUSES:
        return status
    return "not_started"


async def get_user_event_progress(fusion_id: str, user_id: str) -> dict[str, str]:
    """Return per-event progress status for a fusion/user tuple."""

    tab_name, _progress_aliases = _resolve_progress_sheet_schema()
    matrix = await afetch_values(_sheet_id(), tab_name)
    if not matrix:
        return {}

    header = [str(cell or "").strip().lower() for cell in matrix[0]]
    index_by_field = _resolve_progress_header_indices(
        tab_name=tab_name,
        header=header,
        include_updated_at=False,
    )
    fusion_idx = index_by_field["fusion_id"]
    user_idx = index_by_field["user_id"]
    event_idx = index_by_field["event_id"]
    status_idx = index_by_field["status"]
    target_fusion = str(fusion_id or "").strip()
    target_user = str(user_id or "").strip()

    rows: dict[str, str] = {}
    for row in matrix[1:]:
        row_fusion = str(row[fusion_idx] if fusion_idx < len(row) else "").strip()
        row_user = str(row[user_idx] if user_idx < len(row) else "").strip()
        if row_fusion != target_fusion or row_user != target_user:
            continue
        event_id = str(row[event_idx] if event_idx < len(row) else "").strip()
        if not event_id:
            continue
        status = _normalize_progress_status(row[status_idx] if status_idx < len(row) else "")
        rows[event_id] = status
    return rows


async def upsert_user_event_progress(
    fusion_id: str,
    user_id: str,
    event_id: str,
    status: str,
    updated_at: dt.datetime,
) -> None:
    """Write user progress status for one fusion/user/event tuple."""

    normalized_status = str(status or "").strip().lower()
    if normalized_status not in _PROGRESS_ALLOWED_STATUSES:
        raise ValueError(
            "Invalid fusion progress status; expected one of "
            f"{sorted(_PROGRESS_ALLOWED_STATUSES)}, got={status!r}"
        )
    tab_name, _progress_aliases = _resolve_progress_sheet_schema()
    matrix = await afetch_values(_sheet_id(), tab_name)
    if not matrix:
        raise RuntimeError(
            "Fusion user progress sheet is empty "
            f"(tab={tab_name}, key={_FUSION_PROGRESS_TAB_KEY})"
        )

    header = [str(cell or "").strip().lower() for cell in matrix[0]]
    index_by_field = _resolve_progress_header_indices(
        tab_name=tab_name,
        header=header,
        include_updated_at=True,
    )
    fusion_idx = index_by_field["fusion_id"]
    user_idx = index_by_field["user_id"]
    event_idx = index_by_field["event_id"]
    status_idx = index_by_field["status"]
    updated_idx = index_by_field["updated_at_utc"]
    target_fusion = str(fusion_id or "").strip()
    target_user = str(user_id or "").strip()
    target_event = str(event_id or "").strip()
    timestamp = updated_at.astimezone(dt.timezone.utc).isoformat()

    worksheet = await aget_worksheet(_sheet_id(), tab_name)
    for row_idx, row in enumerate(matrix[1:], start=2):
        row_fusion = str(row[fusion_idx] if fusion_idx < len(row) else "").strip()
        row_user = str(row[user_idx] if user_idx < len(row) else "").strip()
        row_event = str(row[event_idx] if event_idx < len(row) else "").strip()
        if (row_fusion, row_user, row_event) != (target_fusion, target_user, target_event):
            continue
        await acall_with_backoff(
            worksheet.update,
            f"{_column_label(status_idx)}{row_idx}",
            [[normalized_status]],
            value_input_option="RAW",
        )
        await acall_with_backoff(
            worksheet.update,
            f"{_column_label(updated_idx)}{row_idx}",
            [[timestamp]],
            value_input_option="RAW",
        )
        return

    row_values = [""] * len(header)
    row_values[fusion_idx] = target_fusion
    row_values[user_idx] = target_user
    row_values[event_idx] = target_event
    row_values[status_idx] = normalized_status
    row_values[updated_idx] = timestamp
    await acall_with_backoff(
        worksheet.append_row,
        row_values,
        value_input_option="RAW",
    )




async def get_ended_fusions(now: dt.datetime | None = None) -> list[FusionRow]:
    """Return ended fusions that may still need post-end cleanup tasks."""

    reference = _coerce_utc_now(now)
    fusion_bucket, _ = register_cache_buckets()
    rows = [row for row in await _cached_rows(fusion_bucket) if isinstance(row, FusionRow)]

    ended = [
        row
        for row in rows
        if row.status.casefold() in {"active", "published"} and row.end_at_utc <= reference
    ]
    ended.sort(key=lambda row: (row.end_at_utc, row.fusion_id), reverse=True)
    return ended

def _tracker_kind(row: FusionRow) -> str:
    return "titan" if str(row.fusion_type or "").strip().casefold() == "titan" else "fusion"


async def get_publishable_fusion(
    *,
    include_draft: bool = False,
    tracker_kind: str | None = None,
    prefer_draft: bool = False,
) -> FusionRow | None:
    """Return the best fusion/titan row for command and publish flow selection."""

    fusion_bucket, _ = register_cache_buckets()
    rows = [row for row in await _cached_rows(fusion_bucket) if isinstance(row, FusionRow)]
    if not rows:
        return None

    allowed_statuses: tuple[str, ...]
    if include_draft and prefer_draft:
        allowed_statuses = ("draft", "active", "published")
    elif include_draft:
        allowed_statuses = ("active", "published", "draft")
    else:
        allowed_statuses = ("active", "published")

    normalized_kind = str(tracker_kind or "").strip().casefold()
    if normalized_kind:
        rows = [row for row in rows if _tracker_kind(row) == normalized_kind]
    if not rows:
        return None

    for status in allowed_statuses:
        matches = [row for row in rows if row.status.casefold() == status]
        if matches:
            matches.sort(key=lambda row: (row.start_at_utc, row.fusion_id), reverse=True)
            return matches[0]

    return None


async def update_fusion_publication(
    fusion_id: str,
    *,
    announcement_message_id: int,
    announcement_channel_id: int | None,
    published_at: dt.datetime,
    set_published_status: bool,
) -> None:
    """Write publish metadata back to the fusion row in the configured sheet."""

    tab_name = _resolve_tab_name("FUSION_TAB")
    sheet_id, matrix, header = await _load_fusion_sheet_matrix(tab_name)
    row_idx = _resolve_fusion_row_index(
        fusion_id=fusion_id,
        header=header,
        matrix=matrix,
        tab_name=tab_name,
    )

    required_cols = ["announcement_message_id", "published_at"]
    if announcement_channel_id is not None:
        required_cols.append("announcement_channel_id")
    if set_published_status:
        required_cols.append("status")

    _require_fusion_headers(tab_name=tab_name, header=header, required=tuple(required_cols))

    worksheet = await aget_worksheet(sheet_id, tab_name)
    updates = {
        "announcement_message_id": str(announcement_message_id),
        "published_at": published_at.astimezone(dt.timezone.utc).isoformat(),
    }
    if announcement_channel_id is not None:
        updates["announcement_channel_id"] = str(announcement_channel_id)
    if set_published_status:
        updates["status"] = "published"

    for col_name, value in updates.items():
        col_index = header.index(col_name)
        cell = f"{_column_label(col_index)}{row_idx}"
        await acall_with_backoff(
            worksheet.update,
            cell,
            [[value]],
            value_input_option="RAW",
        )

    register_cache_buckets()
    await cache.refresh_now(_FUSION_BUCKET, actor="fusion_publish")


async def update_fusion_announcement_refresh_state(
    fusion_id: str,
    *,
    refreshed_at: dt.datetime,
    status_hash: str,
) -> None:
    """Persist announcement refresh metadata for scheduler dedupe/restart safety."""

    tab_name = _resolve_tab_name("FUSION_TAB")
    sheet_id, matrix, header = await _load_fusion_sheet_matrix(tab_name)
    row_idx = _resolve_fusion_row_index(
        fusion_id=fusion_id,
        header=header,
        matrix=matrix,
        tab_name=tab_name,
    )
    _require_fusion_headers(
        tab_name=tab_name,
        header=header,
        required=("last_announcement_refresh_at", "last_announcement_status_hash"),
    )

    worksheet = await aget_worksheet(sheet_id, tab_name)
    updates = {
        "last_announcement_refresh_at": refreshed_at.astimezone(dt.timezone.utc).isoformat(),
        "last_announcement_status_hash": str(status_hash or "").strip(),
    }
    for col_name, value in updates.items():
        col_index = header.index(col_name)
        cell = f"{_column_label(col_index)}{row_idx}"
        await acall_with_backoff(
            worksheet.update,
            cell,
            [[value]],
            value_input_option="RAW",
        )

    register_cache_buckets()
    await cache.refresh_now(_FUSION_BUCKET, actor="fusion_announcement_refresh")


__all__ = [
    "FusionEventRow",
    "FusionRow",
    "FusionUserEventProgressRow",
    "get_active_fusion",
    "get_ended_fusions",
    "get_published_fusions",
    "get_active_events",
    "derive_event_status",
    "get_valid_event_timing",
    "get_publishable_fusion",
    "get_fusion_events",
    "get_sent_reminder_keys",
    "get_user_event_progress",
    "get_upcoming_events",
    "mark_reminder_sent",
    "upsert_user_event_progress",
    "update_fusion_publication",
    "update_fusion_announcement_refresh_state",
    "register_cache_buckets",
]
