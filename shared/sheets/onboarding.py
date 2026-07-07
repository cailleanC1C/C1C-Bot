"""Onboarding sheet helpers (Welcome Crew)."""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from shared.sheets import core
from shared.sheets.async_core import afetch_values
from shared.sheets.cache_service import cache

_CACHE_TTL = int(os.getenv("SHEETS_CACHE_TTL_SEC", "900"))
_CONFIG_TTL = int(os.getenv("SHEETS_CONFIG_CACHE_TTL_SEC", str(_CACHE_TTL)))
_CLAN_TAG_TTL = int(os.getenv("CLAN_TAGS_CACHE_TTL_SEC", str(_CACHE_TTL)))

_CONFIG_CACHE: Dict[str, str] | None = None
_CONFIG_CACHE_TS: float = 0.0

_CLAN_TAGS: List[str] | None = None
_CLAN_TAG_TS: float = 0.0
_WELCOME_REPAIR_LAST_RUN: float = 0.0
_WELCOME_REPAIR_ALERT_LAST_TS: float = 0.0
_WELCOME_REPAIR_ALERT_PENDING: str | None = None


log = logging.getLogger(__name__)

WELCOME_HEADERS: List[str] = [
    "ticket_number",
    "username",
    "clantag",
    "date_closed",
    "thread_name",
    "user_id",
    "thread_id",
    "panel_message_id",
    "status",
    "review_reason",
    "created_at",
    "updated_at",
]
PROMO_HEADERS: List[str] = [
    "ticket number",
    "username",
    "clantag",
    "source_clan_tag",
    "date closed",
    "type",
    "thread created",
    "year",
    "month",
    "join_month",
    "clan name",
    "progression",
    "thread_name",
    "user_id",
    "thread_id",
    "panel_message_id",
    "status",
    "review_reason",
    "created_at",
    "updated_at",
]
PROMO_SOURCE_CLAN_TAG_HEADER_CONFIG_KEY = "PROMO_SOURCE_CLAN_TAG_HEADER"
_PROMO_SOURCE_CLAN_TAG_HEADER_DEFAULT_SLOT = 3


_FINALIZATION_CONFIG_KEYS: Dict[str, Dict[str, str]] = {
    "welcome": {
        "finalization_status": "WELCOME_FINALIZATION_STATUS_HEADER",
        "reservation_status": "WELCOME_RESERVATION_STATUS_HEADER",
        "clan_update_status": "WELCOME_CLAN_UPDATE_STATUS_HEADER",
        "finalization_note": "WELCOME_FINALIZATION_NOTE_HEADER",
    },
    "promo": {
        "finalization_status": "PROMO_FINALIZATION_STATUS_HEADER",
        "reservation_status": "PROMO_RESERVATION_STATUS_HEADER",
        "clan_update_status": "PROMO_CLAN_UPDATE_STATUS_HEADER",
        "finalization_note": "PROMO_FINALIZATION_NOTE_HEADER",
    },
}


def _required_config_header(key: str) -> str:
    header = _config_lookup(key)
    cleaned = str(header or "").strip()
    if not cleaned:
        raise RuntimeError(f"Onboarding Config missing {key}")
    return cleaned


def get_finalization_headers(flow: str, *, force: bool = False) -> Dict[str, str]:
    """Return Config-resolved finalization state headers for Welcome/Promo."""

    normalized = (flow or "").strip().lower()
    if normalized not in _FINALIZATION_CONFIG_KEYS:
        raise ValueError(f"unknown onboarding finalization flow: {flow!r}")
    if force:
        _load_config(force=True)
    return {field: _required_config_header(key) for field, key in _FINALIZATION_CONFIG_KEYS[normalized].items()}


def require_finalization_headers(flow: str, headers: Sequence[str]) -> Dict[str, str]:
    """Validate that the sheet contains all Config-resolved finalization columns."""

    resolved = get_finalization_headers(flow)
    normalized_headers = {_normalize_header_name(header) for header in headers}
    missing = [
        f"{_FINALIZATION_CONFIG_KEYS[(flow or '').strip().lower()][field]}={header!r}"
        for field, header in resolved.items()
        if _normalize_header_name(header) not in normalized_headers
    ]
    if missing:
        raise RuntimeError(
            f"{flow.title()} sheet missing configured finalization header(s): " + ", ".join(missing)
        )
    return resolved


def get_welcome_headers(*, force: bool = False) -> List[str]:
    """Return Welcome headers with finalization columns resolved from Config."""

    final_headers = get_finalization_headers("welcome", force=force)
    return [*WELCOME_HEADERS, *final_headers.values()]


def get_promo_source_clan_tag_header(*, force: bool = False) -> str:
    """Return the Config-driven Promo source clan header.

    This is intentionally required. Promo move close math must not silently fall
    back to destination-only behavior if the source column mapping is absent.
    """

    if force:
        _load_config(force=True)
    header = _config_lookup(PROMO_SOURCE_CLAN_TAG_HEADER_CONFIG_KEY)
    cleaned = str(header or "").strip()
    if not cleaned:
        raise RuntimeError(
            "Onboarding Config missing PROMO_SOURCE_CLAN_TAG_HEADER for Promo source clan header"
        )
    return cleaned


def get_promo_headers(*, force: bool = False) -> List[str]:
    """Return Promo headers with Config-resolved source/finalization columns."""

    headers = list(PROMO_HEADERS)
    headers[_PROMO_SOURCE_CLAN_TAG_HEADER_DEFAULT_SLOT] = (
        get_promo_source_clan_tag_header(force=force)
    )
    final_headers = get_finalization_headers("promo", force=force)
    return [*headers, *final_headers.values()]


def require_promo_source_clan_header(headers: Sequence[str]) -> str:
    """Validate that ``headers`` include the Config-resolved source header."""

    source_header = get_promo_source_clan_tag_header()
    normalized_source = _normalize_header_name(source_header)
    normalized_headers = {_normalize_header_name(header) for header in headers}
    if normalized_source not in normalized_headers:
        raise RuntimeError(
            "Promo sheet/header mapping missing configured source clan header "
            f"{PROMO_SOURCE_CLAN_TAG_HEADER_CONFIG_KEY}={source_header!r}"
        )
    require_finalization_headers("promo", headers)
    return source_header

WELCOME_TICKET_INDEX = 0
WELCOME_CLAN_TAG_INDEX = 2
WELCOME_DATE_CLOSED_INDEX = 3
_DISCORD_ID_RE = re.compile(r"^\d{15,22}$")
_WELCOME_TICKET_RE = re.compile(r"^[A-Z]+\d{2,}$")
_CURRENT_TICKET_CUTOFF = datetime(2026, 4, 1, tzinfo=timezone.utc)


def _sheet_id() -> str:
    """Resolve the onboarding sheet id – no legacy fallbacks."""

    sheet_id = os.getenv("ONBOARDING_SHEET_ID", "").strip()
    if not sheet_id:
        raise RuntimeError("ONBOARDING_SHEET_ID not set")
    # Log tail only, never the full id
    tail = sheet_id[-6:] if len(sheet_id) >= 6 else sheet_id
    redacted = f"…{tail}" if len(sheet_id) > len(tail) else tail
    log.info("📄 Onboarding sheet resolved • id_tail=%s", redacted)
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
    return os.getenv("ONBOARDING_CONFIG_TAB", "Config")


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



async def _aload_config(force: bool = False) -> Dict[str, str]:
    global _CONFIG_CACHE, _CONFIG_CACHE_TS
    now = time.time()
    if not force and _CONFIG_CACHE and (now - _CONFIG_CACHE_TS) < _CONFIG_TTL:
        return _CONFIG_CACHE

    records = await afetch_records(_sheet_id(), _config_tab())
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
        if key_value and stored_value:
            parsed[key_value] = stored_value

    _CONFIG_CACHE = parsed
    _CONFIG_CACHE_TS = now
    return parsed


async def _aconfig_lookup(key: str, default: Optional[str] = None) -> Optional[str]:
    want = (key or "").strip().lower()
    if not want:
        return default
    config = await _aload_config()
    return config.get(want, default)

def _config_lookup(key: str, default: Optional[str] = None) -> Optional[str]:
    want = (key or "").strip().lower()
    if not want:
        return default
    config = _load_config()
    return config.get(want, default)


def _resolve_onboarding_sheet_id() -> str:
    """Return the configured onboarding sheet identifier."""

    return _sheet_id()


