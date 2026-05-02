"""Recruitment-specific Google Sheets accessors."""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, cast

from shared.sheets import core
from shared.sheets.async_core import afetch_records, afetch_values
from shared.sheets.cache_service import cache

log = logging.getLogger(__name__)

_CACHE_TTL = int(os.getenv("SHEETS_CACHE_TTL_SEC", "900"))
_CONFIG_TTL = int(os.getenv("SHEETS_CONFIG_CACHE_TTL_SEC", str(_CACHE_TTL)))

_CONFIG_CACHE: Dict[str, str] | None = None
_CONFIG_CACHE_TS: float = 0.0

_CLAN_ROWS: List[List[str]] | None = None
_CLAN_ROWS_TS: float = 0.0
_CLAN_TAG_INDEX: Dict[str, List[str]] | None = None
_CLAN_TAG_INDEX_TS: float = 0.0

_TEMPLATE_ROWS: List[Dict[str, Any]] | None = None
_TEMPLATE_ROWS_TS: float = 0.0

_CLAN_HEADER_ROW: List[str] | None = None
_CLAN_HEADER_MAP: Dict[str, int] | None = None
_CLAN_HEADER_TS: float = 0.0

_CLAN_RECORDS: List["RecruitmentClanRecord"] | None = None
_CLAN_RECORDS_TS: float = 0.0


@dataclass(frozen=True, slots=True)
class RecruitmentClanRecord:
    """Normalized representation of a recruitment roster row."""

    row: Sequence[str]
    open_spots: int
    inactives: int
    reserved: int
    roster: str


@dataclass(frozen=True, slots=True)
class ReservationsConfig:
    """Typed view of reservations-related sheet configuration."""

    enabled: bool
    tab_name: str


DEFAULT_ROSTER_INDEX = 4
# Fallback indices for legacy rows when header resolution is unavailable.
FALLBACK_OPEN_SPOTS_INDEX = 31  # Column AF
FALLBACK_INACTIVES_INDEX = 32  # Column AG
FALLBACK_RESERVED_INDEX = 34  # Column AI


def _column_aliases(*aliases: str) -> tuple[str, ...]:
    return tuple(aliases)


HEADER_MAP: Dict[str, tuple[str, ...]] = {
    "cb": _column_aliases("cb", "clan boss", "clanboss"),
    "hydra": _column_aliases("hydra", "hydra difficulty"),
    "chimera": _column_aliases("chimera", "chimera difficulty"),
    "cb_range": _column_aliases("cb range", "clan boss range", "clanboss range"),
    "hydra_range": _column_aliases("hydra range", "hydra difficulty range"),
    "chimera_range": _column_aliases("chimera range", "chimera difficulty range"),
    "open_spots": _column_aliases(
        "open spots",
        "spots",
        "open spot",
        "spots avail",
        "spots available",
        "open slots",
        "available slots",
        "avail. slots",
        "manual open spots",
    ),
    "inactives": _column_aliases(
        "inactives",
        "inactive",
        "inactive count",
        "inactive players",
        "manual inactives",
    ),
    "reserved": _column_aliases(
        "reserved",
        "reserved slots",
        "reserved spot",
        "reserved count",
        "reserved spots",
        "manual reserved",
    ),
    "roster": _column_aliases(
        "roster",
        "roster status",
        "roster column",
    ),
}


def _normalize_header(cell: Any) -> str:
    text = "" if cell is None else str(cell).strip().lower()
    return " ".join(text.split())


def _find_header_row(raw_rows: Sequence[Sequence[Any]]) -> List[str]:
    if len(raw_rows) >= 3:
        candidate = raw_rows[2]
        if any(str(cell or "").strip() for cell in candidate):
            return list(candidate)
    for row in raw_rows:
        if any(str(cell or "").strip() for cell in row):
            return list(row)
    return []


def _index_to_column_letter(index: int) -> str:
    """Return the spreadsheet column label for ``index`` (0-based)."""

    if index < 0:
        return ""
    label = ""
    value = index
    while True:
        value, remainder = divmod(value, 26)
        label = chr(ord("A") + remainder) + label
        if value == 0:
            break
        value -= 1
    return label


