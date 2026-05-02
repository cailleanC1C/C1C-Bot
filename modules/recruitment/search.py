"""Shared recruitment roster helpers kept import-safe for C-03 guardrail."""

from __future__ import annotations

import logging
from typing import Iterable, Sequence

from shared.sheets import async_facade as sheets
from shared.sheets import recruitment as sheet_recruitment
from shared.sheets.recruitment import (
    DEFAULT_ROSTER_INDEX,
    FALLBACK_INACTIVES_INDEX,
    FALLBACK_OPEN_SPOTS_INDEX,
    FALLBACK_RESERVED_INDEX,
    RecruitmentClanRecord,
)

from modules.recruitment import search_helpers
from modules.recruitment.search_helpers import parse_inactives_num, parse_spots_num

COL_S_CVC = 18
COL_T_SIEGE = 19
COL_U_STYLE = 20

__all__ = [
    "fetch_roster_records",
    "filter_records",
    "normalize_records",
    "enforce_inactives_only",
    "filter_records_with_diagnostics",
]

log = logging.getLogger(__name__)


def _norm(value: str | None) -> str:
    return (value or "").strip().upper()


def _difficulty_match(cell_value: str, wanted: str | None) -> bool:
    if not wanted:
        return True
    token = _norm(wanted)
    cell = _norm(cell_value)
    if token in {"UNM", "ULTRA NIGHTMARE", "ULTRA-NIGHTMARE", "ULTRANIGHTMARE"}:
        return any(k in cell for k in ("UNM", "ULTRA NIGHTMARE", "ULTRA-NIGHTMARE", "ULTRANIGHTMARE"))
    return token in cell


def _cell(row: Sequence[str], header_map: dict[str, int], key: str) -> str:
    idx = header_map.get(key)
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    return str(row[idx] or "")


def _flag_ok(row: Sequence[str], idx: int, expected: str | None) -> bool:
    token = _norm(expected)
    if token in {"", "—", "-", "ANY", "NONE", "NULL"}:
        return True
    if idx < 0 or idx >= len(row):
        return False
    return str(row[idx] or "").strip() == expected


def _playstyle_ok(row: Sequence[str], wanted: str | None) -> bool:
    if not wanted:
        return True
    if COL_U_STYLE >= len(row):
        return False
    return search_helpers._playstyle_ok(str(row[COL_U_STYLE] or ""), wanted)



async def fetch_roster_records(*, force: bool = False) -> list[RecruitmentClanRecord]:
    """Load normalized clan roster records from Sheets."""

    records: Iterable[RecruitmentClanRecord] = await sheets.fetch_clan_records(
        force=force
    )
    return normalize_records(list(records))


def _ensure_record(
    entry: RecruitmentClanRecord | Sequence[str],
    *,
    header_map: dict[str, int] | None,
) -> tuple[RecruitmentClanRecord, dict[str, int] | None]:
    if isinstance(entry, RecruitmentClanRecord):
        if not entry.roster.strip():
            raise ValueError("blank roster cell")
        return entry, header_map

    try:
        mapping = header_map or sheet_recruitment.get_clan_header_map()
    except Exception:
        mapping = header_map or {}

    def _cell(idx: int | None) -> str:
        if idx is None or idx < 0:
            return ""
        if idx >= len(entry):
            return ""
        value = entry[idx]
        return "" if value is None else str(value)

    row = tuple("" if cell is None else str(cell) for cell in entry)
    roster_idx = mapping.get("roster")
    if roster_idx is None:
        roster_idx = DEFAULT_ROSTER_INDEX
    roster_cell = _cell(roster_idx).strip()
    if not roster_cell:
        raise ValueError("blank roster cell")

    open_idx = mapping.get("open_spots")
    if open_idx is None:
        open_idx = FALLBACK_OPEN_SPOTS_INDEX
    inactives_idx = mapping.get("inactives")
    if inactives_idx is None:
        inactives_idx = FALLBACK_INACTIVES_INDEX
    reserved_idx = mapping.get("reserved")
    if reserved_idx is None:
        reserved_idx = FALLBACK_RESERVED_INDEX

    open_spots = parse_spots_num(_cell(open_idx))
    inactives = parse_inactives_num(_cell(inactives_idx))
    reserved = parse_spots_num(_cell(reserved_idx))

    record = RecruitmentClanRecord(
        row=row,
        open_spots=open_spots,
        inactives=inactives,
        reserved=reserved,
        roster=roster_cell,
    )
    return record, mapping


