"""Fusion and fusion-event sheet accessors backed by cache buckets."""

from __future__ import annotations

import datetime as dt
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
_LAST_FUSION_PARSE_ERRORS: dict[str, str] = {}
_FUSION_REMINDER_TAB_KEY = "FUSION_REMINDER_TAB"
_FUSION_PROGRESS_TAB_KEY = "FUSION_USER_EVENT_PROGRESS_TAB"
_FUSION_REMINDER_SETTINGS_TAB_KEY = "FUSION_REMINDER_SETTINGS_TAB"
_FUSION_REMINDER_SETTINGS_COLUMN_HEADERS: dict[str, tuple[str, ...]] = {
    "setting_key": ("setting_key",),
    "value": ("value",),
}
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
    "milestone_key": ("milestone_key", "milestone", "milestone key"),
    "status": ("status",),
    "partial_amount": ("partial_amount",),
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
class FusionEventMilestone:
    points_needed: int
    reward_amount: float


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
    milestones: tuple[FusionEventMilestone, ...] = tuple()
    is_estimated: bool = False
    sort_order: int = 0
    embed_title: str = ""
    embed_description: str = ""
    embed_footer: str = ""


@dataclass(frozen=True, slots=True)
class FusionUserEventProgressRow:
    fusion_id: str
    user_id: str
    event_id: str
    milestone_key: str
    status: str
    updated_at: dt.datetime


@dataclass(frozen=True, slots=True)
class FusionProgressSaveResult:
    fusion_id: str
    event_id: str
    user_id: str
    selected_status: str
    tab_name: str
    headers: tuple[str, ...]
    row_key: tuple[str, str, str, str]
    row_number: int
    operation: str
    saved: bool
    failure_reason: str = ""


@dataclass(frozen=True, slots=True)
class FusionReminderSettingSource:
    tab_name: str = ""
    key_header: str = ""
    value_header: str = ""
    raw_value: str = ""
    duplicate_count: int = 0


@dataclass(frozen=True, slots=True)
class FusionReminderSettings:
    start_offset_minutes: int = 360
    end_lookahead_hours: int = 24
    upcoming_window_days: int = 2
    group_events: bool = False
    grouped_post_time_utc: str = ""
    include_start_events: bool = True
    include_ending_events: bool = False
    include_upcoming_events: bool = False
    grouped_embed_title: str = ""
    grouped_embed_description: str = ""
    grouped_live_label: str = ""
    grouped_upcoming_label: str = ""
    grouped_ending_label: str = ""
    grouped_empty_value: str = ""
    grouped_jump_label: str = ""
    settings_source_tab: str = ""
    settings_sheet_id_tail: str = ""
    settings_headers: tuple[str, ...] = tuple()
    settings_key_header: str = ""
    settings_value_header: str = ""
    settings_raw_values: Mapping[str, object] = field(default_factory=dict)
    settings_raw_types: Mapping[str, str] = field(default_factory=dict)
    settings_raw_key_names: Mapping[str, str] = field(default_factory=dict)
    settings_cache_status: str = "not_cached"
    group_events_source: FusionReminderSettingSource = field(default_factory=FusionReminderSettingSource)


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