def _build_header_map(header_row: Sequence[Any], tab: str) -> Dict[str, int]:
    lookup: Dict[str, list[int]] = {}
    for idx, cell in enumerate(header_row):
        normalized = _normalize_header(cell)
        if not normalized:
            continue
        lookup.setdefault(normalized, []).append(idx)

    column_map: Dict[str, int] = {}
    missing: list[str] = []

    for key, aliases in HEADER_MAP.items():
        resolved: Optional[int] = None
        ignored: list[int] = []
        for alias in aliases:
            candidates = lookup.get(_normalize_header(alias))
            if not candidates:
                continue
            resolved = max(candidates)
            if len(candidates) > 1:
                ignored = [idx for idx in candidates if idx != resolved]
            break
        if resolved is None:
            missing.append(key)
            continue
        if ignored:
            log.debug(
                "recruitment sheet column resolved to later header",
                extra={
                    "tab": tab,
                    "column": key,
                    "selected": _index_to_column_letter(resolved),
                    "ignored": [
                        _index_to_column_letter(idx) for idx in sorted(ignored)
                    ],
                },
            )
        column_map[key] = resolved

    for key in missing:
        log.debug(
            "recruitment sheet column missing", extra={"tab": tab, "column": key}
        )

    return column_map


def _cell_value(row: Sequence[Any], index: Optional[int]) -> Any:
    if index is None or index < 0:
        return ""
    if index >= len(row):
        return ""
    return row[index]


def _to_int(value: Any) -> int:
    if value is None:
        return 0
    text = str(value).strip()
    if not text:
        return 0
    if text in {"-", "—"}:
        return 0
    match = re.search(r"-?\d+", text)
    if not match:
        return 0
    try:
        return int(match.group())
    except (TypeError, ValueError):  # pragma: no cover - defensive guard
        return 0


def _make_clan_record(row: Sequence[str], header_map: Dict[str, int]) -> RecruitmentClanRecord:
    roster_idx = header_map.get("roster", DEFAULT_ROSTER_INDEX)
    roster_cell = _cell_value(row, roster_idx)

    open_idx = header_map.get("open_spots", FALLBACK_OPEN_SPOTS_INDEX)
    inactives_idx = header_map.get("inactives", FALLBACK_INACTIVES_INDEX)
    reserved_idx = header_map.get("reserved", FALLBACK_RESERVED_INDEX)

    open_spots = _to_int(_cell_value(row, open_idx))
    inactives = _to_int(_cell_value(row, inactives_idx))
    reserved = _to_int(_cell_value(row, reserved_idx))
    return RecruitmentClanRecord(
        row=tuple(str(cell) if cell is not None else "" for cell in row),
        open_spots=open_spots,
        inactives=inactives,
        reserved=reserved,
        roster=str(roster_cell or "").strip(),
    )


def _process_clan_sheet(
    raw_rows: List[List[str]], now: float, tab: str
) -> List[List[str]]:
    global _CLAN_HEADER_ROW, _CLAN_HEADER_MAP, _CLAN_HEADER_TS
    global _CLAN_RECORDS, _CLAN_RECORDS_TS

    header_row = _find_header_row(raw_rows)
    header_map = _build_header_map(header_row, tab)
    sanitized = _sanitize_clan_rows(raw_rows, header_map.get("roster"))

    records = [_make_clan_record(row, header_map) for row in sanitized]

    _CLAN_HEADER_ROW = list(header_row)
    _CLAN_HEADER_MAP = dict(header_map)
    _CLAN_HEADER_TS = now
    _CLAN_RECORDS = records
    _CLAN_RECORDS_TS = now

    return sanitized


def _sheet_id() -> str:
    sheet_id = os.getenv("RECRUITMENT_SHEET_ID", "").strip()
    if not sheet_id:
        raise RuntimeError("RECRUITMENT_SHEET_ID not set")
    return sheet_id


def _ensure_service_account_credentials() -> None:
    creds = (
        os.getenv("GSPREAD_CREDENTIALS")
        or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        or ""
    ).strip()
    if not creds:
        raise RuntimeError("GSPREAD_CREDENTIALS not set")


def _config_tab() -> str:
    return os.getenv("RECRUITMENT_CONFIG_TAB", "Config")