def _read_onboarding_config(sheet_id: Optional[str] = None) -> Dict[str, str]:
    """Return the onboarding config mapping using upper-case keys.

    ``_load_config`` normalises keys to lower-case for internal use.  Some
    callers expect the sheet's original upper-case key names, so we build a new
    dictionary with upper-case keys while reusing the cached configuration
    values.
    """

    _ = sheet_id  # preserved for API compatibility with older helpers
    config = _load_config()
    return {key.upper(): value for key, value in config.items()}


def _resolve_onboarding_and_welcome_tab() -> Tuple[str, str]:
    """Return the onboarding sheet id and configured welcome tab name."""

    sheet_id = _resolve_onboarding_sheet_id()
    cfg = _read_onboarding_config(sheet_id)
    tab = cfg.get("WELCOME_TICKETS_TAB")
    if not tab:
        raise RuntimeError("Onboarding Config missing WELCOME_TICKETS_TAB")
    return sheet_id, str(tab)


def _resolve_onboarding_and_promo_tab() -> Tuple[str, str]:
    """Return the onboarding sheet id and configured promo tab name."""

    sheet_id = _resolve_onboarding_sheet_id()
    cfg = _read_onboarding_config(sheet_id)
    tab = cfg.get("PROMO_TICKETS_TAB")
    if not tab:
        raise RuntimeError("Onboarding Config missing PROMO_TICKETS_TAB")
    return sheet_id, str(tab)


def _resolve_onboarding_and_sessions_tab() -> Tuple[str, str]:
    """Return the onboarding sheet id and configured sessions tab name."""

    sheet_id = _resolve_onboarding_sheet_id()
    cfg = _read_onboarding_config(sheet_id)
    tab = cfg.get("ONBOARDING_SESSIONS_TAB")
    if not tab:
        raise RuntimeError("Onboarding Config missing ONBOARDING_SESSIONS_TAB")
    return sheet_id, str(tab)


def _welcome_tab() -> str:
    return (
        _config_lookup("welcome_tickets_tab", "WelcomeTickets")
        or "WelcomeTickets"
    )


def _promo_tab() -> str:
    return (
        _config_lookup("promo_tickets_tab", "PromoTickets")
        or "PromoTickets"
    )


def _clanlist_tab() -> str:
    return _config_lookup("clanlist_tab", "ClanList") or "ClanList"


async def _aclanlist_tab() -> str:
    return await _aconfig_lookup("clanlist_tab", "ClanList") or "ClanList"


def _worksheet(tab: str):
    return core.get_worksheet(_sheet_id(), tab)


def _resolve_onboarding_and_clanlist_tab() -> Tuple[str, str]:
    """Return the onboarding sheet id and configured clan list tab name."""

    sheet_id = _resolve_onboarding_sheet_id()
    cfg = _read_onboarding_config(sheet_id)
    tab = cfg.get("CLANLIST_TAB")
    if not tab:
        raise RuntimeError("Onboarding Config missing CLANLIST_TAB")
    return sheet_id, str(tab)


def _column_index(headers: Sequence[str], name: str, default: int = 0) -> int:
    target = (name or "").strip().lower()
    for idx, header in enumerate(headers):
        if (header or "").strip().lower() == target:
            return idx
    return default


def _col_to_a1(col_index: int) -> str:
    if col_index < 0:
        raise ValueError("column index must be >= 0")
    letters = ""
    value = col_index
    while True:
        value, remainder = divmod(value, 26)
        letters = chr(ord("A") + remainder) + letters
        if value == 0:
            break
        value -= 1
    return letters


def _ensure_headers(ws, headers: Sequence[str]) -> List[str]:
    desired = [h.strip() for h in headers]
    try:
        existing = core.call_with_backoff(ws.row_values, 1)
    except Exception:
        existing = []
    existing_norm = [h.strip() for h in existing]
    if existing_norm != desired:
        core.call_with_backoff(ws.update, "A1", [list(headers)])
        return list(headers)
    return list(existing) if existing else list(headers)


def _read_promo_live_header_row(ws) -> List[str]:
    """Read the operator-owned Promo header row without mutating it."""

    try:
        existing = core.call_with_backoff(ws.row_values, 1)
    except Exception:
        existing = []
    header = [str(value or "").strip() for value in existing]
    if not any(header):
        raise RuntimeError("Promo sheet header row missing")
    return header


def _ensure_promo_headers(ws) -> List[str]:
    """Return the live Promo header row without mutating it.

    Promo column order is operator-owned. Existing headers are the source of
    truth; callers must map writes by matching header names only.
    """

    header = _read_promo_live_header_row(ws)
    source_header = get_promo_source_clan_tag_header()
    normalized_existing = {_normalize_header_name(col) for col in header}
    if _normalize_header_name(source_header) not in normalized_existing:
        raise RuntimeError(
            "Promo sheet missing configured source clan header "
            f"{PROMO_SOURCE_CLAN_TAG_HEADER_CONFIG_KEY}={source_header!r}"
        )
    return header


def get_live_promo_headers() -> List[str]:
    """Return the operator-owned live Promo header row."""

    sheet_id, tab = _resolve_onboarding_and_promo_tab()
    ws = core.get_worksheet(sheet_id, tab)
    return _ensure_promo_headers(ws)


def _fmt_ticket(ticket: str | None) -> str:
    text = (ticket or "").strip().lstrip("#")
    return text.upper()


def _normalize_header_name(name: str) -> str:
    return "".join(ch for ch in str(name or "").lower() if ch.isalnum())