def _normalize_fusion_id(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _normalize_fusion_type(value: object) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _pick(row: Mapping[str, object], *keys: str) -> object:
    for key in keys:
        if key in row:
            value = row[key]
            if str(value or "").strip() != "":
                return value
    return ""


def _sheet_tail(sheet_id: object) -> str:
    text = str(sheet_id or "").strip()
    if not text:
        return "missing"
    tail = text[-6:] if len(text) >= 6 else text
    return f"…{tail}" if len(text) > len(tail) else tail


def _time_from_sheet_value(value: object) -> dt.time | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.time().replace(tzinfo=None, microsecond=0)
    if isinstance(value, dt.time):
        return value.replace(tzinfo=None, microsecond=0)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        raw = float(value)
        fraction = raw % 1
        total_seconds = int(round(fraction * 24 * 60 * 60)) % (24 * 60 * 60)
        return (dt.datetime.min + dt.timedelta(seconds=total_seconds)).time().replace(microsecond=0)

    text = str(value or "").strip()
    if not text:
        return None
    try:
        numeric = float(text)
    except ValueError:
        numeric = None
    if numeric is not None and 0 <= numeric < 1:
        return _time_from_sheet_value(numeric)
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M:%S %p"):
        try:
            parsed = dt.datetime.strptime(text, fmt).time()
            return parsed.replace(tzinfo=None, microsecond=0)
        except ValueError:
            continue
    return None


def _configured_timezone() -> ZoneInfo:
    raw = str(cfg.get("TIMEZONE") or "Europe/Vienna").strip() or "Europe/Vienna"
    try:
        return ZoneInfo(raw)
    except ZoneInfoNotFoundError:
        log.warning("fusion reminder settings timezone invalid; falling back to Europe/Vienna", extra={"timezone": raw})
        return ZoneInfo("Europe/Vienna")


def _local_time_to_utc_text(value: object, *, reference: dt.datetime | None = None) -> str:
    parsed = _time_from_sheet_value(value)
    if parsed is None:
        return ""
    ref = reference or dt.datetime.now(dt.timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=dt.timezone.utc)
    local_zone = _configured_timezone()
    local_day = ref.astimezone(local_zone).date()
    local_dt = dt.datetime.combine(local_day, parsed, tzinfo=local_zone)
    return local_dt.astimezone(dt.timezone.utc).strftime("%H:%M")


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


def _parse_milestones(value: object, *, fusion_id: str, event_id: str) -> tuple[FusionEventMilestone, ...]:
    text = str(value or "").strip()
    if not text:
        return tuple()
    parsed: list[FusionEventMilestone] = []
    for token in text.split(","):
        part = token.strip()
        if not part:
            continue
        try:
            points_raw, reward_raw = part.split(":", 1)
            points = int(float(points_raw.strip()))
            reward = float(reward_raw.strip())
            if points <= 0 or reward < 0:
                raise ValueError("non-positive milestone")
            parsed.append(FusionEventMilestone(points_needed=points, reward_amount=reward))
        except Exception:
            log.warning("fusion milestones entry malformed; ignoring", extra={"fusion_id": fusion_id, "event_id": event_id, "entry": part})
    parsed.sort(key=lambda m: m.points_needed)
    return tuple(parsed)

def _parse_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    text = ("" if value is None else str(value)).strip().casefold()
    if text in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "off"}:
        return False
    return False


def _parse_nonnegative_int(value: object, default: int) -> int:
    parsed = _parse_int_optional(value)
    if parsed is None or parsed < 0:
        return default
    return parsed


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


def reminder_dedupe_backend_metadata() -> dict[str, str]:
    """Return non-secret metadata for reminder durable dedupe diagnostics."""

    debug = _reminder_schema_debug()
    tab_name = str(debug.get(_FUSION_REMINDER_TAB_KEY, "") or "").strip()
    return {
        "backend_type": "google_sheets",
        "config_key": _FUSION_REMINDER_TAB_KEY,
        "tab_name": tab_name,
    }


def _resolve_progress_sheet_schema() -> tuple[str, dict[str, str]]:
    tab_name = _resolve_tab_name(_FUSION_PROGRESS_TAB_KEY)
    return tab_name, dict(_FUSION_PROGRESS_COLUMN_ALIASES)


def _resolve_progress_header_indices(
    *,
    tab_name: str,
    header: list[str],
    include_updated_at: bool,
) -> dict[str, int]:
    required_fields = ["fusion_id", "user_id", "event_id", "milestone_key", "status"]
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


def _find_optional_progress_column_index(*, header: list[str], field: str) -> int:
    aliases = _FUSION_PROGRESS_COLUMN_ALIASES.get(field, (field,))
    for alias in aliases:
        normalized = alias.strip().lower()
        if normalized and normalized in header:
            return header.index(normalized)
    return -1


