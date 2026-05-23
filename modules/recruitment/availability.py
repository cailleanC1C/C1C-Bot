"""Helpers for recomputing clan availability based on reservations."""

from __future__ import annotations

import logging
import re
from typing import Sequence

from shared.sheets import async_core
from shared.sheets import recruitment
from shared.sheets import reservations

log = logging.getLogger(__name__)


async def adjust_manual_open_spots(clan_tag: str, delta: int) -> int:
    """Adjust manual open spots for ``clan_tag`` and return the new value."""
    try:
        log.info("adjust_manual_open_spots:start clan_tag=%s delta=%s", clan_tag, delta)
        entry = recruitment.find_clan_row(clan_tag)
        if entry is None:
            raise ValueError(f"Unknown clan tag: {clan_tag}")

        sheet_row, row = entry
        log.info("adjust_manual_open_spots:resolved_row clan_tag=%s sheet_row=%s", clan_tag, sheet_row)
        header_map = recruitment.get_clan_header_map()
        manual_open_index = header_map.get("manual_open_spots")
        open_index = header_map.get("open_spots")
        seen_index = header_map.get("manual_open_spots_seen")
        if manual_open_index is None or open_index is None or seen_index is None:
            raise ValueError(
                "clan header missing required manual_open_spots/open_spots/manual_open_spots_seen column"
            )
        log.info(
            "adjust_manual_open_spots:resolved_columns clan_tag=%s open_spots_col=%s seen_col=%s",
            clan_tag,
            _column_label(open_index),
            _column_label(seen_index),
        )
        manual_open = _parse_manual_open_spots(row, open_index=manual_open_index)
        current_available = _parse_manual_open_spots(row, open_index=open_index)
        seen_manual_open = _parse_manual_open_spots(row, open_index=seen_index)
        rebase_manual_open_spots = manual_open != seen_manual_open
        base_available = manual_open if rebase_manual_open_spots else current_available
        new_value = max(base_available + delta, 0)
        log.info(
            "adjust_manual_open_spots:computed clan_tag=%s af_before=%s af_after=%s delta=%s",
            clan_tag,
            current_available,
            new_value,
            delta,
        )

        updated_row = list(row)
        _ensure_row_length(updated_row, max(open_index, seen_index) + 1)
        updated_row[open_index] = str(new_value)
        updated_row[seen_index] = str(manual_open)

        sheet_id = recruitment.get_recruitment_sheet_id()
        tab_name = recruitment.get_clans_tab_name()
        worksheet = await async_core.aget_worksheet(sheet_id, tab_name)
        open_column = _column_label(open_index)
        seen_column = _column_label(seen_index)
        if abs(open_index - seen_index) == 1:
            first_index = min(open_index, seen_index)
            second_index = max(open_index, seen_index)
            first_value = str(new_value) if first_index == open_index else str(manual_open)
            second_value = str(manual_open) if second_index == seen_index else str(new_value)
            update_range = f"{_column_label(first_index)}{sheet_row}:{_column_label(second_index)}{sheet_row}"
            log.info("adjust_manual_open_spots:worksheet_update clan_tag=%s range=%s", clan_tag, update_range)
            update_result = await async_core.acall_with_backoff(
                worksheet.update,
                update_range,
                [[first_value, second_value]],
                value_input_option="RAW",
            )
            log.info("adjust_manual_open_spots:worksheet_update_result clan_tag=%s result=%r", clan_tag, update_result)
        else:
            open_range = f"{open_column}{sheet_row}"
            seen_range = f"{seen_column}{sheet_row}"
            log.info("adjust_manual_open_spots:worksheet_update clan_tag=%s range=%s", clan_tag, open_range)
            update_result = await async_core.acall_with_backoff(
                worksheet.update,
                open_range,
                [[str(new_value)]],
                value_input_option="RAW",
            )
            log.info("adjust_manual_open_spots:worksheet_update_result clan_tag=%s result=%r", clan_tag, update_result)
            log.info("adjust_manual_open_spots:worksheet_update clan_tag=%s range=%s", clan_tag, seen_range)
            seen_result = await async_core.acall_with_backoff(
                worksheet.update,
                seen_range,
                [[str(manual_open)]],
                value_input_option="RAW",
            )
            log.info("adjust_manual_open_spots:worksheet_update_result clan_tag=%s result=%r", clan_tag, seen_result)

        cache_result = recruitment.update_cached_clan_row(sheet_row, updated_row)
        log.info("adjust_manual_open_spots:cache_update_result clan_tag=%s result=%r", clan_tag, cache_result)
        refreshed = recruitment.find_clan_row(clan_tag)
        if refreshed is None:
            raise RuntimeError(f"clan cache refresh failed for {clan_tag}")
        _, refreshed_row = refreshed
        refreshed_after = _parse_manual_open_spots(refreshed_row, open_index=open_index)
        log.info("adjust_manual_open_spots:af_after_actual clan_tag=%s af_after_actual=%s", clan_tag, refreshed_after)
        log.info(
            "adjusted clan availability",
            extra={
                "clan_tag": _normalize_tag(clan_tag),
                "rebase_manual_open_spots": rebase_manual_open_spots,
                "open_spots_e_before": manual_open,
                "af_before": current_available,
                "af_after": new_value,
                "aj_before": seen_manual_open,
                "aj_after": manual_open,
                "delta": delta,
            },
        )
        return new_value
    except Exception:
        log.exception("adjust_manual_open_spots:exception clan_tag=%s delta=%s", clan_tag, delta)
        raise


async def recompute_clan_availability(
    clan_tag: str,
    *,
    guild: reservations.SupportsMemberLookup | None = None,
    resolver: reservations.ResolveUserFn | None = None,
) -> None:
    """Recompute AF/AH/AI for ``clan_tag`` and refresh the in-memory cache."""

    clan_entry = recruitment.find_clan_row(clan_tag)
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

    inactives_value = updated_row[inactives_index] if len(updated_row) > inactives_index else ""
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


__all__ = ["adjust_manual_open_spots", "recompute_clan_availability"]