def normalize_records(
    records: Sequence[RecruitmentClanRecord | Sequence[str]],
) -> list[RecruitmentClanRecord]:
    normalized: list[RecruitmentClanRecord] = []
    header_map: dict[str, int] | None = None
    for entry in records or []:
        try:
            record, header_map = _ensure_record(entry, header_map=header_map)
            normalized.append(record)
        except Exception:
            continue
    return normalized


def filter_records(
    records: Sequence[RecruitmentClanRecord | Sequence[str]],
    *,
    cb: str | None,
    hydra: str | None,
    chimera: str | None,
    cvc: str | None,
    siege: str | None,
    playstyle: str | None,
    roster_mode: str | None,
) -> list[RecruitmentClanRecord]:
    """Apply sheet and roster-mode filters to ``records``."""

    matches, _diag = filter_records_with_diagnostics(
        records,
        cb=cb,
        hydra=hydra,
        chimera=chimera,
        cvc=cvc,
        siege=siege,
        playstyle=playstyle,
        roster_mode=roster_mode,
    )
    return matches


def filter_records_with_diagnostics(
    records: Sequence[RecruitmentClanRecord | Sequence[str]],
    *,
    cb: str | None,
    hydra: str | None,
    chimera: str | None,
    cvc: str | None,
    siege: str | None,
    playstyle: str | None,
    roster_mode: str | None,
) -> tuple[list[RecruitmentClanRecord], dict[str, int]]:
    """Apply filters and return matches plus reason counts for dropped rows."""

    normalized = normalize_records(records)
    matches: list[RecruitmentClanRecord] = []
    diagnostics: dict[str, int] = {}

    try:
        header_map = sheet_recruitment.get_clan_header_map()
    except Exception:
        header_map = {}

    primary_matches: list[RecruitmentClanRecord] = []
    range_matches: list[RecruitmentClanRecord] = []

    for record in normalized:
        try:
            row = record.row

            cb_primary_ok = _difficulty_match(_cell(row, header_map, "cb"), cb)
            hydra_primary_ok = _difficulty_match(_cell(row, header_map, "hydra"), hydra)
            chimera_primary_ok = _difficulty_match(_cell(row, header_map, "chimera"), chimera)

            cb_range_ok = _difficulty_match(_cell(row, header_map, "cb_range"), cb)
            hydra_range_ok = _difficulty_match(_cell(row, header_map, "hydra_range"), hydra)
            chimera_range_ok = _difficulty_match(_cell(row, header_map, "chimera_range"), chimera)

            primary_ok = cb_primary_ok and hydra_primary_ok and chimera_primary_ok
            fallback_ok = (cb_primary_ok or cb_range_ok) and (hydra_primary_ok or hydra_range_ok) and (chimera_primary_ok or chimera_range_ok)

            if not fallback_ok:
                diagnostics["difficulty"] = diagnostics.get("difficulty", 0) + 1
                continue
            if not _flag_ok(row, COL_S_CVC, cvc):
                diagnostics["cvc"] = diagnostics.get("cvc", 0) + 1
                continue
            if not _flag_ok(row, COL_T_SIEGE, siege):
                diagnostics["siege"] = diagnostics.get("siege", 0) + 1
                continue
            if not _playstyle_ok(row, playstyle):
                diagnostics["playstyle"] = diagnostics.get("playstyle", 0) + 1
                continue
            if roster_mode == "open" and record.open_spots <= 0:
                diagnostics["roster_open_spots"] = diagnostics.get("roster_open_spots", 0) + 1
                continue
            if roster_mode == "full" and record.open_spots > 0:
                diagnostics["roster_full_only"] = diagnostics.get("roster_full_only", 0) + 1
                continue
            if roster_mode == "inactives" and record.inactives <= 0:
                diagnostics["roster_inactives"] = diagnostics.get("roster_inactives", 0) + 1
                continue

            if primary_ok:
                primary_matches.append(record)
            else:
                range_matches.append(record)
        except Exception:
            diagnostics["exception"] = diagnostics.get("exception", 0) + 1
            continue

    matches = list(primary_matches)
    if len(matches) < 3:
        matches.extend(range_matches)
    else:
        diagnostics["range_fallback_held"] = len(range_matches)

    return matches, diagnostics


def enforce_inactives_only(
    records: Sequence[RecruitmentClanRecord | Sequence[str]],
    roster_mode: str | None,
    *,
    context: str,
) -> list[RecruitmentClanRecord]:
    """Re-apply the inactives-only guard and emit a debug log when rows drop."""

    normalized = normalize_records(records)

    if roster_mode != "inactives":
        return normalized

    filtered = [record for record in normalized if record.inactives > 0]
    removed = len(normalized) - len(filtered)
    if removed:
        log.debug(
            "recruitment dropped rows failing inactives guard",
            extra={"removed": removed, "context": context},
        )
    return filtered