def _load_config(force: bool = False) -> Dict[str, str]:
    global _CONFIG_CACHE, _CONFIG_CACHE_TS
    now = time.time()
    if not force and _CONFIG_CACHE and (now - _CONFIG_CACHE_TS) < _CONFIG_TTL:
        return _CONFIG_CACHE

    records = core.fetch_records(_sheet_id(), _config_tab())
    parsed: Dict[str, str] = {}
    for row in records:
        key_value: Optional[str] = None
        stored_value: Optional[str] = None
        for col, value in row.items():
            col_norm = (col or "").strip().lower()
            if col_norm == "key":
                key_value = str(value).strip().lower() if value is not None else ""
            elif col_norm in {"value", "val"}:
                stored_value = str(value).strip() if value is not None else ""
        if key_value:
            if stored_value:
                parsed[key_value] = stored_value
                continue
            for col, value in row.items():
                if (col or "").strip().lower() == "key":
                    continue
                if value is None:
                    continue
                candidate = str(value).strip()
                if candidate:
                    parsed[key_value] = candidate
                    break

    _CONFIG_CACHE = parsed
    _CONFIG_CACHE_TS = now
    return parsed


def _config_lookup(key: str, default: Optional[str] = None) -> Optional[str]:
    want = (key or "").strip().lower()
    if not want:
        return default
    config = _load_config()
    return config.get(want, default)


def _config_lookup_bool(key: str, default: bool = False) -> bool:
    value = _config_lookup(key)
    if value is None:
        return default
    text = str(value).strip().lower()
    if not text:
        return default
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def get_config_value(key: str, default: Optional[str] = None) -> Optional[str]:
    """Return a trimmed Config-tab value keyed by ``key`` (case-insensitive)."""

    value = _config_lookup(key, default)
    if value is None:
        return default
    text = str(value).strip()
    return text or default


def _clans_tab() -> str:
    return _config_lookup("clans_tab", os.getenv("WORKSHEET_NAME", "bot_info")) or "bot_info"


def _templates_tab() -> str:
    return _config_lookup("welcome_templates_tab", "WelcomeTemplates") or "WelcomeTemplates"


def get_recruitment_sheet_id() -> str:
    """Public helper for resolving the recruitment sheet identifier."""

    return _sheet_id()


def ensure_service_account_credentials() -> None:
    """Expose credential validation for callers performing write operations."""

    _ensure_service_account_credentials()


def get_clans_tab_name() -> str:
    """Return the configured clan roster tab name."""

    return _clans_tab()


def get_reservations_tab_name(default: str = "Reservations") -> str:
    value = _config_lookup("reservations_tab", default) or default
    text = str(value or "").strip()
    return text or default


def get_role_map_tab_name(default: str = "WhoWeAre") -> str:
    """Return the configured role map worksheet name."""

    value = _config_lookup("rolemap_tab", default) or default
    text = str(value or "").strip()
    return text or default


def get_reservations_config(force: bool = False) -> ReservationsConfig:
    """Return reservations feature wiring from the Config worksheet."""

    if force:
        _load_config(force=True)
    tab_name = get_reservations_tab_name()
    enabled = _config_lookup_bool("feature_reservations", False)
    return ReservationsConfig(enabled=enabled, tab_name=tab_name)


def get_reports_tab_name(default: str = "Statistics") -> str:
    """Return the configured Reports worksheet name (default "Statistics")."""

    value = _config_lookup("reports_tab", default) or default
    text = str(value or "").strip()
    return text or default


async def afetch_reports_tab(tab_name: str | None = None) -> List[List[str]]:
    """Fetch the recruitment reports worksheet as a raw matrix."""

    _ensure_service_account_credentials()
    sheet_id = _sheet_id()
    tab = tab_name or get_reports_tab_name()
    normalized = tab.strip() or get_reports_tab_name()
    return await afetch_values(sheet_id, normalized)


def _sanitize_clan_rows(
    raw_rows: List[List[str]], roster_index: int | None
) -> List[List[str]]:
    """Drop header rows and blank entries from ``raw_rows``."""

    def _norm(value: Any) -> str:
        return str(value).strip().upper() if value is not None else ""

    cleaned: List[List[str]] = []
    roster_idx = roster_index if roster_index is not None else DEFAULT_ROSTER_INDEX

    for row in raw_rows[3:]:  # Sheet headers occupy rows 1–3.
        if not row:
            continue
        name = _norm(row[1] if len(row) > 1 else "")
        tag = _norm(row[2] if len(row) > 2 else "")
        roster = str(_cell_value(row, roster_idx) or "").strip()
        if not name and not tag:
            continue
        if name in {"CLAN", "CLAN NAME", "CLANS"}:
            continue
        if tag in {"TAG", "CLAN TAG", "CLAN"}:
            continue
        if not roster:
            continue
        cleaned.append(row)
    return cleaned