def _is_isoish(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _to_iso_date(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text[:10] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text[:10]) else text
    return parsed.date().isoformat()


def _parse_dt(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_discord_id(value: str) -> bool:
    return bool(_DISCORD_ID_RE.match(str(value or "").strip()))


def _looks_like_ticket(value: str) -> bool:
    return bool(_WELCOME_TICKET_RE.match(_fmt_ticket(value)))


def repair_welcome_rows(ws) -> dict[str, Any]:
    header = _ensure_headers(ws, get_welcome_headers())
    norm = [_normalize_header_name(col) for col in header]
    idx = {name: pos for pos, name in enumerate(norm)}
    required = {"ticketnumber", "username"}
    missing_required = sorted(required - set(idx))
    if missing_required:
        log.error(
            "welcome ticket metadata check skipped: missing required header",
            extra={"missing_headers": missing_required},
        )
        return {"repaired": 0, "flagged": 0, "scanned": 0, "config_error": "missing_required_header"}

    values = core.call_with_backoff(ws.get_all_values)
    repaired = 0
    flagged = 0
    scanned = 0
    legacy_rows = 0
    welcome_rows = 0
    reservation_rows = 0
    malformed_rows = 0
    review_detail_rows = 0
    app_logged_review_details = 0
    ticket_identity: dict[str, dict[str, set[str]]] = {}
    for row in values[1:]:
        row_values = list(row) + [""] * (len(header) - len(row))
        ticket = _fmt_ticket(row_values[idx["ticketnumber"]].strip())
        if not ticket:
            continue
        identity = ticket_identity.setdefault(ticket, {"user_ids": set(), "thread_ids": set()})
        raw_user = row_values[idx.get("userid", -1)].strip() if idx.get("userid") is not None else ""
        raw_thread = row_values[idx.get("threadid", -1)].strip() if idx.get("threadid") is not None else ""
        if _is_discord_id(raw_user):
            identity["user_ids"].add(raw_user)
        if _is_discord_id(raw_thread):
            identity["thread_ids"].add(raw_thread)

    for row_number, row in enumerate(values[1:], start=2):
        scanned += 1
        row_values = list(row) + [""] * (len(header) - len(row))
        ticket = row_values[idx["ticketnumber"]].strip()
        username = row_values[idx["username"]].strip()
        user_id_idx = idx.get("userid")
        thread_id_idx = idx.get("threadid")
        status_idx = idx.get("status")
        created_idx = idx.get("createdat")
        updated_idx = idx.get("updatedat")

        user_id = row_values[user_id_idx].strip() if user_id_idx is not None else ""
        thread_id = row_values[thread_id_idx].strip() if thread_id_idx is not None else ""

        changed = False
        if user_id_idx is not None and not _is_discord_id(user_id):
            candidate = thread_id if _is_discord_id(thread_id) else ""
            if candidate:
                row_values[user_id_idx] = candidate
                if thread_id_idx is not None:
                    row_values[thread_id_idx] = ""
                changed = True
            elif _is_isoish(user_id):
                row_values[user_id_idx] = ""
                changed = True

        if thread_id_idx is not None and not _is_discord_id(row_values[thread_id_idx]):
            if _is_isoish(row_values[thread_id_idx]) or "-" in row_values[thread_id_idx]:
                row_values[thread_id_idx] = ""
                changed = True

        ticket_like = _looks_like_ticket(ticket)
        has_username = bool(username)
        has_user_or_thread_id = _is_discord_id(user_id) or _is_discord_id(thread_id)
        status_value = str(row_values[status_idx] or "").strip().lower() if status_idx is not None else ""
        is_reservation = status_value in {"reserved", "reservation", "queued", "pending"}
        has_welcome_shape = ticket_like or has_username or has_user_or_thread_id
        looks_like_current_welcome = ticket_like and has_username

        if is_reservation:
            reservation_rows += 1
        elif looks_like_current_welcome:
            welcome_rows += 1
        elif not has_welcome_shape and status_value in {"", "completed", "closed", "archived", "done"}:
            legacy_rows += 1
        else:
            # Legacy rows often carry partial data (username-only or stale IDs)
            # that should not be counted as malformed unless they appear to be
            # welcome-shaped records with inconsistent required fields.
            if has_welcome_shape and not looks_like_current_welcome and not is_reservation:
                malformed_rows += 1
            else:
                legacy_rows += 1

        created_marker = _parse_dt(row_values[created_idx]) if created_idx is not None and created_idx < len(row_values) else None
        is_current_ticket = created_marker is not None and created_marker >= _CURRENT_TICKET_CUTOFF
        ticket_key = _fmt_ticket(ticket)
        identity = ticket_identity.get(ticket_key, {"user_ids": set(), "thread_ids": set()})
        review_idx = idx.get("reviewreason")
        if is_current_ticket and looks_like_current_welcome:
            if not _is_discord_id(user_id) and len(identity["user_ids"]) == 1 and user_id_idx is not None:
                row_values[user_id_idx] = next(iter(identity["user_ids"]))
                changed = True
            if not _is_discord_id(thread_id) and len(identity["thread_ids"]) == 1 and thread_id_idx is not None:
                row_values[thread_id_idx] = next(iter(identity["thread_ids"]))
                changed = True
            user_id = row_values[user_id_idx].strip() if user_id_idx is not None else ""
            thread_id = row_values[thread_id_idx].strip() if thread_id_idx is not None else ""
            has_user_or_thread_id = _is_discord_id(user_id) or _is_discord_id(thread_id)
            if review_idx is not None and has_user_or_thread_id and str(row_values[review_idx] or "").strip():
                row_values[review_idx] = ""
                changed = True

        invalid = is_current_ticket and looks_like_current_welcome and not has_user_or_thread_id
        if invalid and status_idx is not None:
            status_value = str(row_values[status_idx] or "").strip().lower()
            if len(identity["thread_ids"]) > 1:
                review_reason = "conflicting thread IDs"
            elif len(identity["user_ids"]) > 1:
                review_reason = "conflicting user IDs"
            else:
                review_reason = "no matching ticket source found"

            if status_value not in {"invalid", "needs_review", "closed"}:
                row_values[status_idx] = "needs_review"
                changed = True
            if review_idx is not None and not str(row_values[review_idx] or "").strip():
                row_values[review_idx] = review_reason
                changed = True
            if review_idx is not None and str(row_values[review_idx] or "").strip():
                review_detail_rows += 1

            log.warning(
                "welcome ticket metadata check needs attention",
                extra={
                    "ticket": ticket_key,
                    "row_number": row_number,
                    "username": username,
                    "thread_id": thread_id or None,
                    "user_id": user_id or None,
                    "review_reason": str(row_values[review_idx] or "").strip() if review_idx is not None else review_reason,
                    "review_reason_persisted": bool(
                        review_idx is not None and str(row_values[review_idx] or "").strip()
                    ),
                },
            )
            app_logged_review_details += 1
            flagged += 1
        elif status_idx is not None and str(row_values[status_idx] or "").strip().lower() == "needs_review":
            # keep explicit invalid marker untouched
            pass

        now_iso = datetime.now(timezone.utc).isoformat()
        if changed and updated_idx is not None:
            row_values[updated_idx] = now_iso
        if created_idx is not None and not str(row_values[created_idx] or "").strip():
            row_values[created_idx] = now_iso
            changed = True

        if changed:
            end_col = _col_to_a1(len(header) - 1)
            core.call_with_backoff(ws.update, f"A{row_number}:{end_col}{row_number}", [row_values[: len(header)]])
            repaired += 1

    return {"repaired": repaired, "flagged": flagged, "scanned": scanned, "legacy_rows": legacy_rows, "welcome_rows": welcome_rows, "reservation_rows": reservation_rows, "malformed_rows": malformed_rows, "review_detail_rows": review_detail_rows, "app_logged_review_details": app_logged_review_details}


def _format_welcome_repair_alert(summary: dict[str, Any]) -> str | None:
    if summary.get("config_error"):
        return "⚠️ Welcome ticket metadata check skipped: required sheet configuration is missing."

    flagged = int(summary.get("flagged", 0) or 0)
    repaired = int(summary.get("repaired", 0) or 0)
    welcome_rows = int(summary.get("welcome_rows", 0) or 0)
    review_detail_rows = int(summary.get("review_detail_rows", 0) or 0)
    app_logged_review_details = int(summary.get("app_logged_review_details", 0) or 0)

    if flagged <= 0 and repaired <= 0:
        return f"✅ Welcome ticket metadata check: no repair needed. {welcome_rows} welcome tickets checked."
    if flagged <= 0:
        return f"✅ Welcome ticket metadata check: {repaired} repaired, none need review."
    if review_detail_rows >= flagged:
        return (
            "⚠️ Welcome ticket metadata check: "
            f"{flagged} tickets need review, {repaired} repaired. "
            "See review_reason in the configured onboarding sheet."
        )
    if app_logged_review_details >= flagged:
        return (
            "⚠️ Welcome ticket metadata check: "
            f"{flagged} ticket records could not be auto-repaired. Details in app logs."
        )
    log.error(
        "welcome ticket metadata check found records without review visibility",
        extra={"flagged": flagged, "repaired": repaired, "welcome_rows": welcome_rows},
    )
    return None


def _queue_welcome_repair_alert(summary: dict[str, Any]) -> None:
    global _WELCOME_REPAIR_ALERT_LAST_TS, _WELCOME_REPAIR_ALERT_PENDING
    message = _format_welcome_repair_alert(summary)
    if not message:
        return
    flagged = int(summary.get("flagged", 0) or 0)
    if flagged <= 0 and int(summary.get("repaired", 0) or 0) <= 0:
        return
    now_ts = time.time()
    if (now_ts - _WELCOME_REPAIR_ALERT_LAST_TS) < 3600:
        return
    _WELCOME_REPAIR_ALERT_LAST_TS = now_ts
    _WELCOME_REPAIR_ALERT_PENDING = message


def consume_welcome_repair_alert() -> str | None:
    global _WELCOME_REPAIR_ALERT_PENDING
    message = _WELCOME_REPAIR_ALERT_PENDING
    _WELCOME_REPAIR_ALERT_PENDING = None
    return message


def _match_row(
    headers: Sequence[str],
    row: Sequence[str],
    key_columns: Sequence[Tuple[str, Callable[[str | None], str]]],
    candidates: Sequence[str],
) -> bool:
    for (name, formatter), candidate in zip(key_columns, candidates):
        idx = _column_index(headers, name)
        current = row[idx] if idx < len(row) else ""
        if formatter(current) != formatter(candidate):
            return False
    return True


def _upsert(
    ws,
    key_columns: Sequence[Tuple[str, Callable[[str | None], str]]],
    row_values: Sequence[str],
    headers: Sequence[str],
    *,
    search_values: Optional[Sequence[str]] = None,
) -> str:
    header = _ensure_headers(ws, headers)
    total_cols = len(header)
    if len(row_values) < total_cols:
        row_values = list(row_values) + ["" for _ in range(total_cols - len(row_values))]
    values = core.call_with_backoff(ws.get_all_values)
    if search_values is None:
        search_values = []
        for name, _ in key_columns:
            idx = _column_index(header, name)
            search_values.append(row_values[idx] if idx < len(row_values) else "")

    for row_idx, row in enumerate(values[1:], start=2):
        if _match_row(header, row, key_columns, search_values):
            end_col = _col_to_a1(total_cols - 1)
            rng = f"A{row_idx}:{end_col}{row_idx}"
            core.call_with_backoff(ws.update, rng, [list(row_values)])
            return "updated"

    core.call_with_backoff(ws.append_row, list(row_values), value_input_option="RAW")
    return "inserted"


def upsert_welcome(row_values: Sequence[str], headers: Sequence[str]) -> str:
    """Insert or update a welcome ticket row based on its ticket number."""

    ws = _worksheet(_welcome_tab())
    keys = [("ticket number", _fmt_ticket)]
    return _upsert(ws, keys, row_values, headers)


def _build_ticket_row(header: Sequence[str], value_map: dict[str, str]) -> list[str]:
    normalized_header = [_normalize_header_name(col) for col in header]
    return [value_map.get(name, "") for name in normalized_header]


def _ticket_key_columns(
    header: Sequence[str],
    ticket_value: str,
    thread_id: int | None,
) -> tuple[list[tuple[str, Callable[[str | None], str]]], list[str]]:
    normalized_header = [_normalize_header_name(col) for col in header]
    key_columns: list[tuple[str, Callable[[str | None], str]]] = []
    search_values: list[str] = []

    for column, normalized in zip(header, normalized_header):
        if normalized in {"ticket", "ticketnumber", "ticketid"}:
            key_columns.append((column, _fmt_ticket))
            search_values.append(ticket_value)
            break

    if thread_id is not None and not _looks_like_ticket(ticket_value):
        for column, normalized in zip(header, normalized_header):
            if normalized in {"thread", "threadid"}:
                key_columns.append((column, lambda value: str(value or "").strip()))
                search_values.append(str(thread_id))
                break

    if not key_columns:
        raise RuntimeError("Onboarding tickets tab missing ticket identifier column")

    return key_columns, search_values




def _promo_header_index(header: Sequence[str]) -> dict[str, int]:
    return {_normalize_header_name(name): idx for idx, name in enumerate(header)}


def _promo_find_row_in_values(
    header: Sequence[str],
    values: Sequence[Sequence[str]],
    *,
    ticket: str | None = None,
    thread_id: int | str | None = None,
) -> tuple[int | None, list[str] | None]:
    idx = _promo_header_index(header)
    target_thread = str(thread_id or "").strip()
    thread_col = idx.get("threadid")
    if target_thread and thread_col is not None:
        for row_idx, row in enumerate(values[1:], start=2):
            current = row[thread_col] if thread_col < len(row) else ""
            if str(current or "").strip() == target_thread:
                return row_idx, list(row)

    target_ticket = _fmt_ticket(ticket) if ticket else ""
    ticket_col = idx.get("ticketnumber")
    if target_ticket and ticket_col is not None:
        for row_idx, row in enumerate(values[1:], start=2):
            current = row[ticket_col] if ticket_col < len(row) else ""
            if _fmt_ticket(current) == target_ticket:
                return row_idx, list(row)
    return None, None


def _promo_update_data_row(ws, header: Sequence[str], row_number: int, row_values: Sequence[str]) -> None:
    end_col = _col_to_a1(len(header) - 1)
    core.call_with_backoff(ws.update, f"A{row_number}:{end_col}{row_number}", [list(row_values)[: len(header)]])


def _promo_safe_upsert_mapped(
    ws,
    header: Sequence[str],
    incoming: dict[str, str],
    *,
    ticket: str | None = None,
    thread_id: int | str | None = None,
    preserve_existing: bool = True,
) -> str:
    """Insert/update a Promo data row without touching row 1."""

    idx = _promo_header_index(header)
    if "ticketnumber" not in idx:
        raise RuntimeError("Promo sheet missing required header: ticket number")
    values = core.call_with_backoff(ws.get_all_values)
    row_number, row = _promo_find_row_in_values(header, values, ticket=ticket, thread_id=thread_id)
    if row is None:
        row = ["" for _ in header]
    elif len(row) < len(header):
        row.extend("" for _ in range(len(header) - len(row)))

    changed = False
    for normalized, value in incoming.items():
        col = idx.get(_normalize_header_name(normalized))
        if col is None:
            continue
        text = str(value or "").strip()
        if not text:
            continue
        if preserve_existing and str(row[col] or "").strip():
            continue
        if row[col] != text:
            row[col] = text
            changed = True

    if row_number is None:
        core.call_with_backoff(ws.append_row, row[: len(header)], value_input_option="RAW")
        return "inserted"
    if not changed:
        return "unchanged"
    _promo_update_data_row(ws, header, row_number, row)
    return "updated"


def _normalize_ticket_timestamps(
    created_at: datetime | None, updated_at: datetime | None
) -> tuple[datetime, datetime]:
    created = created_at or datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    normalized_updated = updated_at or created
    if normalized_updated.tzinfo is None:
        normalized_updated = normalized_updated.replace(tzinfo=timezone.utc)
    return created, normalized_updated




def run_welcome_ticket_repair_pass(*, min_interval_sec: float = 3600) -> dict[str, Any]:
    """Run a repair/flag sweep on the Welcome ticket tab using config-resolved sheet/tab."""

    global _WELCOME_REPAIR_LAST_RUN
    now_ts = time.time()
    if min_interval_sec > 0 and (now_ts - _WELCOME_REPAIR_LAST_RUN) <= min_interval_sec:
        return {"repaired": 0, "flagged": 0, "scanned": 0}

    sheet_id, tab = _resolve_onboarding_and_welcome_tab()
    ws = core.get_worksheet(sheet_id, tab)
    scanned_rows = -1
    try:
        raw_values = core.call_with_backoff(ws.get_all_values)
        scanned_rows = max(0, len(raw_values) - 1)
    except Exception:
        scanned_rows = -1
    try:
        summary = repair_welcome_rows(ws)
    except Exception:
        log.exception(
            "welcome ticket repair failed with exception",
            extra={"tab_name": tab, "sheet_id": sheet_id, "row_count": scanned_rows},
        )
        raise
    _WELCOME_REPAIR_LAST_RUN = now_ts
    _queue_welcome_repair_alert(summary)
    return summary
def append_welcome_ticket_row(
    ticket: str,
    username: str,
    clan_tag: str,
    date_closed: str,
    *,
    thread_name: str | None = None,
    user_id: int | str | None = None,
    thread_id: int | None = None,
    panel_message_id: int | None = None,
    status: str = "open",
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> str:
    sheet_id, tab = _resolve_onboarding_and_welcome_tab()
    ws = core.get_worksheet(sheet_id, tab)
    header = _ensure_headers(ws, get_welcome_headers())
    run_welcome_ticket_repair_pass(min_interval_sec=3600)
    normalized_header = [_normalize_header_name(col) for col in header]
    ticket_value = _fmt_ticket(ticket)
    if not ticket_value:
        raise ValueError("ticket value is required for welcome ticket writes")
    if not _looks_like_ticket(ticket_value):
        raise ValueError(f"ticket value has unexpected format: {ticket_value}")
    if not str(username or "").strip():
        raise ValueError("username is required for welcome ticket writes")

    existing_match = None
    try:
        keys, search_values = _ticket_key_columns(header, ticket_value, thread_id)
        values = core.call_with_backoff(ws.get_all_values)
        for row in values[1:]:
            if _match_row(header, row, keys, search_values):
                existing_match = list(row)
                break
    except Exception:
        existing_match = None

    created, updated = _normalize_ticket_timestamps(created_at, updated_at)
    if existing_match and created_at is None:
        try:
            created_idx = normalized_header.index("createdat")
        except ValueError:
            created_idx = -1
        if created_idx >= 0 and created_idx < len(existing_match):
            existing_created = str(existing_match[created_idx] or "").strip()
            if existing_created:
                try:
                    created = datetime.fromisoformat(existing_created.replace("Z", "+00:00"))
                except ValueError:
                    pass

    clan_text = str(clan_tag or "").strip()
    closed_text = str(date_closed or "").strip()
    closed_text = _to_iso_date(closed_text)
    if existing_match:
        try:
            clan_idx = normalized_header.index("clantag")
            if not clan_text and clan_idx < len(existing_match):
                clan_text = str(existing_match[clan_idx] or "").strip()
        except ValueError:
            pass
        try:
            closed_idx = normalized_header.index("dateclosed")
            if not closed_text and closed_idx < len(existing_match):
                closed_text = _to_iso_date(str(existing_match[closed_idx] or "").strip())
        except ValueError:
            pass

    value_map: dict[str, str] = {
        "ticket": ticket_value,
        "ticketnumber": ticket_value,
        "ticketid": ticket_value,
        "username": str(username or "").strip(),
        "clantag": clan_text,
        "dateclosed": closed_text,
        "threadname": str(thread_name or "").strip(),
        "userid": str(user_id) if user_id is not None else "",
        "threadid": str(thread_id) if thread_id is not None else "",
        "thread": str(thread_id) if thread_id is not None else "",
        "panelmessageid": str(panel_message_id or ""),
        "status": str(status or "").strip(),
        "reviewreason": "",
        "createdat": created.isoformat(),
        "updatedat": updated.isoformat(),
    }
    try:
        final_headers = require_finalization_headers("welcome", header)
    except Exception:
        log.exception("welcome finalization header mapping unavailable; refusing welcome row write", extra={"ticket": ticket_value})
        raise
    existing_map = _row_map(header, existing_match or []) if existing_match else {}
    value_map[_normalize_header_name(final_headers["finalization_status"])] = existing_map.get(final_headers["finalization_status"], "") or "pending"
    value_map[_normalize_header_name(final_headers["reservation_status"])] = existing_map.get(final_headers["reservation_status"], "") or ("pending" if clan_text else "none")
    value_map[_normalize_header_name(final_headers["clan_update_status"])] = existing_map.get(final_headers["clan_update_status"], "") or "pending"
    value_map[_normalize_header_name(final_headers["finalization_note"])] = existing_map.get(final_headers["finalization_note"], "")

    row_values = _build_ticket_row(header, value_map)
    key_columns, search_values = _ticket_key_columns(header, ticket_value, thread_id)
    return _upsert(ws, key_columns, row_values, header, search_values=search_values)


def _row_map(header: Sequence[str], row: Sequence[str]) -> Dict[str, str]:
    """Return a header-to-value mapping for a sheet row."""

    return {
        str(name): (row[idx] if idx < len(row) else "")
        for idx, name in enumerate(header)
    }


def _find_row_by_thread_id(
    *, tab: str, headers: Sequence[str], thread_id: int | str | None
) -> Optional[Tuple[int, Dict[str, str]]]:
    """Return the (1-indexed) row number and mapped values for ``thread_id``."""

    if thread_id is None:
        return None
    target = str(thread_id).strip()
    if not target:
        return None

    ws = _worksheet(tab)
    if tab == _promo_tab():
        header = _ensure_promo_headers(ws)
    else:
        header = _ensure_headers(ws, headers)
    normalized_header = [_normalize_header_name(col) for col in header]
    thread_indexes = [
        idx
        for idx, name in enumerate(normalized_header)
        if name in {"threadid", "thread"}
    ]
    if not thread_indexes:
        return None

    values = core.call_with_backoff(ws.get_all_values)
    for row_idx, row in enumerate(values[1:], start=2):
        for col_idx in thread_indexes:
            current = row[col_idx] if col_idx < len(row) else ""
            if str(current or "").strip() == target:
                return row_idx, _row_map(header, row)
    return None


def find_welcome_row_by_thread_id(
    thread_id: int | str | None,
) -> Optional[Tuple[int, Dict[str, str]]]:
    """Return the Welcome row matching ``thread_id`` if present."""

    return _find_row_by_thread_id(
        tab=_welcome_tab(), headers=get_welcome_headers(), thread_id=thread_id
    )


def find_promo_row_by_thread_id(
    thread_id: int | str | None,
) -> Optional[Tuple[int, Dict[str, str]]]:
    """Return the Promo row matching ``thread_id`` if present."""

    result = _find_row_by_thread_id(
        tab=_promo_tab(), headers=get_promo_headers(), thread_id=thread_id
    )
    if result is not None:
        require_promo_source_clan_header(list(result[1].keys()))
    return result

def find_welcome_row(ticket: str | None) -> Optional[Tuple[int, List[str]]]:
    """Return the (1-indexed) row number and values for ``ticket`` if present."""

    if not ticket:
        return None

    ws = _worksheet(_welcome_tab())
    header = _ensure_headers(ws, get_welcome_headers())
    ticket_col = _column_index(header, "ticket_number")
    target = _fmt_ticket(ticket)

    values = core.call_with_backoff(ws.get_all_values)
    for row_idx, row in enumerate(values[1:], start=2):
        current = row[ticket_col] if ticket_col < len(row) else ""
        if _fmt_ticket(current) == target:
            return row_idx, list(row)
    return None


def upsert_promo(
    row_values: Sequence[str],
    headers: Sequence[str],
) -> str:
    """Insert or update a promo ticket row based on its ticket number.

    Existing metadata/finalization columns are preserved when older callers pass
    the core Promo values only. Finalization updates should use
    :func:`update_ticket_finalization_state`.
    """

    resolved_headers = list(headers or get_promo_headers())
    require_promo_source_clan_header(resolved_headers)
    ws = _worksheet(_promo_tab())
    actual_headers = _ensure_promo_headers(ws)
    final_headers = require_finalization_headers("promo", actual_headers)
    incoming = list(row_values)
    ticket_idx = _column_index(resolved_headers, "ticket number")
    ticket_value = incoming[ticket_idx] if ticket_idx < len(incoming) else ""
    existing = find_promo_row(ticket_value) if ticket_value else None
    existing_map = existing[1] if existing else {}
    if len(incoming) < len(resolved_headers):
        incoming.extend("" for _ in range(len(resolved_headers) - len(incoming)))
    for idx, header in enumerate(resolved_headers):
        if idx >= len(incoming):
            continue
        if str(incoming[idx] or "").strip():
            continue
        existing_value = existing_map.get(header, "")
        if str(existing_value or "").strip():
            incoming[idx] = existing_value
    for field, header in final_headers.items():
        col = _column_index(resolved_headers, header, default=-1)
        if col >= 0 and not str(incoming[col] or "").strip():
            incoming[col] = existing_map.get(header, "") or ("pending" if field != "finalization_note" else "")
    actual_idx = _promo_header_index(actual_headers)
    resolved_idx = {_normalize_header_name(name): pos for pos, name in enumerate(resolved_headers)}
    incoming_map: dict[str, str] = {}
    for normalized, actual_pos in actual_idx.items():
        source_pos = resolved_idx.get(normalized)
        value = incoming[source_pos] if source_pos is not None and source_pos < len(incoming) else ""
        if str(value or "").strip():
            incoming_map[normalized] = str(value)
    thread_value = incoming_map.get("threadid") or incoming_map.get("thread")
    return _promo_safe_upsert_mapped(
        ws,
        actual_headers,
        incoming_map,
        ticket=ticket_value,
        thread_id=thread_value,
        preserve_existing=False,
    )


PROMO_METADATA_REQUIRED_HEADERS = {
    "ticketnumber": "ticket number",
    "threadid": "thread_id",
}
PROMO_METADATA_FIELDS = {
    "ticketnumber",
    "username",
    "threadname",
    "userid",
    "threadid",
    "panelmessageid",
    "status",
    "reviewreason",
    "createdat",
    "updatedat",
}
PROMO_METADATA_PRESERVE_FIELDS = {
    "ticketnumber",
    "username",
    "clantag",
    "sourceclantag",
    "dateclosed",
    "type",
    "threadcreated",
    "finalizationstatus",
    "reservationstatus",
    "clanupdatestatus",
    "finalizationnote",
    "year",
    "month",
    "joinmonth",
    "clanname",
    "progression",
}
_LEGACY_USER_ID_REVIEW_REASONS = {
    "user_id_unresolved",
    "user_id unresolved from ticket tool intro message",
}
_USER_ID_REVIEW_REASONS = {
    "missing_user_id",
    "invalid_user_id",
    "discord_user_lookup_failed",
    *_LEGACY_USER_ID_REVIEW_REASONS,
}


def _promo_user_id_review_reason(
    *,
    stored_user_id: str,
    incoming_review_reason: str,
    existing_review_reason: str,
) -> str:
    """Return the precise user-id review reason for Promo ticket metadata.

    Legacy callers used ``user_id_unresolved`` for several different states.
    Keep the classification local to metadata patching so dynamic sheet/header
    behavior stays unchanged while stale rows are repaired on the next write.
    """

    user_id_text = str(stored_user_id or "").strip()
    requested_reason = str(incoming_review_reason or "").strip()
    existing_reason = str(existing_review_reason or "").strip()
    active_reason = requested_reason or existing_reason
    normalized_requested = requested_reason.lower()
    normalized_active = active_reason.lower()

    if not user_id_text:
        if normalized_active in _LEGACY_USER_ID_REVIEW_REASONS:
            return "missing_user_id"
        return active_reason
    if not _is_discord_id(user_id_text):
        if not active_reason or normalized_active in _USER_ID_REVIEW_REASONS:
            return "invalid_user_id"
        return active_reason
    if normalized_requested == "discord_user_lookup_failed":
        return requested_reason
    if normalized_active in _USER_ID_REVIEW_REASONS:
        return ""
    return active_reason


def patch_promo_ticket_metadata(
    *,
    ticket: str | None,
    thread_id: int | str | None = None,
    thread_name: str | None = None,
    username: str | None = None,
    user_id: int | str | None = None,
    panel_message_id: int | str | None = None,
    status: str | None = None,
    review_reason: str | None = None,
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> str:
    """Patch Promo ticket metadata using the live sheet headers.

    Existing nonblank values are preserved unless the incoming value is a
    lifecycle update that is expected to be newer (status, panel id, updated_at,
    review_reason). Business and finalization columns are never changed here.
    """

    sheet_id, tab = _resolve_onboarding_and_promo_tab()
    ws = core.get_worksheet(sheet_id, tab)
    header = _ensure_promo_headers(ws)
    normalized_header = [_normalize_header_name(col) for col in header]
    idx = {name: pos for pos, name in enumerate(normalized_header)}

    missing = [
        label
        for normalized, label in PROMO_METADATA_REQUIRED_HEADERS.items()
        if normalized not in idx
    ]
    if missing:
        log.warning(
            "promo metadata skipped: missing required header",
            extra={"missing_headers": missing, "ticket": _fmt_ticket(ticket), "thread_id": thread_id},
        )
        return "skipped_missing_header"

    ticket_value = _fmt_ticket(ticket)
    thread_value = str(thread_id or "").strip()
    if not ticket_value and not thread_value:
        log.warning("promo metadata skipped: missing ticket and thread_id")
        return "skipped_missing_key"

    values = core.call_with_backoff(ws.get_all_values)
    row_number: int | None = None
    row_values: list[str] | None = None

    thread_idx = idx.get("threadid")
    ticket_idx = idx.get("ticketnumber")

    if thread_value and thread_idx is not None:
        for current_idx, row in enumerate(values[1:], start=2):
            current = row[thread_idx] if thread_idx < len(row) else ""
            if str(current or "").strip() == thread_value:
                row_number = current_idx
                row_values = list(row)
                break

    if row_number is None and ticket_value and ticket_idx is not None:
        for current_idx, row in enumerate(values[1:], start=2):
            current = row[ticket_idx] if ticket_idx < len(row) else ""
            if _fmt_ticket(current) == ticket_value:
                row_number = current_idx
                row_values = list(row)
                break

    if row_values is None:
        row_values = ["" for _ in header]
    elif len(row_values) < len(header):
        row_values.extend("" for _ in range(len(header) - len(row_values)))

    now = updated_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    created = created_at
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)

    incoming = {
        "ticketnumber": ticket_value,
        "username": str(username or "").strip(),
        "threadname": str(thread_name or "").strip(),
        "userid": str(user_id).strip() if user_id is not None else "",
        "threadid": thread_value,
        "panelmessageid": str(panel_message_id).strip() if panel_message_id is not None else "",
        "status": str(status or "").strip(),
        "reviewreason": str(review_reason or "").strip(),
        "createdat": created.isoformat() if created is not None else "",
        "updatedat": now.isoformat(),
    }
    effective_user_id = incoming["userid"]
    user_id_idx = idx.get("userid")
    if not effective_user_id and user_id_idx is not None and user_id_idx < len(row_values):
        effective_user_id = str(row_values[user_id_idx] or "").strip()
    review_idx = idx.get("reviewreason")
    if review_idx is not None:
        existing_review = str(row_values[review_idx] or "").strip() if review_idx < len(row_values) else ""
        incoming["reviewreason"] = _promo_user_id_review_reason(
            stored_user_id=effective_user_id,
            incoming_review_reason=incoming["reviewreason"],
            existing_review_reason=existing_review,
        )

    changed = False
    for field, value in incoming.items():
        if field not in idx or field not in PROMO_METADATA_FIELDS:
            continue
        current = str(row_values[idx[field]] or "").strip()
        if not value and not (
            field == "reviewreason"
            and current
            and current.lower() in _USER_ID_REVIEW_REASONS
        ):
            continue
        if field in PROMO_METADATA_PRESERVE_FIELDS and current:
            continue
        if field in {"threadid", "userid", "threadname", "createdat"} and current:
            continue
        if field == "reviewreason" and current and current == value:
            continue
        if field == "panelmessageid" and current == value:
            continue
        if field == "status" and current == value:
            continue
        if field == "updatedat" and current == value:
            continue
        row_values[idx[field]] = value
        changed = True

    if not changed:
        return "unchanged"

    if row_number is None:
        core.call_with_backoff(ws.append_row, row_values[: len(header)], value_input_option="RAW")
        return "inserted"

    end_col = _col_to_a1(len(header) - 1)
    core.call_with_backoff(ws.update, f"A{row_number}:{end_col}{row_number}", [row_values[: len(header)]])
    return "updated"


def append_promo_ticket_row(
    ticket: str,
    username: str,
    clan_tag: str,
    source_clan_tag: str,
    promo_type: str,
    thread_created: str,
    year: str,
    month: str,
    join_month: str,
    clan_name: str,
    progression: str,
    *,
    thread_name: str | None = None,
    user_id: int | str | None = None,
    thread_id: int | None = None,
    panel_message_id: int | None = None,
    status: str = "open",
    created_at: datetime | None = None,
    updated_at: datetime | None = None,
) -> str:
    sheet_id, tab = _resolve_onboarding_and_promo_tab()
    ws = core.get_worksheet(sheet_id, tab)
    header = _read_promo_live_header_row(ws)
    source_header = require_promo_source_clan_header(header)
    ticket_value = _fmt_ticket(ticket)
    created, updated = _normalize_ticket_timestamps(created_at, updated_at)

    value_map: dict[str, str] = {
        "ticket": ticket_value,
        "ticketnumber": ticket_value,
        "ticketid": ticket_value,
        "username": str(username or "").strip(),
        "clantag": str(clan_tag or "").strip(),
        _normalize_header_name(source_header): str(source_clan_tag or "").strip(),
        "dateclosed": "",
        "type": str(promo_type or "").strip(),
        "threadcreated": str(thread_created or "").strip(),
        "year": str(year or "").strip(),
        "month": str(month or "").strip(),
        "joinmonth": str(join_month or "").strip(),
        "clanname": str(clan_name or "").strip(),
        "progression": str(progression or "").strip(),
        "threadname": str(thread_name or "").strip(),
        "userid": str(user_id) if user_id is not None else "",
        "threadid": str(thread_id) if thread_id is not None else "",
        "thread": str(thread_id) if thread_id is not None else "",
        "panelmessageid": str(panel_message_id or ""),
        "status": str(status or "").strip(),
        "reviewreason": "",
        "createdat": created.isoformat(),
        "updatedat": updated.isoformat(),
    }
    final_headers = require_finalization_headers("promo", header)
    value_map[_normalize_header_name(final_headers["finalization_status"])] = "pending"
    value_map[_normalize_header_name(final_headers["reservation_status"])] = "pending" if clan_tag else "none"
    value_map[_normalize_header_name(final_headers["clan_update_status"])] = "pending"
    value_map[_normalize_header_name(final_headers["finalization_note"])] = ""

    return _promo_safe_upsert_mapped(
        ws,
        header,
        value_map,
        ticket=ticket_value,
        thread_id=thread_id,
        preserve_existing=False,
    )


def append_onboarding_session_row(
    *,
    ticket: str,
    thread_id: int,
    user_id: int,
    flow: str,
    status: str,
    created_at: datetime | None = None,
) -> str:
    """Append or update an onboarding session row keyed by ticket/thread."""

    sheet_id, tab = _resolve_onboarding_and_sessions_tab()
    ws = core.get_worksheet(sheet_id, tab)
    header = core.call_with_backoff(ws.row_values, 1)
    if not header:
        raise RuntimeError("Onboarding sessions header missing; refusing to write")

    normalized_header = [_normalize_header_name(col) for col in header]

    ticket_value = _fmt_ticket(ticket)
    created = created_at or datetime.now(timezone.utc)
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    created_iso = created.isoformat()

    value_map: Dict[str, str] = {
        "ticket": ticket_value,
        "ticketnumber": ticket_value,
        "ticketid": ticket_value,
        "thread": str(thread_id),
        "threadid": str(thread_id),
        "userid": str(user_id),
        "user": str(user_id),
        "flow": str(flow or "").strip(),
        "status": str(status or "").strip(),
        "createdat": created_iso,
        "updatedat": created_iso,
    }

    row_values = [value_map.get(name, "") for name in normalized_header]

    def _ticket_formatter(value: str | None) -> str:
        return _fmt_ticket(value)

    key_columns: list[tuple[str, Callable[[str | None], str]]] = []
    for column, normalized in zip(header, normalized_header):
        if normalized in {"ticket", "ticketnumber", "ticketid"}:
            key_columns.append((column, _ticket_formatter))
            break
    if not key_columns:
        raise RuntimeError("Onboarding sessions tab missing ticket identifier column")

    return _upsert(ws, key_columns, row_values, header)


def find_promo_row(ticket: str | None) -> Optional[Tuple[int, Dict[str, str]]]:
    """Return the (1-indexed) row number and values for ``ticket`` if present."""

    if not ticket:
        return None

    ws = _worksheet(_promo_tab())
    header = _ensure_promo_headers(ws)
    require_promo_source_clan_header(header)
    ticket_col = _column_index(header, "ticket number")
    target = _fmt_ticket(ticket)

    values = core.call_with_backoff(ws.get_all_values)
    for row_idx, row in enumerate(values[1:], start=2):
        current = row[ticket_col] if ticket_col < len(row) else ""
        if _fmt_ticket(current) == target:
            mapped = {
                header[idx]: row[idx] if idx < len(row) else ""
                for idx in range(len(header))
            }
            return row_idx, mapped
    return None


def get_ticket_finalization_state(flow: str, row_values: Dict[str, str] | Sequence[str]) -> Dict[str, str]:
    headers = get_finalization_headers(flow)
    if isinstance(row_values, dict):
        return {field: str(row_values.get(header, "") or "").strip() for field, header in headers.items()}
    base_headers = get_welcome_headers() if flow == "welcome" else get_promo_headers()
    row_map = _row_map(base_headers, row_values)
    require_finalization_headers(flow, base_headers)
    return {field: str(row_map.get(header, "") or "").strip() for field, header in headers.items()}



def patch_promo_prompt_source(
    *,
    ticket: str | None,
    thread_id: int | str | None = None,
    source_clan_tag: str,
    finalization_status: str | None = None,
    finalization_note: str | None = None,
) -> str:
    """Patch only Promo source/finalization prompt fields on an existing row."""

    sheet_id, tab = _resolve_onboarding_and_promo_tab()
    ws = core.get_worksheet(sheet_id, tab)
    header = _ensure_promo_headers(ws)
    source_header = require_promo_source_clan_header(header)
    final_headers = require_finalization_headers("promo", header)
    values = core.call_with_backoff(ws.get_all_values)
    row_number, row = _promo_find_row_in_values(
        header,
        values,
        ticket=_fmt_ticket(ticket) if ticket else "",
        thread_id=str(thread_id or "").strip(),
    )
    if row_number is None or row is None:
        raise RuntimeError(f"promo prompt source row not found for ticket={ticket or '-'} thread_id={thread_id or '-'}")
    if len(row) < len(header):
        row.extend("" for _ in range(len(header) - len(row)))

    updates: dict[str, str] = {source_header: str(source_clan_tag or "").strip()}
    if finalization_status is not None:
        updates[final_headers["finalization_status"]] = str(finalization_status)
    if finalization_note is not None:
        updates[final_headers["finalization_note"]] = str(finalization_note)
    updated_col = next(
        (idx for idx, name in enumerate(_normalize_header_name(h) for h in header) if name == "updatedat"),
        -1,
    )
    if updated_col >= 0:
        row[updated_col] = datetime.now(timezone.utc).isoformat()

    for header_name, value in updates.items():
        col = _column_index(header, header_name, default=-1)
        if col < 0:
            raise RuntimeError(f"promo prompt source header missing: {header_name!r}")
        row[col] = value

    end_col = _col_to_a1(len(header) - 1)
    core.call_with_backoff(ws.update, f"A{row_number}:{end_col}{row_number}", [row[: len(header)]])
    return "updated"


def patch_promo_final_close(
    *,
    ticket: str | None,
    thread_id: int | str | None = None,
    clan_tag: str,
    source_clan_tag: str,
    date_closed: str,
    clan_name: str | None = None,
    progression: str | None = None,
    year: str | None = None,
    month: str | None = None,
    join_month: str | None = None,
    status: str | None = None,
) -> str:
    """Patch only intended Promo final-close fields on an existing row."""

    sheet_id, tab = _resolve_onboarding_and_promo_tab()
    ws = core.get_worksheet(sheet_id, tab)
    header = _read_promo_live_header_row(ws)
    source_header = get_promo_source_clan_tag_header()
    values = core.call_with_backoff(ws.get_all_values)
    row_number, row = _promo_find_row_in_values(
        header,
        values,
        ticket=_fmt_ticket(ticket) if ticket else "",
        thread_id=str(thread_id or "").strip(),
    )
    if row_number is None or row is None:
        raise RuntimeError(f"promo final close row not found for ticket={ticket or '-'} thread_id={thread_id or '-'}")
    if len(row) < len(header):
        row.extend("" for _ in range(len(header) - len(row)))

    normalized_to_col = {_normalize_header_name(name): idx for idx, name in enumerate(header)}
    required_updates: dict[str, str] = {
        "clantag": str(clan_tag or "").strip(),
        _normalize_header_name(source_header): str(source_clan_tag or "").strip(),
        "dateclosed": str(date_closed or "").strip(),
    }
    if status is not None:
        required_updates["status"] = str(status or "").strip()
    for normalized in required_updates:
        if normalized not in normalized_to_col:
            raise RuntimeError(
                "promo final close missing required header "
                f"normalized_field={normalized!r} ticket={ticket or '-'} thread_id={thread_id or '-'} "
                "operation=promo final close"
            )

    optional_updates: dict[str, str] = {}
    if clan_name is not None:
        optional_updates["clanname"] = str(clan_name or "").strip()
    if progression is not None:
        optional_updates["progression"] = str(progression or "").strip()

    year_text = str(year or "").strip()
    try:
        valid_year = int(year_text) > 1900
    except (TypeError, ValueError):
        valid_year = False
    if valid_year:
        optional_updates["year"] = year_text
        if str(month or "").strip():
            optional_updates["month"] = str(month or "").strip()
        if str(join_month or "").strip():
            optional_updates["joinmonth"] = str(join_month or "").strip()

    updated_col = normalized_to_col.get("updatedat")
    if updated_col is not None:
        row[updated_col] = datetime.now(timezone.utc).isoformat()

    changed = False
    for normalized, value in {**required_updates, **optional_updates}.items():
        col = normalized_to_col.get(normalized)
        if col is None:
            continue
        if normalized in required_updates and not value:
            raise RuntimeError(
                "promo final close missing required value "
                f"normalized_field={normalized!r} ticket={ticket or '-'} thread_id={thread_id or '-'} "
                "operation=promo final close"
            )
        if not value and normalized not in {"clanname", "progression"}:
            continue
        if str(row[col] if col < len(row) else "") == value:
            continue
        row[col] = value
        changed = True

    if not changed and updated_col is None:
        return "unchanged"
    end_col = _col_to_a1(len(header) - 1)
    core.call_with_backoff(ws.update, f"A{row_number}:{end_col}{row_number}", [row[: len(header)]])
    return "updated" if changed else "updated_timestamp"

def update_ticket_finalization_state(
    flow: str,
    *,
    ticket: str | None = None,
    thread_id: int | str | None = None,
    finalization_status: str | None = None,
    reservation_status: str | None = None,
    clan_update_status: str | None = None,
    finalization_note: str | None = None,
) -> str:
    """Patch only the finalization state columns for a Welcome/Promo row."""

    normalized = (flow or "").strip().lower()
    if normalized == "welcome":
        sheet_id, tab = _resolve_onboarding_and_welcome_tab()
        desired_headers = get_welcome_headers()
        ticket_header = "ticket_number"
    elif normalized == "promo":
        sheet_id, tab = _resolve_onboarding_and_promo_tab()
        desired_headers = get_promo_headers()
        ticket_header = "ticket number"
    else:
        raise ValueError(f"unknown onboarding finalization flow: {flow!r}")

    ws = core.get_worksheet(sheet_id, tab)
    header = _ensure_promo_headers(ws) if normalized == "promo" else _ensure_headers(ws, desired_headers)
    final_headers = require_finalization_headers(normalized, header)
    values = core.call_with_backoff(ws.get_all_values)
    target_ticket = _fmt_ticket(ticket) if ticket else ""
    target_thread = str(thread_id or "").strip()
    if normalized == "promo":
        row_number, row = _promo_find_row_in_values(
            header,
            values,
            ticket=target_ticket,
            thread_id=target_thread,
        )
    else:
        ticket_col = _column_index(header, ticket_header)
        thread_col = next((idx for idx, name in enumerate(_normalize_header_name(h) for h in header) if name in {"threadid", "thread"}), -1)
        row_number = None
        row = None
        for idx, current in enumerate(values[1:], start=2):
            ticket_match = bool(target_ticket) and ticket_col < len(current) and _fmt_ticket(current[ticket_col]) == target_ticket
            thread_match = bool(target_thread) and thread_col >= 0 and thread_col < len(current) and str(current[thread_col] or "").strip() == target_thread
            if ticket_match or thread_match:
                row_number = idx
                row = list(current)
                break
    if row_number is None or row is None:
        raise RuntimeError(f"{normalized} finalization row not found for ticket={ticket or '-'} thread_id={thread_id or '-'}")
    if len(row) < len(header):
        row.extend("" for _ in range(len(header) - len(row)))
    updates = {
        "finalization_status": finalization_status,
        "reservation_status": reservation_status,
        "clan_update_status": clan_update_status,
        "finalization_note": finalization_note,
    }
    for field, value in updates.items():
        if value is None:
            continue
        col = _column_index(header, final_headers[field], default=-1)
        if col < 0:
            raise RuntimeError(f"{normalized} finalization header missing for {field}: {final_headers[field]!r}")
        row[col] = str(value)
    updated_col = next(
        (
            idx
            for idx, name in enumerate(_normalize_header_name(h) for h in header)
            if name == "updatedat"
        ),
        -1,
    )
    if updated_col >= 0:
        row[updated_col] = datetime.now(timezone.utc).isoformat()
    end_col = _col_to_a1(len(header) - 1)
    core.call_with_backoff(ws.update, f"A{row_number}:{end_col}{row_number}", [row[: len(header)]])
    return "updated"


def list_ticket_rows_for_finalization_backfill(flow: str) -> List[Tuple[int, Dict[str, str]]]:
    normalized = (flow or "").strip().lower()
    if normalized == "welcome":
        ws = _worksheet(_welcome_tab())
        header = _ensure_headers(ws, get_welcome_headers())
    elif normalized == "promo":
        ws = _worksheet(_promo_tab())
        header = _read_promo_live_header_row(ws)
        header_index = _promo_header_index(header)
        required = {
            "ticketnumber": "ticket number",
            "finalizationstatus": "finalization_status",
        }
        missing = [label for key, label in required.items() if key not in header_index]
        if missing:
            raise RuntimeError("Promo backfill missing required header(s): " + ", ".join(missing))
        values = core.call_with_backoff(ws.get_all_values)
        return [(row_idx, _row_map(header, row)) for row_idx, row in enumerate(values[1:], start=2)]
    else:
        raise ValueError(f"unknown onboarding finalization flow: {flow!r}")
    require_finalization_headers(normalized, header)
    values = core.call_with_backoff(ws.get_all_values)
    return [(row_idx, _row_map(header, row)) for row_idx, row in enumerate(values[1:], start=2)]

def dedupe() -> Dict[str, int]:
    """Remove duplicate rows from welcome and promo sheets."""

    results = {
        "welcome": _dedupe_sheet(
            _worksheet(_welcome_tab()),
            key_columns=[("ticket number", _fmt_ticket)],
        ),
        "promo": _dedupe_sheet(
            _worksheet(_promo_tab()),
            key_columns=[
                ("ticket number", _fmt_ticket),
            ],
        ),
    }
    return results


def _collapse_row_ranges(indexes: Sequence[int]) -> List[Tuple[int, int]]:
    if not indexes:
        return []
    ranges: List[Tuple[int, int]] = []
    start = indexes[0]
    prev = indexes[0]
    for idx in indexes[1:]:
        if idx == prev + 1:
            prev = idx
            continue
        ranges.append((start, prev))
        start = idx
        prev = idx
    ranges.append((start, prev))
    return ranges


def _dedupe_sheet(
    ws,
    *,
    key_columns: Sequence[Tuple[str, Callable[[str | None], str]]],
) -> int:
    values = core.call_with_backoff(ws.get_all_values)
    if len(values) <= 1:
        return 0

    header = values[0]
    column_indexes = [_column_index(header, name) for name, _ in key_columns]
    seen: Dict[Tuple[str, ...], int] = {}
    for idx, row in enumerate(values[1:], start=2):
        key_parts: List[str] = []
        for col_index, (_, formatter) in zip(column_indexes, key_columns):
            current = row[col_index] if col_index < len(row) else ""
            key_parts.append(formatter(current))
        key = tuple(key_parts)
        seen[key] = idx  # keep last occurrence

    keep_rows = set(seen.values())
    to_delete = [
        row_idx
        for row_idx in range(2, len(values) + 1)
        if row_idx not in keep_rows
    ]
    if not to_delete:
        return 0

    ranges = _collapse_row_ranges(sorted(to_delete))
    requests = [
        {
            "deleteDimension": {
                "range": {
                    "sheetId": ws.id,
                    "dimension": "ROWS",
                    "startIndex": start - 1,
                    "endIndex": end,
                }
            }
        }
        for start, end in reversed(ranges)
    ]

    deleted = 0
    try:
        core.call_with_backoff(ws.spreadsheet.batch_update, {"requests": requests})
        for start, end in ranges:
            deleted += end - start + 1
        return deleted
    except Exception:
        pass

    # Fallback to per-row deletes if batch update fails.
    for row_idx in sorted(to_delete, reverse=True):
        try:
            core.call_with_backoff(ws.delete_rows, row_idx)
            deleted += 1
        except Exception:
            continue
    return deleted


def load_clan_tags(force: bool = False) -> List[str]:
    """Load and cache clan tags from the configured clan list tab."""

    global _CLAN_TAGS, _CLAN_TAG_TS
    now = time.time()
    if not force and _CLAN_TAGS and (now - _CLAN_TAG_TS) < _CLAN_TAG_TTL:
        return _CLAN_TAGS

    values = core.fetch_values(_sheet_id(), _clanlist_tab())
    tags: List[str] = []
    for row in values:
        if len(row) < 2:
            continue
        tag = (row[1] if len(row) > 1 else "").strip().upper()
        if tag:
            tags.append(tag)

    _CLAN_TAGS = tags
    _CLAN_TAG_TS = now
    return tags


# -----------------------------
# Phase 3 cache registrations
# -----------------------------
_TTL_CLAN_TAGS_SEC = 7 * 24 * 60 * 60


async def _load_clan_tags_async() -> List[str]:
    _ensure_service_account_credentials()
    sheet_id = _sheet_id()
    tab = await _aclanlist_tab()
    values = await afetch_values(sheet_id, tab)
    tags: List[str] = []
    for row in values:
        if len(row) < 2:
            continue
        tag = (row[1] if len(row) > 1 else "").strip().upper()
        if tag:
            tags.append(tag)
    return tags




def register_cache_buckets() -> None:
    """Register onboarding cache buckets if they are not already present."""

    if cache.get_bucket("clan_tags") is None:
        cache.register("clan_tags", _TTL_CLAN_TAGS_SEC, _load_clan_tags_async)
