"""Helpers for recomputing clan availability based on reservations."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Callable, Sequence

from shared.sheets import async_core
from shared.sheets import recruitment
from shared.sheets import reservations

log = logging.getLogger(__name__)


AVAILABILITY_FIELDS = (
    "manual_open_spots",
    "open_spots",
    "inactives",
    "reservation_count",
    "reservation_summary",
    "manual_open_spots_seen",
    "clan_tag",
)

NUMERIC_AVAILABILITY_FIELDS = (
    "manual_open_spots",
    "open_spots",
    "inactives",
    "reservation_count",
    "manual_open_spots_seen",
)


@dataclass(frozen=True, slots=True)
class AvailabilityHeaderResolution:
    tab_name: str
    sheet_id_masked: str
    header_row_index: int
    header_row: tuple[str, ...]
    header_map: dict[str, int]
    configured_headers: dict[str, str]


@dataclass(frozen=True, slots=True)
class AvailabilityPreflightPlan:
    clan_tag: str
    sheet_row: int
    row: tuple[str, ...]
    headers: AvailabilityHeaderResolution
    numeric_values: dict[str, int]
    write_ranges: dict[str, str]


@dataclass(frozen=True, slots=True)
class ManualOpenSpotAdjustmentPlan:
    clan_tag: str
    delta: int
    sheet_row: int
    row: tuple[str, ...]
    manual_open_index: int
    open_index: int
    seen_index: int
    manual_open: int
    current_available: int
    seen_manual_open: int
    new_value: int
    tab_key: str
    tab_name: str
    manual_header_key: str
    manual_header_name: str
    open_range: str
    seen_range: str
    combined_range: str | None
    resolved_clan_tag: str
    headers: AvailabilityHeaderResolution


def _mask_sheet_id(sheet_id: str | None) -> str:
    text = (sheet_id or "").strip()
    if not text:
        return "<unset>"
    if len(text) <= 8:
        return f"{text[:2]}…{text[-2:]}" if len(text) > 4 else "****"
    return f"{text[:4]}…{text[-4:]}"


def _normalize_configured_header(value: Any) -> str:
    text = "" if value is None else str(value).strip().lower()
    return re.sub(r"[\s_]+", "", text)


def _diagnostic_header_values(header_row: Sequence[Any]) -> dict[str, str]:
    values: dict[str, str] = {}
    for index in (4, *range(31, 37)):
        values[_column_label(index)] = str(
            header_row[index] if index < len(header_row) else ""
        )
    return values


def _availability_header_map_columns(header_map: dict[str, int]) -> dict[str, str]:
    return {key: _column_label(index) for key, index in sorted(header_map.items())}


def _log_availability_diagnostics(
    *,
    reason: str,
    tab_name: str,
    sheet_id: str | None,
    header_row_index: int,
    header_row: Sequence[Any],
    configured_headers: dict[str, str] | None = None,
    header_map: dict[str, int] | None = None,
    missing_config_key: str | None = None,
    configured_header_value: str | None = None,
    clan_tag: str | None = None,
    sheet_row: int | None = None,
    operation: str | None = None,
) -> None:
    raw_values = _diagnostic_header_values(header_row)
    normalized_values = {
        column: _normalize_configured_header(value)
        for column, value in raw_values.items()
    }
    resolved_header_map = _availability_header_map_columns(header_map or {})
    extra = {
        "reason": reason,
        "configured_tab_name": tab_name,
        "clans_tab_name": tab_name,
        "sheet_id_masked": _mask_sheet_id(sheet_id),
        "header_row_index": header_row_index,
        "missing_config_key": missing_config_key,
        "configured_header_value": configured_header_value,
        "raw_header_diagnostics": raw_values,
        "normalized_header_diagnostics": normalized_values,
        "resolved_header_map": resolved_header_map,
        "configured_headers": dict(configured_headers or {}),
        "clan_tag": _normalize_tag(clan_tag),
        "bot_info_row_number": sheet_row,
        "operation": operation,
    }
    log_method = log.info if reason in {"resolved", "preflight_ok"} else log.warning
    if reason == "bot_info_row_not_found" and clan_tag:
        extra.update(_known_clan_tag_column_report(
            AvailabilityHeaderResolution(
                tab_name=tab_name,
                sheet_id_masked=_mask_sheet_id(sheet_id),
                header_row_index=header_row_index,
                header_row=tuple(str(cell) if cell is not None else "" for cell in header_row),
                header_map=dict(header_map or {}),
                configured_headers=dict(configured_headers or {}),
            ),
            clan_tag,
        ))
    log_method(
        "bot_info availability header diagnostics: operation=%s tab=%r sheet_id=%s header_row_index=%s "
        "reason=%s missing_config_key=%s configured_header_value=%r raw=%s normalized=%s header_map=%s",
        operation,
        tab_name,
        _mask_sheet_id(sheet_id),
        header_row_index,
        reason,
        missing_config_key,
        configured_header_value,
        raw_values,
        normalized_values,
        resolved_header_map,
        extra=extra,
    )


def _resolve_availability_headers() -> AvailabilityHeaderResolution:
    tab_name = recruitment.get_clans_tab_name()
    sheet_id = recruitment.get_recruitment_sheet_id()
    header_row_getter = getattr(recruitment, "get_clan_header_row", None)
    if callable(header_row_getter):
        try:
            header_row = list(header_row_getter(force=True))
        except TypeError:
            header_row = list(header_row_getter())
    else:
        header_row = []
    header_row_index = 3

    configured_headers: dict[str, str] = {}
    for field in AVAILABILITY_FIELDS:
        config_key = f"clans_header_{field}"
        value = recruitment.get_config_value(config_key, None)
        if value is None or not str(value).strip():
            _log_availability_diagnostics(
                reason="missing_required_config_key",
                tab_name=tab_name,
                sheet_id=sheet_id,
                header_row_index=header_row_index,
                header_row=header_row,
                configured_headers=configured_headers,
                missing_config_key=config_key,
            )
            raise ValueError(f"missing required Config key: {config_key}")
        configured_headers[field] = str(value).strip()

    lookup: dict[str, int] = {}
    for index, cell in enumerate(header_row):
        normalized = _normalize_configured_header(cell)
        if normalized:
            lookup[normalized] = index

    header_map: dict[str, int] = {}
    for field, configured_value in configured_headers.items():
        normalized = _normalize_configured_header(configured_value)
        if normalized not in lookup:
            _log_availability_diagnostics(
                reason="configured_header_not_found",
                tab_name=tab_name,
                sheet_id=sheet_id,
                header_row_index=header_row_index,
                header_row=header_row,
                configured_headers=configured_headers,
                header_map=header_map,
                configured_header_value=configured_value,
            )
            raise ValueError(
                f"configured bot_info header not found for {field}: {configured_value}"
            )
        header_map[field] = lookup[normalized]

    _log_availability_diagnostics(
        reason="resolved",
        tab_name=tab_name,
        sheet_id=sheet_id,
        header_row_index=header_row_index,
        header_row=header_row,
        configured_headers=configured_headers,
        header_map=header_map,
    )
    return AvailabilityHeaderResolution(
        tab_name=tab_name,
        sheet_id_masked=_mask_sheet_id(sheet_id),
        header_row_index=header_row_index,
        header_row=tuple(str(cell) if cell is not None else "" for cell in header_row),
        header_map=header_map,
        configured_headers=configured_headers,
    )


def _find_availability_clan_row(
    clan_tag: str, headers: AvailabilityHeaderResolution
) -> tuple[int, list[str]] | None:
    tag_index = headers.header_map["clan_tag"]
    normalized_target = _normalize_tag(clan_tag)
    try:
        try:
            clan_rows = recruitment.fetch_clans(force=True)
        except TypeError:
            clan_rows = recruitment.fetch_clans()
    except Exception:
        clan_rows = []
    for idx, row in enumerate(clan_rows):
        tag_value = row[tag_index] if tag_index < len(row) else ""
        if _normalize_tag(tag_value) == normalized_target:
            return idx + headers.header_row_index + 1, list(row)
    if not clan_rows:
        try:
            return recruitment.find_clan_row(clan_tag, force=True)
        except TypeError:
            return recruitment.find_clan_row(clan_tag)
    return None



def _known_clan_tag_column_report(headers: AvailabilityHeaderResolution, clan_tag: str) -> dict[str, object]:
    normalized_target = _normalize_tag(clan_tag)
    known_indices: dict[str, int] = {}
    if "clan_tag" in headers.header_map:
        known_indices["configured_clan_tag"] = headers.header_map["clan_tag"]
    try:
        for key, index in recruitment.get_clan_header_map().items():
            text = f"{key} {headers.header_row[index] if index < len(headers.header_row) else ''}"
            normalized = _normalize_configured_header(text)
            if "clan" in normalized and "tag" in normalized:
                known_indices[f"header_map:{key}"] = int(index)
    except Exception:
        pass
    try:
        clan_rows = recruitment.fetch_clans(force=True)
    except TypeError:
        clan_rows = recruitment.fetch_clans()
    except Exception:
        clan_rows = []
    matches: list[str] = []
    for label, index in known_indices.items():
        for offset, row in enumerate(clan_rows):
            value = row[index] if index < len(row) else ""
            if _normalize_tag(value) == normalized_target:
                matches.append(f"{label}:{_column_label(index)}:row{offset + headers.header_row_index + 1}")
                break
    return {
        "known_clan_tag_columns": {label: _column_label(index) for label, index in known_indices.items()},
        "searched_tag": normalized_target,
        "tag_found_in_known_clan_tag_column": bool(matches),
        "tag_column_matches": matches,
    }


def resolve_availability_clan_row(
    clan_tag: str, headers: AvailabilityHeaderResolution
) -> tuple[int, list[str]] | None:
    """Resolve a clan row using only the Config-defined clan_tag header."""

    return _find_availability_clan_row(clan_tag, headers)

def resolve_configured_clan_tag(clan_tag: str) -> str:
    """Resolve a clan tag from bot_info using the Config-provided clan_tag header."""
    headers = _resolve_availability_headers()
    entry = _find_availability_clan_row(clan_tag, headers)
    if entry is None:
        _log_availability_diagnostics(
            reason="bot_info_row_not_found",
            tab_name=headers.tab_name,
            sheet_id=recruitment.get_recruitment_sheet_id(),
            header_row_index=headers.header_row_index,
            header_row=headers.header_row,
            configured_headers=headers.configured_headers,
            header_map=headers.header_map,
            clan_tag=clan_tag,
            operation="resolve_configured_clan_tag",
        )
        raise ValueError(f"Unknown clan tag: {clan_tag}")
    _sheet_row, row = entry
    tag_index = headers.header_map["clan_tag"]
    value = row[tag_index] if tag_index < len(row) else ""
    return (str(value).strip() if value is not None else "") or clan_tag


async def preflight_clan_availability_update(
    clan_tag: str,
    *,
    delta: int = 0,
    operation: str = "preflight_clan_availability_update",
    find_clan_row_fn: Callable[
        [str, AvailabilityHeaderResolution], tuple[int, list[str]] | None
    ]
    | None = None,
) -> AvailabilityPreflightPlan:
    """Preflight Config-resolved bot_info availability dependencies before mutating flows."""
    headers = _resolve_availability_headers()
    sheet_id = recruitment.get_recruitment_sheet_id()
    try:
        worksheet = await async_core.aget_worksheet(sheet_id, headers.tab_name)
    except Exception:
        _log_availability_diagnostics(
            reason="worksheet_not_accessible",
            tab_name=headers.tab_name,
            sheet_id=sheet_id,
            header_row_index=headers.header_row_index,
            header_row=headers.header_row,
            configured_headers=headers.configured_headers,
            header_map=headers.header_map,
            clan_tag=clan_tag,
        )
        raise
    if worksheet is None:
        raise ValueError("bot_info worksheet not accessible")

    row_lookup = find_clan_row_fn or _find_availability_clan_row
    entry = row_lookup(clan_tag, headers)
    if entry is None:
        _log_availability_diagnostics(
            reason="bot_info_row_not_found",
            tab_name=headers.tab_name,
            sheet_id=sheet_id,
            header_row_index=headers.header_row_index,
            header_row=headers.header_row,
            configured_headers=headers.configured_headers,
            header_map=headers.header_map,
            clan_tag=clan_tag,
            operation=operation,
        )
        raise ValueError(f"Unknown clan tag: {clan_tag}")

    sheet_row, row = entry
    numeric_values: dict[str, int] = {}
    for field in NUMERIC_AVAILABILITY_FIELDS:
        index = headers.header_map[field]
        raw_value = row[index] if index < len(row) else ""
        numeric_values[field] = _parse_required_int(
            raw_value,
            clan_tag=clan_tag,
            delta=delta,
            sheet_row=sheet_row,
            tab_name=headers.tab_name,
            header_key=field,
            header_name=headers.configured_headers[field],
        )

    write_ranges = {
        field: f"{_column_label(headers.header_map[field])}{sheet_row}"
        for field in (
            "manual_open_spots",
            "open_spots",
            "inactives",
            "reservation_count",
            "reservation_summary",
            "manual_open_spots_seen",
        )
    }
    _log_availability_diagnostics(
        reason="preflight_ok",
        tab_name=headers.tab_name,
        sheet_id=sheet_id,
        header_row_index=headers.header_row_index,
        header_row=headers.header_row,
        configured_headers=headers.configured_headers,
        header_map=headers.header_map,
        clan_tag=clan_tag,
        sheet_row=sheet_row,
        operation="preflight_clan_availability_update",
    )
    return AvailabilityPreflightPlan(
        clan_tag=clan_tag,
        sheet_row=sheet_row,
        row=tuple(str(cell) if cell is not None else "" for cell in row),
        headers=headers,
        numeric_values=numeric_values,
        write_ranges=write_ranges,
    )


def _adjust_context(
    clan_tag: str,
    delta: int,
    *,
    sheet_row: int | None = None,
    tab_name: str | None = None,
    header_key: str = "manual_open_spots",
    header_name: str | None = None,
    raw_value: str | None = None,
    reason: str,
) -> dict[str, object]:
    return {
        "tag": _normalize_tag(clan_tag),
        "target_clan_tag": clan_tag,
        "bot_info_row_number": sheet_row,
        "configured_tab_key": "clans_tab",
        "configured_tab_name": tab_name,
        "configured_column_header_key": header_key,
        "configured_column_header_name": header_name,
        "raw_cell_value": raw_value,
        "attempted_delta": delta,
        "reason": reason,
    }


def _header_name(header_row: Sequence[str], index: int | None) -> str | None:
    if index is None or index < 0 or index >= len(header_row):
        return None
    return str(header_row[index]).strip() or None


def _parse_required_int(
    value: str | None,
    *,
    clan_tag: str,
    delta: int,
    sheet_row: int,
    tab_name: str,
    header_key: str,
    header_name: str | None,
) -> int:
    raw = "" if value is None else str(value).strip()
    if raw in {"", "-", "—"}:
        return 0
    if not re.fullmatch(r"[+-]?\d+", raw):
        reason = (
            "non_numeric_manual_open_spots_value"
            if header_key == "manual_open_spots"
            else f"non_numeric_{header_key}_value"
        )
        log.warning(
            "manual open spot adjustment preflight failed",
            extra=_adjust_context(
                clan_tag,
                delta,
                sheet_row=sheet_row,
                tab_name=tab_name,
                header_key=header_key,
                header_name=header_name,
                raw_value=value,
                reason=reason,
            ),
        )
        raise ValueError(reason)
    return int(raw)


async def preflight_manual_open_spots_adjustment(
    clan_tag: str,
    delta: int,
    *,
    find_clan_row_fn: Callable[
        [str, AvailabilityHeaderResolution], tuple[int, list[str]] | None
    ]
    | None = None,
) -> ManualOpenSpotAdjustmentPlan:
    """Resolve and validate the row, configured headers, parseable cells, and writable worksheet."""
    plan = await preflight_clan_availability_update(
        clan_tag, delta=delta, operation="adjust_manual_open_spots", find_clan_row_fn=find_clan_row_fn
    )
    header_map = plan.headers.header_map
    row = plan.row
    sheet_row = plan.sheet_row
    tab_name = plan.headers.tab_name

    manual_open_index = int(header_map["manual_open_spots"])
    open_index = int(header_map["open_spots"])
    seen_index = int(header_map["manual_open_spots_seen"])

    manual_open = plan.numeric_values["manual_open_spots"]
    current_available = plan.numeric_values["open_spots"]
    seen_manual_open = plan.numeric_values["manual_open_spots_seen"]

    base_available = (
        manual_open if manual_open != seen_manual_open else current_available
    )
    new_value = max(base_available + delta, 0)
    open_range = plan.write_ranges["open_spots"]
    seen_range = plan.write_ranges["manual_open_spots_seen"]
    tag_index = header_map.get("clan_tag")
    resolved_clan_tag = clan_tag
    if tag_index is not None and tag_index < len(row):
        candidate = str(row[tag_index]).strip()
        if candidate:
            resolved_clan_tag = candidate
    combined_range = None
    if abs(open_index - seen_index) == 1:
        combined_range = (
            f"{_column_label(min(open_index, seen_index))}{sheet_row}:"
            f"{_column_label(max(open_index, seen_index))}{sheet_row}"
        )

    return ManualOpenSpotAdjustmentPlan(
        clan_tag=clan_tag,
        delta=delta,
        sheet_row=sheet_row,
        row=row,
        manual_open_index=manual_open_index,
        open_index=open_index,
        seen_index=seen_index,
        manual_open=manual_open,
        current_available=current_available,
        seen_manual_open=seen_manual_open,
        new_value=new_value,
        tab_key="clans_tab",
        tab_name=tab_name,
        manual_header_key="manual_open_spots",
        manual_header_name=plan.headers.configured_headers["manual_open_spots"],
        open_range=open_range,
        seen_range=seen_range,
        combined_range=combined_range,
        resolved_clan_tag=resolved_clan_tag,
        headers=plan.headers,
    )


async def set_manual_open_spots(clan_tag: str, open_spots: int) -> tuple[int, int, str]:
    """Set visible open spots using the existing manual adjustment semantics.

    The existing manual adjustment flow writes the configured visible open-spots
    value and the manual-open-spots seen marker. It intentionally does not
    overwrite the manual baseline or reservation-derived fields. This wrapper
    computes the delta needed to reach the requested exact value, then delegates
    the actual write to ``adjust_manual_open_spots`` so emergency corrections do
    not invent a separate sheet mutation pattern.
    """

    if open_spots < 0:
        raise ValueError("open_spots must be >= 0")

    plan = await preflight_manual_open_spots_adjustment(clan_tag, 0)
    old_value = plan.current_available
    delta = open_spots - plan.new_value
    new_value = await adjust_manual_open_spots(clan_tag, delta)

    return old_value, new_value, plan.resolved_clan_tag


async def adjust_manual_open_spots(
    clan_tag: str,
    delta: int,
    *,
    find_clan_row_fn: Callable[
        [str, AvailabilityHeaderResolution], tuple[int, list[str]] | None
    ]
    | None = None,
) -> int:
    """Adjust manual open spots for ``clan_tag`` and return the new value."""
    plan: ManualOpenSpotAdjustmentPlan | None = None
    write_range = ""
    try:
        log.info("adjust_manual_open_spots:start clan_tag=%s delta=%s", clan_tag, delta)
        plan = await preflight_manual_open_spots_adjustment(
            clan_tag, delta, find_clan_row_fn=find_clan_row_fn
        )
        rebase_manual_open_spots = plan.manual_open != plan.seen_manual_open
        log.info(
            "adjust_manual_open_spots:resolved_row clan_tag=%s sheet_row=%s",
            clan_tag,
            plan.sheet_row,
        )
        log.info(
            "adjust_manual_open_spots:resolved_columns clan_tag=%s manual_open_col=%s open_spots_col=%s seen_col=%s",
            clan_tag,
            _column_label(plan.manual_open_index),
            _column_label(plan.open_index),
            _column_label(plan.seen_index),
        )
        log.info(
            "adjust_manual_open_spots:computed clan_tag=%s af_before=%s af_after=%s delta=%s",
            clan_tag,
            plan.current_available,
            plan.new_value,
            delta,
        )

        updated_row = list(plan.row)
        _ensure_row_length(updated_row, max(plan.open_index, plan.seen_index) + 1)
        updated_row[plan.open_index] = str(plan.new_value)
        updated_row[plan.seen_index] = str(plan.manual_open)

        sheet_id = recruitment.get_recruitment_sheet_id()
        worksheet = await async_core.aget_worksheet(sheet_id, plan.tab_name)
        if plan.combined_range:
            first_index = min(plan.open_index, plan.seen_index)
            second_index = max(plan.open_index, plan.seen_index)
            first_value = (
                str(plan.new_value)
                if first_index == plan.open_index
                else str(plan.manual_open)
            )
            second_value = (
                str(plan.manual_open)
                if second_index == plan.seen_index
                else str(plan.new_value)
            )
            write_range = plan.combined_range
            log.info(
                "adjust_manual_open_spots:worksheet_update clan_tag=%s range=%s",
                clan_tag,
                write_range,
            )
            update_result = await async_core.acall_with_backoff(
                worksheet.update,
                write_range,
                [[first_value, second_value]],
                value_input_option="RAW",
            )
            log.info(
                "adjust_manual_open_spots:worksheet_update_result clan_tag=%s result=%r",
                clan_tag,
                update_result,
            )
        else:
            write_range = plan.open_range
            log.info(
                "adjust_manual_open_spots:worksheet_update clan_tag=%s range=%s",
                clan_tag,
                write_range,
            )
            update_result = await async_core.acall_with_backoff(
                worksheet.update,
                write_range,
                [[str(plan.new_value)]],
                value_input_option="RAW",
            )
            log.info(
                "adjust_manual_open_spots:worksheet_update_result clan_tag=%s result=%r",
                clan_tag,
                update_result,
            )
            write_range = plan.seen_range
            log.info(
                "adjust_manual_open_spots:worksheet_update clan_tag=%s range=%s",
                clan_tag,
                write_range,
            )
            seen_result = await async_core.acall_with_backoff(
                worksheet.update,
                write_range,
                [[str(plan.manual_open)]],
                value_input_option="RAW",
            )
            log.info(
                "adjust_manual_open_spots:worksheet_update_result clan_tag=%s result=%r",
                clan_tag,
                seen_result,
            )

        cache_result = recruitment.update_cached_clan_row(plan.sheet_row, updated_row)
        log.info(
            "adjust_manual_open_spots:cache_update_result clan_tag=%s result=%r",
            clan_tag,
            cache_result,
        )
        refresh_lookup = find_clan_row_fn or _find_availability_clan_row
        refreshed = refresh_lookup(clan_tag, plan.headers)
        if refreshed is None:
            raise RuntimeError(f"clan cache refresh failed for {clan_tag}")
        _, refreshed_row = refreshed
        refreshed_after = _parse_manual_open_spots(
            refreshed_row, open_index=plan.open_index
        )
        log.info(
            "adjust_manual_open_spots:af_after_actual clan_tag=%s af_after_actual=%s",
            clan_tag,
            refreshed_after,
        )
        log.info(
            "adjusted clan availability",
            extra={
                "clan_tag": _normalize_tag(clan_tag),
                "rebase_manual_open_spots": rebase_manual_open_spots,
                "open_spots_e_before": plan.manual_open,
                "af_before": plan.current_available,
                "af_after": plan.new_value,
                "aj_before": plan.seen_manual_open,
                "aj_after": plan.manual_open,
                "delta": delta,
            },
        )
        return plan.new_value
    except Exception:
        extra = {
            "clan_tag": clan_tag,
            "delta": delta,
            "write_range": write_range or None,
        }
        if plan is not None:
            extra.update(
                _adjust_context(
                    clan_tag,
                    delta,
                    sheet_row=plan.sheet_row,
                    tab_name=plan.tab_name,
                    header_key=plan.manual_header_key,
                    header_name=plan.manual_header_name,
                    raw_value=(
                        plan.row[plan.manual_open_index]
                        if plan.manual_open_index < len(plan.row)
                        else ""
                    ),
                    reason=(
                        "sheet_update_failed" if write_range else "adjustment_failed"
                    ),
                )
            )
        log.exception(
            "adjust_manual_open_spots:exception clan_tag=%s delta=%s range=%s",
            clan_tag,
            delta,
            write_range or None,
            extra=extra,
        )
        raise


async def recompute_clan_availability(
    clan_tag: str,
    *,
    guild: reservations.SupportsMemberLookup | None = None,
    resolver: reservations.ResolveUserFn | None = None,
    find_clan_row_fn: Callable[
        [str, AvailabilityHeaderResolution], tuple[int, list[str]] | None
    ]
    | None = None,
) -> None:
    """Recompute Config-resolved bot_info availability for ``clan_tag`` and refresh cache."""

    plan = await preflight_clan_availability_update(
        clan_tag, delta=0, operation="recompute_clan_availability", find_clan_row_fn=find_clan_row_fn
    )
    sheet_row = plan.sheet_row
    row = plan.row
    header_map = plan.headers.header_map
    manual_open_index = header_map["manual_open_spots"]
    open_index = header_map["open_spots"]
    seen_index = header_map["manual_open_spots_seen"]
    inactives_index = header_map["inactives"]
    reservation_count_index = header_map["reservation_count"]
    reservation_summary_index = header_map["reservation_summary"]

    manual_open = plan.numeric_values["manual_open_spots"]
    current_available = plan.numeric_values["open_spots"]
    seen_manual_open = plan.numeric_values["manual_open_spots_seen"]
    rebase_manual_open_spots = manual_open != seen_manual_open
    base_available = manual_open if rebase_manual_open_spots else current_available

    active_reservations = await reservations.get_active_reservations_for_clan(clan_tag)
    reservation_count = len(active_reservations)
    available_after_reservations = max(base_available - reservation_count, 0)

    names = await reservations.resolve_reservation_names(
        active_reservations,
        guild=guild,
        resolver=resolver,
    )
    reservation_summary = _format_reservation_summary(reservation_count, names)

    updated_row = list(row)
    _ensure_row_length(
        updated_row,
        max(
            open_index,
            seen_index,
            inactives_index,
            reservation_count_index,
            reservation_summary_index,
        )
        + 1,
    )

    inactives_value = updated_row[inactives_index]
    updated_row[open_index] = str(available_after_reservations)
    updated_row[seen_index] = str(manual_open)
    updated_row[reservation_count_index] = str(reservation_count)
    updated_row[reservation_summary_index] = reservation_summary

    sheet_id = recruitment.get_recruitment_sheet_id()
    worksheet = await async_core.aget_worksheet(sheet_id, plan.headers.tab_name)

    writes = [
        ("open_spots", plan.write_ranges["open_spots"], available_after_reservations),
        (
            "manual_open_spots_seen",
            plan.write_ranges["manual_open_spots_seen"],
            manual_open,
        ),
        ("inactives", plan.write_ranges["inactives"], inactives_value),
        (
            "reservation_count",
            plan.write_ranges["reservation_count"],
            reservation_count,
        ),
        (
            "reservation_summary",
            plan.write_ranges["reservation_summary"],
            reservation_summary,
        ),
    ]
    for field, write_range, value in writes:
        try:
            log.info(
                "recompute_clan_availability:worksheet_update clan_tag=%s field=%s range=%s",
                clan_tag,
                field,
                write_range,
            )
            await async_core.acall_with_backoff(
                worksheet.update,
                write_range,
                [[value]],
                value_input_option="RAW",
            )
        except Exception:
            log.exception(
                "recompute_clan_availability:worksheet_update_exception clan_tag=%s field=%s range=%s",
                clan_tag,
                field,
                write_range,
                extra={
                    "clan_tag": _normalize_tag(clan_tag),
                    "field": field,
                    "write_range": write_range,
                },
            )
            raise

    recruitment.update_cached_clan_row(sheet_row, updated_row)

    log.debug(
        "recomputed clan availability",
        extra={
            "clan_tag": _normalize_tag(clan_tag),
            "manual_open": manual_open,
            "rebase_manual_open_spots": rebase_manual_open_spots,
            "af_before": current_available,
            "active_reservations": reservation_count,
            "available_after_reservations": available_after_reservations,
            "aj_before": seen_manual_open,
            "aj_after": manual_open,
            "resolved_header_map": _availability_header_map_columns(header_map),
        },
    )


def _parse_manual_open_spots(row: Sequence[str], *, open_index: int = 4) -> int:
    if open_index < 0 or len(row) <= open_index:
        return 0
    return _to_int(row[open_index])


def _format_reservation_summary(count: int, names: Sequence[str]) -> str:
    if count <= 0:
        return ""
    if names:
        return f"{count} -> {', '.join(names)}"
    return f"{count} ->"


def _ensure_row_length(row: list[str], length: int) -> None:
    if len(row) >= length:
        return
    row.extend("" for _ in range(length - len(row)))


def _column_label(index: int) -> str:
    if index < 0:
        raise ValueError("column index must be non-negative")
    value = index + 1
    label = ""
    while value > 0:
        value, remainder = divmod(value - 1, 26)
        label = chr(65 + remainder) + label
    return label or "A"


def _to_int(value: str | None) -> int:
    if not value:
        return 0
    match = re.search(r"-?\d+", str(value))
    if not match:
        return 0
    try:
        return int(match.group(0))
    except ValueError:
        return 0


def _normalize_tag(tag: str | None) -> str:
    text = "" if tag is None else str(tag).strip().upper()
    return "".join(ch for ch in text if ch.isalnum())


__all__ = [
    "adjust_manual_open_spots",
    "preflight_manual_open_spots_adjustment",
    "preflight_clan_availability_update",
    "resolve_availability_clan_row",
    "resolve_configured_clan_tag",
    "recompute_clan_availability",
]