async def _ensure_optional_progress_header(
    *,
    worksheet: Any,
    tab_name: str,
    matrix: list[list[object]],
    header: list[str],
    field: str,
    before_field: str | None = None,
) -> tuple[list[list[object]], list[str]]:
    if _find_optional_progress_column_index(header=header, field=field) >= 0:
        return matrix, header
    if field != "partial_amount":
        return matrix, header

    insert_at = len(header) + 1
    if before_field:
        before_idx = _find_optional_progress_column_index(header=header, field=before_field)
        if before_idx >= 0:
            insert_at = before_idx + 1

    await acall_with_backoff(
        worksheet.insert_cols,
        values=[[field]],
        col=insert_at,
    )
    refreshed = await afetch_values(_sheet_id(), tab_name)
    if not refreshed:
        return matrix, header
    refreshed_header = [str(cell or "").strip().lower() for cell in refreshed[0]]
    return refreshed, refreshed_header

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
    parse_errors: dict[str, str] = {}
    for raw in rows or []:
        row = _normalize(raw)
        fusion_id = str(row.get("fusion_id") or "").strip()
        fusion_name = str(row.get("fusion_name") or "").strip()
        status = str(row.get("status") or "").strip().lower()
        fusion_type_raw = row.get("fusion_type")
        raw_keys = sorted([str(key) for key in row.keys()]) if isinstance(row, dict) else []
        try:
            champion = str(row.get("champion") or "").strip()
            champion_image_url = str(row.get("champion_image_url") or "").strip()
            fusion_type = _normalize_fusion_type(fusion_type_raw)
            fusion_structure = str(row.get("fusion_structure") or "").strip()
            reward_type = str(row.get("reward_type") or "").strip()
            needed = _parse_int(_pick(row, "needed_total", "needed", "fusion.needed"))
            available = _parse_int(_pick(row, "fusion.available", "available"))
            start_at_utc = _parse_iso_utc(row.get("start_at_utc"))
            end_at_utc = _parse_iso_utc(row.get("end_at_utc"))
            announcement_channel_id = _parse_discord_id(row.get("announcement_channel_id"))
            opt_in_role_id = _parse_discord_id(row.get("opt_in_role_id"))
            announcement_message_id = _parse_discord_id(row.get("announcement_message_id"))
            published_at = _parse_iso_utc_optional(row.get("published_at"))
            last_announcement_refresh_at = _parse_iso_utc_optional(row.get("last_announcement_refresh_at"))
            last_announcement_status_hash = str(row.get("last_announcement_status_hash") or "").strip()
            parsed.append(
                FusionRow(
                    fusion_id=fusion_id,
                    fusion_name=fusion_name,
                    champion=champion,
                    champion_image_url=champion_image_url,
                    fusion_type=fusion_type,
                    fusion_structure=fusion_structure,
                    reward_type=reward_type,
                    needed=needed,
                    available=available,
                    start_at_utc=start_at_utc,
                    end_at_utc=end_at_utc,
                    announcement_channel_id=announcement_channel_id,
                    opt_in_role_id=opt_in_role_id,
                    announcement_message_id=announcement_message_id,
                    published_at=published_at,
                    last_announcement_refresh_at=last_announcement_refresh_at,
                    last_announcement_status_hash=last_announcement_status_hash,
                    status=status,
                )
            )
        except Exception as exc:
            failed_field = "unknown"
            for field_name, parser in (
                ("fusion_type", lambda: _normalize_fusion_type(fusion_type_raw)),
                ("needed", lambda: _parse_int(_pick(row, "needed_total", "needed", "fusion.needed"))),
                ("available", lambda: _parse_int(_pick(row, "fusion.available", "available"))),
                ("start_at_utc", lambda: _parse_iso_utc(row.get("start_at_utc"))),
                ("end_at_utc", lambda: _parse_iso_utc(row.get("end_at_utc"))),
                ("announcement_channel_id", lambda: _parse_discord_id(row.get("announcement_channel_id"))),
                ("opt_in_role_id", lambda: _parse_discord_id(row.get("opt_in_role_id"))),
                ("announcement_message_id", lambda: _parse_discord_id(row.get("announcement_message_id"))),
                ("published_at", lambda: _parse_iso_utc_optional(row.get("published_at"))),
                ("last_announcement_refresh_at", lambda: _parse_iso_utc_optional(row.get("last_announcement_refresh_at"))),
            ):
                try:
                    parser()
                except Exception:
                    failed_field = field_name
                    break
            log.warning(
                "fusion row skipped due to parse error",
                extra={
                    "fusion_id": fusion_id,
                    "fusion_name": fusion_name,
                    "status": status,
                    "fusion_type": str(fusion_type_raw or "").strip(),
                    "failed_field": failed_field,
                    "tab": tab_name,
                    "row_keys": raw_keys,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                },
                exc_info=True,
            )
            if status == "draft":
                log.info(
                    "draft fusion row skipped because it could not be parsed",
                    extra={"fusion_id": fusion_id, "fusion_name": fusion_name, "failed_field": failed_field},
                )
            if fusion_id:
                parse_errors[fusion_id] = failed_field
    global _LAST_FUSION_PARSE_ERRORS
    _LAST_FUSION_PARSE_ERRORS = parse_errors
    return tuple(parsed)


