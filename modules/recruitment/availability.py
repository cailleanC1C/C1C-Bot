"""Helpers for recomputing clan availability based on reservations."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Sequence

from shared.sheets import async_core
from shared.sheets import recruitment
from shared.sheets import reservations

log = logging.getLogger(__name__)


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
    clan_tag: str, delta: int
) -> ManualOpenSpotAdjustmentPlan:
    """Resolve and validate the row, configured headers, parseable cells, and writable worksheet."""
    tab_name = recruitment.get_clans_tab_name()
    entry = recruitment.find_clan_row(clan_tag, force=True)
    if entry is None:
        log.warning(
            "manual open spot adjustment preflight failed",
            extra=_adjust_context(
                clan_tag, delta, tab_name=tab_name, reason="bot_info_row_not_found"
            ),
        )
        raise ValueError(f"Unknown clan tag: {clan_tag}")

    sheet_row, row = entry
    header_map = recruitment.get_clan_header_map()
    header_row_getter = getattr(recruitment, "get_clan_header_row", None)
    header_row = header_row_getter() if callable(header_row_getter) else []
    required_keys = ("manual_open_spots", "open_spots", "manual_open_spots_seen")
    missing = [key for key in required_keys if header_map.get(key) is None]
    if missing:
        missing_key = missing[0]
        log.warning(
            "manual open spot adjustment preflight failed",
            extra=_adjust_context(
                clan_tag,
                delta,
                sheet_row=sheet_row,
                tab_name=tab_name,
                header_key=missing_key,
                reason="configured_column_not_found",
            ),
        )
        raise ValueError(f"clan header missing required column: {missing_key}")

    manual_open_index = int(header_map["manual_open_spots"])
    open_index = int(header_map["open_spots"])
    seen_index = int(header_map["manual_open_spots_seen"])

    manual_header_name = _header_name(header_row, manual_open_index)
    manual_raw = row[manual_open_index] if manual_open_index < len(row) else ""
    open_raw = row[open_index] if open_index < len(row) else ""
    seen_raw = row[seen_index] if seen_index < len(row) else ""
    manual_open = _parse_required_int(
        manual_raw,
        clan_tag=clan_tag,
        delta=delta,
        sheet_row=sheet_row,
        tab_name=tab_name,
        header_key="manual_open_spots",
        header_name=manual_header_name,
    )
    current_available = _parse_required_int(
        open_raw,
        clan_tag=clan_tag,
        delta=delta,
        sheet_row=sheet_row,
        tab_name=tab_name,
        header_key="open_spots",
        header_name=_header_name(header_row, open_index),
    )
    seen_manual_open = _parse_required_int(
        seen_raw,
        clan_tag=clan_tag,
        delta=delta,
        sheet_row=sheet_row,
        tab_name=tab_name,
        header_key="manual_open_spots_seen",
        header_name=_header_name(header_row, seen_index),
    )

    base_available = (
        manual_open if manual_open != seen_manual_open else current_available
    )
    new_value = max(base_available + delta, 0)
    open_range = f"{_column_label(open_index)}{sheet_row}"
    seen_range = f"{_column_label(seen_index)}{sheet_row}"
    combined_range = None
    if abs(open_index - seen_index) == 1:
        combined_range = (
            f"{_column_label(min(open_index, seen_index))}{sheet_row}:"
            f"{_column_label(max(open_index, seen_index))}{sheet_row}"
        )

    # Resolve the worksheet during preflight so missing tabs/permissions fail before Discord mutations.
    sheet_id = recruitment.get_recruitment_sheet_id()
    try:
        await async_core.aget_worksheet(sheet_id, tab_name)
    except Exception:
        log.exception(
            "manual open spot adjustment preflight failed",
            extra=_adjust_context(
                clan_tag,
                delta,
                sheet_row=sheet_row,
                tab_name=tab_name,
                header_key="manual_open_spots",
                header_name=manual_header_name,
                raw_value=manual_raw,
                reason="worksheet_not_writable",
            ),
        )
        raise

    return ManualOpenSpotAdjustmentPlan(
        clan_tag=clan_tag,
        delta=delta,
        sheet_row=sheet_row,
        row=tuple(str(cell) if cell is not None else "" for cell in row),
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
        manual_header_name=manual_header_name or "",
        open_range=open_range,
        seen_range=seen_range,
        combined_range=combined_range,
    )


async def adjust_manual_open_spots(clan_tag: str, delta: int) -> int:
    """Adjust manual open spots for ``clan_tag`` and return the new value."""
    plan: ManualOpenSpotAdjustmentPlan | None = None
    write_range = ""
    try:
        log.info("adjust_manual_open_spots:start clan_tag=%s delta=%s", clan_tag, delta)
        plan = await preflight_manual_open_spots_adjustment(clan_tag, delta)
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
        refreshed = recruitment.find_clan_row(clan_tag, force=True)
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
) -> None:
    """Recompute AF/AH/AI for ``clan_tag`` and refresh the in-memory cache."""

    clan_entry = recruitment.find_clan_row(clan_tag, force=True)
    if clan_entry is None:
        raise ValueError(f"Unknown clan tag: {clan_tag}")

    sheet_row, row = clan_entry
    header_map = recruitment.get_clan_header_map()
    manual_open_index = header_map.get("manual_open_spots")
    open_index = header_map.get("open_spots")
    seen_index = header_map.get("manual_open_spots_seen")
    inactives_index = header_map.get("inactives")
    reservation_count_index = header_map.get("reservation_count")
    reservation_summary_index = header_map.get("reservation_summary")
    if (
        manual_open_index is None
        or open_index is None
        or seen_index is None
        or inactives_index is None
        or reservation_count_index is None
        or reservation_summary_index is None
    ):
        raise ValueError(
            "clan header missing required manual_open_spots/open_spots/manual_open_spots_seen/inactives/reservation_count/reservation_summary column"
        )
    manual_open = _parse_manual_open_spots(row, open_index=manual_open_index)
    current_available = _parse_manual_open_spots(row, open_index=open_index)
    seen_manual_open = _parse_manual_open_spots(row, open_index=seen_index)
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

    inactives_value = (
        updated_row[inactives_index] if len(updated_row) > inactives_index else ""
    )
    updated_row[open_index] = str(available_after_reservations)
    updated_row[seen_index] = str(manual_open)
    updated_row[reservation_count_index] = str(reservation_count)
    updated_row[reservation_summary_index] = reservation_summary

    sheet_id = recruitment.get_recruitment_sheet_id()
    tab_name = recruitment.get_clans_tab_name()
    worksheet = await async_core.aget_worksheet(sheet_id, tab_name)

    await async_core.acall_with_backoff(
        worksheet.update,
        f"{_column_label(open_index)}{sheet_row}",
        [[available_after_reservations]],
        value_input_option="RAW",
    )
    await async_core.acall_with_backoff(
        worksheet.update,
        f"{_column_label(seen_index)}{sheet_row}",
        [[manual_open]],
        value_input_option="RAW",
    )
    await async_core.acall_with_backoff(
        worksheet.update,
        f"{_column_label(inactives_index)}{sheet_row}",
        [[inactives_value]],
        value_input_option="RAW",
    )
    await async_core.acall_with_backoff(
        worksheet.update,
        f"{_column_label(reservation_count_index)}{sheet_row}",
        [[reservation_count]],
        value_input_option="RAW",
    )
    await async_core.acall_with_backoff(
        worksheet.update,
        f"{_column_label(reservation_summary_index)}{sheet_row}",
        [[reservation_summary]],
        value_input_option="RAW",
    )

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
    "recompute_clan_availability",
]
