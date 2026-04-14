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


@dataclass(frozen=True, slots=True)
class FusionRow:
    fusion_id: str
    fusion_name: str
    champion: str
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
                    start_at_utc=_parse_iso_utc(row.get("start_at_utc")),
                    end_at_utc=_parse_iso_utc(row.get("end_at_utc")),
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


async def get_publishable_fusion() -> FusionRow | None:
    """Return the best fusion row for publish flow selection."""

    fusion_bucket, _ = register_cache_buckets()
    rows = [row for row in await _cached_rows(fusion_bucket) if isinstance(row, FusionRow)]
    if not rows:
        return None

    for status in ("active", "published", "draft"):
        matches = [row for row in rows if row.status.casefold() == status]
        if matches:
            matches.sort(key=lambda row: (row.start_at_utc, row.fusion_id), reverse=True)
            return matches[0]

    rows.sort(key=lambda row: (row.start_at_utc, row.fusion_id), reverse=True)
    return rows[0]


async def update_fusion_publication(
    fusion_id: str,
    *,
    announcement_message_id: int,
    published_at: dt.datetime,
    set_published_status: bool,
) -> None:
    """Write publish metadata back to the fusion row in the configured sheet."""

    tab_name = _resolve_tab_name("FUSION_TAB")
    sheet_id = _sheet_id()
    matrix = await afetch_values(sheet_id, tab_name)
    if not matrix:
        raise RuntimeError("Fusion sheet is empty")

    header = [str(cell or "").strip().lower() for cell in matrix[0]]
    row_idx: int | None = None
    fusion_col = header.index("fusion_id") if "fusion_id" in header else -1
    if fusion_col < 0:
        raise RuntimeError("Fusion sheet missing fusion_id column")

    for idx, row in enumerate(matrix[1:], start=2):
        cell = str(row[fusion_col] if fusion_col < len(row) else "").strip()
        if cell == fusion_id:
            row_idx = idx
            break

    if row_idx is None:
        raise RuntimeError(f"Fusion row not found for fusion_id={fusion_id}")

    required_cols = ["announcement_message_id", "published_at"]
    if set_published_status:
        required_cols.append("status")

    missing = [col for col in required_cols if col not in header]
    if missing:
        raise RuntimeError(f"Fusion sheet missing columns: {', '.join(missing)}")

    worksheet = await aget_worksheet(sheet_id, tab_name)
    updates = {
        "announcement_message_id": str(announcement_message_id),
        "published_at": published_at.astimezone(dt.timezone.utc).isoformat(),
    }
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


__all__ = [
    "FusionEventRow",
    "FusionRow",
    "get_active_fusion",
    "get_publishable_fusion",
    "get_fusion_events",
    "update_fusion_publication",
    "register_cache_buckets",
]