def fetch_clans(force: bool = False) -> List[List[str]]:
    """Fetch the recruitment clan matrix from Sheets."""

    global _CLAN_ROWS, _CLAN_ROWS_TS, _CLAN_TAG_INDEX, _CLAN_TAG_INDEX_TS
    global _CLAN_RECORDS, _CLAN_RECORDS_TS
    now = time.time()
    if not force and _CLAN_ROWS and (now - _CLAN_ROWS_TS) < _CACHE_TTL:
        if _CLAN_TAG_INDEX is None:
            _CLAN_TAG_INDEX = _build_tag_index(_CLAN_ROWS)
            _CLAN_TAG_INDEX_TS = _CLAN_ROWS_TS
        return _CLAN_ROWS

    tab = _clans_tab()
    rows = core.fetch_values(_sheet_id(), tab)
    sanitized = _process_clan_sheet(rows, now, tab)
    _CLAN_ROWS = sanitized
    _CLAN_ROWS_TS = now
    _CLAN_TAG_INDEX = _build_tag_index(sanitized)
    _CLAN_TAG_INDEX_TS = now
    _CLAN_RECORDS_TS = now
    return sanitized


def get_clan_header_map(force: bool = False) -> Dict[str, int]:
    """Return the cached header-to-column mapping for the clan roster."""

    global _CLAN_HEADER_MAP, _CLAN_HEADER_TS
    now = time.time()
    if force or _CLAN_HEADER_MAP is None or (now - _CLAN_HEADER_TS) >= _CACHE_TTL:
        fetch_clans(force=force)
    return dict(_CLAN_HEADER_MAP or {})


def get_clan_records(force: bool = False) -> List[RecruitmentClanRecord]:
    """Return normalized clan roster records with numeric roster metadata."""

    global _CLAN_RECORDS, _CLAN_RECORDS_TS
    now = time.time()
    if force or _CLAN_RECORDS is None or (now - _CLAN_RECORDS_TS) >= _CACHE_TTL:
        fetch_clans(force=force)
    return list(_CLAN_RECORDS or [])


def fetch_templates(force: bool = False) -> List[Dict[str, Any]]:
    """Fetch welcome templates for recruitment flows."""

    global _TEMPLATE_ROWS, _TEMPLATE_ROWS_TS
    now = time.time()
    if not force and _TEMPLATE_ROWS and (now - _TEMPLATE_ROWS_TS) < _CACHE_TTL:
        return _TEMPLATE_ROWS

    rows = core.fetch_records(_sheet_id(), _templates_tab())
    _TEMPLATE_ROWS = rows
    _TEMPLATE_ROWS_TS = now
    return rows


def fetch_clan_rows(force: bool = False) -> List[List[str]]:
    """Backward-compatible alias for legacy imports."""

    return fetch_clans(force=force)


def fetch_welcome_templates(tab: str | None = None) -> List[Dict[str, Any]]:
    """Backward-compatible alias retaining the previous signature."""

    if tab:
        return core.fetch_records(_sheet_id(), tab)
    return fetch_templates()


def get_cached_welcome_templates() -> List[Dict[str, Any]]:
    """Return cached WelcomeTemplates rows when available, falling back to a live fetch."""

    bucket = cache.get_bucket("templates")
    if bucket and bucket.value is not None:
        return cast(List[Dict[str, Any]], bucket.value)
    return fetch_welcome_templates()


# -----------------------------
# Phase 3 cache registrations
# -----------------------------
_TTL_CLANS_SEC = 3 * 60 * 60
_TTL_TEMPLATES_SEC = 7 * 24 * 60 * 60


async def _load_clans_async() -> List[List[str]]:
    _ensure_service_account_credentials()
    sheet_id = _sheet_id()
    tab = _clans_tab()
    rows = await afetch_values(sheet_id, tab)
    now = time.time()
    sanitized = _process_clan_sheet(rows, now, tab)

    global _CLAN_ROWS, _CLAN_ROWS_TS, _CLAN_TAG_INDEX, _CLAN_TAG_INDEX_TS
    _CLAN_ROWS = sanitized
    _CLAN_ROWS_TS = now
    _CLAN_TAG_INDEX = _build_tag_index(sanitized)
    _CLAN_TAG_INDEX_TS = now

    return sanitized