def get_last_fusion_parse_errors() -> dict[str, str]:
    return dict(_LAST_FUSION_PARSE_ERRORS)


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
                    milestones=_parse_milestones(row.get("milestones"), fusion_id=str(row.get("fusion_id") or "").strip(), event_id=str(row.get("event_id") or "").strip()),
                    is_estimated=_parse_bool(row.get("is_estimated")),
                    sort_order=_parse_int(row.get("sort_order")),
                    embed_title=str(row.get("embed_title") or "").strip(),
                    embed_description=str(row.get("embed_description") or "").strip(),
                    embed_footer=str(row.get("embed_footer") or "").strip(),
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
    target_norm = _normalize_fusion_id(fusion_id)
    filtered = [
        row
        for row in rows
        if isinstance(row, FusionEventRow)
        and (row.fusion_id == target or _normalize_fusion_id(row.fusion_id) == target_norm)
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


async def get_fusion_reminder_settings(*, now: dt.datetime | None = None) -> FusionReminderSettings:
    tab_name = _resolve_tab_name(_FUSION_REMINDER_SETTINGS_TAB_KEY)
    sheet_id, values, header = await _load_fusion_sheet_matrix(tab_name)
    key_idx = _resolve_header_index(
        tab_name=tab_name,
        header=header,
        field="setting_key",
        aliases_by_field=_FUSION_REMINDER_SETTINGS_COLUMN_HEADERS,
    )
    value_idx = _resolve_header_index(
        tab_name=tab_name,
        header=header,
        field="value",
        aliases_by_field=_FUSION_REMINDER_SETTINGS_COLUMN_HEADERS,
    )
    key_header = header[key_idx]
    value_header = header[value_idx]

    parsed: dict[str, object] = {}
    raw_types: dict[str, str] = {}
    raw_key_names: dict[str, str] = {}
    for row in values[1:]:
        key_value = row[key_idx] if key_idx < len(row) else ""
        key = str(key_value or "").strip().lower()
        if not key:
            continue
        value = row[value_idx] if value_idx < len(row) else ""
        parsed[key] = value
        raw_types[key] = type(value).__name__
        raw_key_names[key] = str(key_value or "").strip()

    grouped_post_time_utc = _local_time_to_utc_text(
        parsed.get("grouped_daily_post_time"),
        reference=now,
    )

    return FusionReminderSettings(
        start_offset_minutes=_parse_nonnegative_int(parsed.get("start_offset_minutes"), 360),
        end_lookahead_hours=_parse_nonnegative_int(parsed.get("end_lookahead_hours"), 24),
        upcoming_window_days=_parse_nonnegative_int(parsed.get("upcoming_window_days"), 2),
        group_events=_parse_bool(parsed.get("group_events")),
        grouped_post_time_utc=grouped_post_time_utc,
        include_start_events=_parse_bool(parsed.get("include_start_events", True)),
        include_ending_events=_parse_bool(parsed.get("include_ending_events")),
        include_upcoming_events=_parse_bool(parsed.get("include_upcoming_events")),
        grouped_embed_title=str(parsed.get("grouped_embed_title") or "").strip(),
        grouped_embed_description=str(parsed.get("grouped_embed_description") or "").strip(),
        grouped_live_label=str(parsed.get("grouped_live_label") or "").strip(),
        grouped_upcoming_label=str(parsed.get("grouped_upcoming_label") or "").strip(),
        grouped_ending_label=str(parsed.get("grouped_ending_label") or "").strip(),
        grouped_empty_value=str(parsed.get("grouped_empty_value") or "").strip(),
        grouped_jump_label=str(parsed.get("grouped_jump_label") or "").strip(),
        settings_source_tab=tab_name,
        settings_sheet_id_tail=_sheet_tail(sheet_id),
        settings_headers=tuple(header),
        settings_key_header=key_header,
        settings_value_header=value_header,
        settings_raw_values=parsed,
        settings_raw_types=raw_types,
        settings_raw_key_names=raw_key_names,
        settings_cache_status="not_cached",
        group_events_source=FusionReminderSettingSource(
            tab_name=tab_name,
            key_header=key_header,
            value_header=value_header,
            raw_value=str(parsed.get("group_events") or ""),
        ),
    )


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


async def get_last_reminder_sent_at(
    fusion_id: str,
    *,
    reminder_type: str,
) -> dt.datetime | None:
    """Return the latest sent_at marker for one fusion/reminder type."""

    tab_name, required_fields = _resolve_reminder_sheet_schema(include_sent_at=True)
    matrix = await afetch_values(_sheet_id(), tab_name)
    if not matrix:
        return None

    header = [str(cell or "").strip().lower() for cell in matrix[0]]
    index_by_field = {
        field: _resolve_header_index(tab_name=tab_name, header=header, field=field)
        for field in required_fields
    }
    fusion_idx = index_by_field["fusion_id"]
    reminder_idx = index_by_field["reminder_type"]
    sent_idx = index_by_field["sent_at_utc"]
    target_fusion = str(fusion_id or "").strip()
    target_type = str(reminder_type or "").strip()

    latest: dt.datetime | None = None
    for row in matrix[1:]:
        row_fusion = str(row[fusion_idx] if fusion_idx < len(row) else "").strip()
        row_type = str(row[reminder_idx] if reminder_idx < len(row) else "").strip()
        if row_fusion != target_fusion or row_type != target_type:
            continue
        sent_at = _parse_iso_utc_optional(row[sent_idx] if sent_idx < len(row) else "")
        if sent_at is not None and (latest is None or sent_at > latest):
            latest = sent_at
    return latest


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
    milestone_idx = index_by_field["milestone_key"]
    status_idx = index_by_field["status"]
    partial_idx = _find_optional_progress_column_index(header=header, field="partial_amount")
    target_fusion = str(fusion_id or "").strip()
    target_user = str(user_id or "").strip()

    rows: dict[str, str] = {}
    partials: dict[str, float] = {}
    for row in matrix[1:]:
        row_fusion = str(row[fusion_idx] if fusion_idx < len(row) else "").strip()
        row_user = str(row[user_idx] if user_idx < len(row) else "").strip()
        if row_fusion != target_fusion or row_user != target_user:
            continue
        event_id = str(row[event_idx] if event_idx < len(row) else "").strip()
        if not event_id:
            continue
        milestone_key = str(row[milestone_idx] if milestone_idx < len(row) else "").strip()
        status = _normalize_progress_status(row[status_idx] if status_idx < len(row) else "")
        key = f"{event_id}:{milestone_key}" if milestone_key else event_id
        rows[key] = status
        if partial_idx >= 0 and partial_idx < len(row):
            try:
                partials[key] = max(0.0, float(str(row[partial_idx]).strip() or "0"))
            except ValueError:
                continue
    return {"progress": rows, "partials": partials}


def _progress_row_matches(
    row: list[object],
    *,
    index_by_field: Mapping[str, int],
    row_key: tuple[str, str, str, str],
    status: str,
    partial_idx: int,
    partial_amount: float | None,
) -> bool:
    fusion_idx = index_by_field["fusion_id"]
    user_idx = index_by_field["user_id"]
    event_idx = index_by_field["event_id"]
    milestone_idx = index_by_field["milestone_key"]
    status_idx = index_by_field["status"]
    current_key = (
        str(row[fusion_idx] if fusion_idx < len(row) else "").strip(),
        str(row[user_idx] if user_idx < len(row) else "").strip(),
        str(row[event_idx] if event_idx < len(row) else "").strip(),
        str(row[milestone_idx] if milestone_idx < len(row) else "").strip(),
    )
    if current_key != row_key:
        return False
    current_status = str(row[status_idx] if status_idx < len(row) else "").strip().lower()
    if current_status != status:
        return False
    if partial_idx < 0:
        return True
    expected_partial = "" if partial_amount is None else f"{max(0.0, partial_amount):g}"
    current_partial = str(row[partial_idx] if partial_idx < len(row) else "").strip()
    return current_partial == expected_partial


async def _verify_progress_save(
    *,
    tab_name: str,
    index_by_field: Mapping[str, int],
    row_key: tuple[str, str, str, str],
    status: str,
    partial_idx: int,
    partial_amount: float | None,
) -> None:
    refreshed = await afetch_values(_sheet_id(), tab_name)
    for row in refreshed[1:]:
        if _progress_row_matches(
            row,
            index_by_field=index_by_field,
            row_key=row_key,
            status=status,
            partial_idx=partial_idx,
            partial_amount=partial_amount,
        ):
            return
    raise RuntimeError(
        "Fusion progress save verification failed "
        f"(tab={tab_name}, row_key={'|'.join(row_key)}, status={status})"
    )


async def get_progress_sheet_diagnostics() -> dict[str, object]:
    """Return non-secret progress-sheet schema details for failure logs."""

    tab_name = ""
    headers: list[str] = []
    try:
        tab_name, _aliases = _resolve_progress_sheet_schema()
        matrix = await afetch_values(_sheet_id(), tab_name)
        if matrix:
            headers = [str(cell or "").strip().lower() for cell in matrix[0]]
    except Exception as exc:
        return {
            "progress_config_key": _FUSION_PROGRESS_TAB_KEY,
            "tab_name": tab_name or str(cfg.get(_FUSION_PROGRESS_TAB_KEY) or "").strip(),
            "headers_resolved": ",".join(headers),
            "diagnostics_error": str(exc),
        }
    return {
        "progress_config_key": _FUSION_PROGRESS_TAB_KEY,
        "tab_name": tab_name,
        "headers_resolved": ",".join(headers),
    }


async def upsert_user_event_progress(
    fusion_id: str,
    user_id: str,
    event_id: str,
    status: str,
    updated_at: dt.datetime,
    milestone_key: str = "",
    partial_amount: float | None = None,
) -> FusionProgressSaveResult:
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
    milestone_idx = index_by_field["milestone_key"]
    status_idx = index_by_field["status"]
    updated_idx = index_by_field["updated_at_utc"]
    partial_idx = _find_optional_progress_column_index(header=header, field="partial_amount")
    target_fusion = str(fusion_id or "").strip()
    target_user = str(user_id or "").strip()
    target_event = str(event_id or "").strip()
    target_milestone = str(milestone_key or "").strip()
    timestamp = updated_at.astimezone(dt.timezone.utc).isoformat()
    row_key = (target_fusion, target_user, target_event, target_milestone)

    worksheet = await aget_worksheet(_sheet_id(), tab_name)
    if partial_idx < 0:
        matrix, header = await _ensure_optional_progress_header(
            worksheet=worksheet,
            tab_name=tab_name,
            matrix=matrix,
            header=header,
            field="partial_amount",
            before_field="updated_at_utc",
        )
        index_by_field = _resolve_progress_header_indices(
            tab_name=tab_name,
            header=header,
            include_updated_at=True,
        )
        fusion_idx = index_by_field["fusion_id"]
        user_idx = index_by_field["user_id"]
        event_idx = index_by_field["event_id"]
        milestone_idx = index_by_field["milestone_key"]
        status_idx = index_by_field["status"]
        updated_idx = index_by_field["updated_at_utc"]
        partial_idx = _find_optional_progress_column_index(header=header, field="partial_amount")
    for row_idx, row in enumerate(matrix[1:], start=2):
        row_fusion = str(row[fusion_idx] if fusion_idx < len(row) else "").strip()
        row_user = str(row[user_idx] if user_idx < len(row) else "").strip()
        row_event = str(row[event_idx] if event_idx < len(row) else "").strip()
        row_milestone = str(row[milestone_idx] if milestone_idx < len(row) else "").strip()
        if (row_fusion, row_user, row_event, row_milestone) != row_key:
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
        if partial_idx >= 0:
            partial_token = "" if partial_amount is None else f"{max(0.0, partial_amount):g}"
            await acall_with_backoff(
                worksheet.update,
                f"{_column_label(partial_idx)}{row_idx}",
                [[partial_token]],
                value_input_option="RAW",
            )
        await _verify_progress_save(
            tab_name=tab_name,
            index_by_field=index_by_field,
            row_key=row_key,
            status=normalized_status,
            partial_idx=partial_idx,
            partial_amount=partial_amount,
        )
        result = FusionProgressSaveResult(
            fusion_id=target_fusion,
            event_id=target_event,
            user_id=target_user,
            selected_status=normalized_status,
            tab_name=tab_name,
            headers=tuple(header),
            row_key=row_key,
            row_number=row_idx,
            operation="updated",
            saved=True,
        )
        log.info("fusion progress save succeeded", extra=_progress_save_log_fields(result))
        return result

    row_values = [""] * len(header)
    row_values[fusion_idx] = target_fusion
    row_values[user_idx] = target_user
    row_values[event_idx] = target_event
    row_values[milestone_idx] = target_milestone
    row_values[status_idx] = normalized_status
    row_values[updated_idx] = timestamp
    if partial_idx >= 0:
        row_values[partial_idx] = "" if partial_amount is None else f"{max(0.0, partial_amount):g}"
    await acall_with_backoff(
        worksheet.append_row,
        row_values,
        value_input_option="RAW",
    )
    await _verify_progress_save(
        tab_name=tab_name,
        index_by_field=index_by_field,
        row_key=row_key,
        status=normalized_status,
        partial_idx=partial_idx,
        partial_amount=partial_amount,
    )
    result = FusionProgressSaveResult(
        fusion_id=target_fusion,
        event_id=target_event,
        user_id=target_user,
        selected_status=normalized_status,
        tab_name=tab_name,
        headers=tuple(header),
        row_key=row_key,
        row_number=len(matrix) + 1,
        operation="inserted",
        saved=True,
    )
    log.info("fusion progress save succeeded", extra=_progress_save_log_fields(result))
    return result


def _progress_save_log_fields(result: FusionProgressSaveResult) -> dict[str, object]:
    return {
        "fusion_id": result.fusion_id,
        "event_id": result.event_id,
        "user_id": result.user_id,
        "selected_status": result.selected_status,
        "tab_name": result.tab_name,
        "headers_resolved": ",".join(result.headers),
        "row_key": "|".join(result.row_key),
        "row_number": result.row_number,
        "row_operation": result.operation,
        "save_success": result.saved,
        "save_failure_reason": result.failure_reason,
    }




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
    normalized = _normalize_fusion_type(row.fusion_type)
    if normalized in {"titan", "titan_event", "titan event"}:
        return "titan"
    return "fusion"


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


async def transition_fusion_to_ended(fusion_id: str) -> bool:
    """Set the configured Fusion row status to ended, if not already ended."""

    tab_name = _resolve_tab_name("FUSION_TAB")
    sheet_id, matrix, header = await _load_fusion_sheet_matrix(tab_name)
    row_idx = _resolve_fusion_row_index(
        fusion_id=fusion_id,
        header=header,
        matrix=matrix,
        tab_name=tab_name,
    )
    _require_fusion_headers(tab_name=tab_name, header=header, required=("status",))

    status_idx = header.index("status")
    matrix_row = matrix[row_idx - 1] if row_idx - 1 < len(matrix) else []
    current_status = str(matrix_row[status_idx] if status_idx < len(matrix_row) else "").strip().lower()
    if current_status == "ended":
        return False

    worksheet = await aget_worksheet(sheet_id, tab_name)
    await acall_with_backoff(
        worksheet.update,
        f"{_column_label(status_idx)}{row_idx}",
        [["ended"]],
        value_input_option="RAW",
    )
    register_cache_buckets()
    await cache.refresh_now(_FUSION_BUCKET, actor="fusion_status_ended")
    return True


__all__ = [
    "FusionEventRow",
    "FusionRow",
    "FusionUserEventProgressRow",
    "FusionProgressSaveResult",
    "FusionReminderSettings",
    "get_active_fusion",
    "get_ended_fusions",
    "get_published_fusions",
    "get_active_events",
    "derive_event_status",
    "get_valid_event_timing",
    "get_publishable_fusion",
    "get_last_fusion_parse_errors",
    "get_fusion_events",
    "get_sent_reminder_keys",
    "reminder_dedupe_backend_metadata",
    "get_user_event_progress",
    "get_progress_sheet_diagnostics",
    "get_upcoming_events",
    "get_fusion_reminder_settings",
    "get_last_reminder_sent_at",
    "mark_reminder_sent",
    "upsert_user_event_progress",
    "update_fusion_publication",
    "update_fusion_announcement_refresh_state",
    "transition_fusion_to_ended",
    "register_cache_buckets",
]
