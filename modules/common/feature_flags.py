"""Feature toggle loader with strict fail-closed defaults."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Mapping, Sequence

from shared.config import get_admin_role_ids, get_recruitment_sheet_id
from shared.sheets.async_core import afetch_records, afetch_values

log = logging.getLogger("c1c.features")

_DEFAULT_TOGGLES_TAB = "FeatureToggles"
_CONFIG_TAB_ENV = "RECRUITMENT_CONFIG_TAB"

_LOCK = asyncio.Lock()
_FEATURE_VALUES: Dict[str, bool] = {}
_LOADED_AT: datetime | None = None
_ROW_COUNT: int = 0
_SOURCE_TAB: str = _DEFAULT_TOGGLES_TAB
_DISABLED_BY_DEFAULT: bool = True
_NOTES: list[str] = []
_GLOBAL_FAILURE_REASON: str | None = "uninitialized"

_GLOBAL_WARNINGS_SENT: set[str] = set()
_INVALID_WARNINGS_SENT: set[str] = set()
_MISSING_WARNINGS_SENT: set[str] = set()

_DEFAULT_TRUE_FEATURES = {
    "housekeeping_enabled",
    "mirralith_overview_enabled",
    "ops_permissions_enabled",
    "promo_watcher_enabled",
    "resume_command_enabled",
    "welcome_watcher_enabled",
}

_TRUE_VALUES = {"true", "1"}
_FALSE_VALUES = {"false", "0"}


sys.modules.setdefault("shared.features", sys.modules[__name__])


def _normalize_key(value: object) -> str:
    text = str(value or "").strip()
    return text.lower()


def _admin_mention() -> str:
    role_ids = sorted(get_admin_role_ids())
    if role_ids:
        return f"<@&{role_ids[0]}>"
    return "@Administrator"


def _parse_enabled_value(raw_value: str) -> tuple[bool, bool]:
    normalized = str(raw_value or "").strip().lower()
    if normalized in _TRUE_VALUES:
        return True, True
    if normalized in _FALSE_VALUES:
        return False, True
    return False, False


async def _emit_admin_alert(detail: str) -> None:
    """Send a structured warning to the runtime log channel after startup."""

    message = f"⚠️ {_admin_mention()} Feature toggle misconfiguration: {detail}".strip()
    try:
        import asyncio
        from modules.common import runtime

        # Schedule asynchronously to avoid blocking before bot.start()
        asyncio.create_task(runtime.send_log_message(message))
    except Exception:
        import logging

        logging.warning(f"Feature toggle misconfiguration (deferred alert): {detail}")


def _schedule_admin_alert(detail: str) -> None:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        log.debug("event loop unavailable for feature toggle alert", exc_info=True)
        return
    loop.create_task(_emit_admin_alert(detail))


def _config_tab_name() -> str:
    raw = os.getenv(_CONFIG_TAB_ENV, "Config")
    text = str(raw or "").strip()
    return text or "Config"


def _parse_config(rows: Sequence[Mapping[str, object]]) -> Dict[str, str]:
    parsed: Dict[str, str] = {}
    for row in rows:
        key_value: str | None = None
        stored_value: str | None = None
        fallback: str | None = None
        for column, raw_value in row.items():
            column_norm = _normalize_key(column)
            text = str(raw_value or "").strip()
            if not text:
                continue
            if column_norm == "key":
                key_value = text.lower()
            elif column_norm in {"value", "val"}:
                stored_value = text
            elif fallback is None:
                fallback = text
        if key_value:
            parsed[key_value] = stored_value or fallback or ""
    return parsed


def _collect_headers(rows: Sequence[Mapping[str, object]]) -> set[str]:
    headers: set[str] = set()
    for row in rows:
        for column in row.keys():
            normalized = _normalize_key(column)
            if normalized:
                headers.add(normalized)
    return headers


def _extract_column(row: Mapping[str, object], column_name: str) -> str:
    want = column_name.lower()
    for column, value in row.items():
        if _normalize_key(column) == want:
            return str(value or "").strip()
    return ""


async def refresh() -> None:
    """Refresh feature toggles from Sheets (fail-closed)."""

    global _FEATURE_VALUES, _LOADED_AT, _ROW_COUNT
    global _SOURCE_TAB, _DISABLED_BY_DEFAULT, _NOTES, _GLOBAL_FAILURE_REASON

    async with _LOCK:
        now = datetime.now(timezone.utc)
        notes: list[str] = []
        feature_values: Dict[str, bool] = {}
        row_count = 0
        disabled = True
        failure_reason: str | None = None
        source_tab = _DEFAULT_TOGGLES_TAB

        sheet_id = (get_recruitment_sheet_id() or "").strip()
        if not sheet_id:
            failure_reason = "Recruitment sheet ID missing; all feature toggles disabled."
            notes.append(failure_reason)
            await _warn_global_once("missing-sheet-id", failure_reason)
        else:
            config_tab = _config_tab_name()
            try:
                config_rows = await afetch_records(sheet_id, config_tab)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure_reason = (
                    f"Config worksheet '{config_tab}' unavailable: {exc}. All features disabled."
                )
                notes.append(failure_reason)
                await _warn_global_once("config-load", failure_reason)
            else:
                config_map = _parse_config(config_rows)
                tab_name = config_map.get("feature_toggles_tab", "").strip() or _DEFAULT_TOGGLES_TAB
                source_tab = tab_name
                try:
                    records = await afetch_records(sheet_id, tab_name)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    failure_reason = (
                        f"Feature toggles worksheet '{tab_name}' unavailable: {exc}. All features disabled."
                    )
                    notes.append(failure_reason)
                    await _warn_global_once("tab-missing", failure_reason)
                else:
                    headers = _collect_headers(records)
                    if not headers:
                        try:
                            values = await afetch_values(sheet_id, tab_name)
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            values = []
                        if values:
                            first_row = values[0]
                            for cell in first_row:
                                normalized = _normalize_key(cell)
                                if normalized:
                                    headers.add(normalized)
                    required = {"feature_name", "enabled"}
                    if not required.issubset(headers):
                        failure_reason = (
                            f"Worksheet '{tab_name}' missing required headers: {sorted(required)}."
                        )
                        notes.append(failure_reason)
                        await _warn_global_once("header-missing", failure_reason)
                    else:
                        disabled = False
                        row_count = len(records)
                        for row in records:
                            feature_label = _extract_column(row, "feature_name")
                            if not feature_label:
                                continue
                            normalized_key = _normalize_key(feature_label)
                            enabled_raw = _extract_column(row, "enabled")
                            is_enabled_flag, is_valid_value = _parse_enabled_value(enabled_raw)
                            feature_values[normalized_key] = is_enabled_flag
                            if not is_valid_value and enabled_raw:
                                notes.append(
                                    f"{feature_label}: value '{enabled_raw}' treated as disabled"
                                )
                                await _warn_invalid_value_once(
                                    normalized_key, feature_label, enabled_raw, tab_name
                                )
                        if not feature_values:
                            notes.append("No feature rows resolved; defaulting to disabled.")
                            disabled = True
                            failure_reason = (
                                "Feature toggle worksheet returned no rows; treating all features as disabled."
                            )
                            await _warn_global_once("no-rows", failure_reason)

        for key in _DEFAULT_TRUE_FEATURES:
            feature_values.setdefault(key, True)

        _FEATURE_VALUES = feature_values
        _LOADED_AT = now
        _ROW_COUNT = row_count
        _SOURCE_TAB = source_tab
        _DISABLED_BY_DEFAULT = disabled or bool(failure_reason)
        _NOTES = notes
        _GLOBAL_FAILURE_REASON = failure_reason


async def _warn_global_once(token: str, detail: str) -> None:
    if token in _GLOBAL_WARNINGS_SENT:
        return
    _GLOBAL_WARNINGS_SENT.add(token)
    log.warning(detail)
    await _emit_admin_alert(detail)


async def _warn_invalid_value_once(
    normalized_key: str,
    feature_label: str,
    raw_value: str,
    tab_name: str,
) -> None:
    if normalized_key in _INVALID_WARNINGS_SENT:
        return
    _INVALID_WARNINGS_SENT.add(normalized_key)
    detail = (
        f"Toggle '{feature_label}' in worksheet '{tab_name}' has invalid value '{raw_value}'; "
        "defaulting to disabled."
    )
    log.warning(detail)
    await _emit_admin_alert(detail)


def is_enabled(key: str) -> bool:
    normalized = _normalize_key(key)
    if not normalized:
        return False
    if _GLOBAL_FAILURE_REASON:
        # Fail-closed when data missing or not yet loaded.
        return False
    if normalized in _FEATURE_VALUES:
        return bool(_FEATURE_VALUES[normalized])
    _warn_missing_feature_once(normalized, key)
    return False


def _warn_missing_feature_once(normalized_key: str, requested_key: str) -> None:
    if normalized_key in _MISSING_WARNINGS_SENT:
        return
    _MISSING_WARNINGS_SENT.add(normalized_key)
    detail = (
        f"Toggle '{requested_key}' is not defined in worksheet '{_SOURCE_TAB}'; defaulting to disabled."
    )
    log.warning(detail)
    _schedule_admin_alert(detail)


def snapshot() -> Dict[str, Any]:
    return {
        "loaded_at": _LOADED_AT.isoformat() if _LOADED_AT else None,
        "row_count": _ROW_COUNT,
        "source_tab": _SOURCE_TAB,
        "disabled_by_default": _DISABLED_BY_DEFAULT,
        "notes": list(_NOTES),
    }


def values() -> Dict[str, bool]:
    return dict(_FEATURE_VALUES)


__all__ = ["is_enabled", "refresh", "snapshot", "values"]