async def _load_templates_async() -> List[Dict[str, Any]]:
    _ensure_service_account_credentials()
    sheet_id = _sheet_id()
    tab = _templates_tab()
    return await afetch_records(sheet_id, tab)




def register_cache_buckets() -> None:
    """Register recruitment cache buckets if they are not already present."""

    if cache.get_bucket("clans") is None:
        cache.register("clans", _TTL_CLANS_SEC, _load_clans_async)
    if cache.get_bucket("templates") is None:
        cache.register("templates", _TTL_TEMPLATES_SEC, _load_templates_async)


def _normalize_tag(tag: str | None) -> str:
    text = "" if tag is None else str(tag).strip().upper()
    return "".join(ch for ch in text if ch.isalnum())


def _build_tag_index(rows: List[List[str]]) -> Dict[str, List[str]]:
    index: Dict[str, List[str]] = {}
    for row in rows:
        if len(row) < 3:
            continue
        normalized = _normalize_tag(row[2])
        if not normalized:
            continue
        index[normalized] = row
    return index


def fetch_clan_tags_index(force: bool = False) -> Dict[str, List[str]]:
    """Return a cached mapping of ``TAG`` → clan row."""

    global _CLAN_TAG_INDEX, _CLAN_TAG_INDEX_TS
    now = time.time()
    if force or _CLAN_TAG_INDEX is None or (now - _CLAN_TAG_INDEX_TS) >= _CACHE_TTL:
        rows = fetch_clans(force=force)
        _CLAN_TAG_INDEX = _build_tag_index(rows)
        _CLAN_TAG_INDEX_TS = time.time()
    return _CLAN_TAG_INDEX or {}


def get_clan_by_tag(tag: str, *, force: bool = False) -> List[str] | None:
    """Lookup a clan row by tag using the cached index when available."""

    normalized = _normalize_tag(tag)
    if not normalized:
        return None

    index = fetch_clan_tags_index(force=force)
    if index:
        return index.get(normalized)

    # Index unavailable — fall back to a lightweight scan of cached rows.
    rows = fetch_clans(force=False)
    for row in rows:
        if len(row) < 3:
            continue
        if _normalize_tag(row[2]) == normalized:
            return row
    return None


def find_clan_row(clan_tag: str, *, force: bool = False) -> tuple[int, List[str]] | None:
    """Return the sheet row number and values for ``clan_tag``."""

    normalized = _normalize_tag(clan_tag)
    if not normalized:
        return None

    rows = fetch_clans(force=force)
    for idx, row in enumerate(rows):
        if len(row) < 3:
            continue
        if _normalize_tag(row[2]) == normalized:
            sheet_row = idx + 4  # Account for the three summary/header rows.
            return sheet_row, list(row)
    return None


def update_cached_clan_row(sheet_row: int, row_values: Sequence[str]) -> None:
    """Update the in-memory caches for the clan located at ``sheet_row``."""

    normalized_row = [str(cell) if cell is not None else "" for cell in row_values]
    index = sheet_row - 4
    if index < 0:
        return

    global _CLAN_ROWS, _CLAN_ROWS_TS, _CLAN_TAG_INDEX, _CLAN_TAG_INDEX_TS
    global _CLAN_RECORDS, _CLAN_RECORDS_TS

    now = time.time()

    if _CLAN_ROWS is not None and 0 <= index < len(_CLAN_ROWS):
        _CLAN_ROWS[index] = list(normalized_row)
        _CLAN_ROWS_TS = now

    if _CLAN_TAG_INDEX is not None:
        tag = normalized_row[2] if len(normalized_row) > 2 else ""
        normalized_tag = _normalize_tag(tag)
        if normalized_tag:
            _CLAN_TAG_INDEX[normalized_tag] = list(normalized_row)
            _CLAN_TAG_INDEX_TS = now

    if _CLAN_RECORDS is not None and 0 <= index < len(_CLAN_RECORDS):
        header_map = get_clan_header_map()
        _CLAN_RECORDS[index] = _make_clan_record(normalized_row, header_map)
        _CLAN_RECORDS_TS = now

    bucket = cache.get_bucket("clans")
    if bucket and isinstance(bucket.value, list) and 0 <= index < len(bucket.value):
        bucket.value[index] = list(normalized_row)
