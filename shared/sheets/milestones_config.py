"""Canonical Milestones Config resolution helpers.

All Milestones workbook tab names used by runtime features should resolve through
this module so failures distinguish sheet-id, Config-load, missing-key, blank
value, and downstream tab-open errors.
"""

from __future__ import annotations

import difflib
import os
from typing import Any, Mapping, Sequence

from shared.config import get_milestones_sheet_id
from shared.sheets import async_core
from shared.sheets import core as sheets_core


class MilestonesConfigError(RuntimeError):
    reason = "config_error"

    def __init__(self, message: str, *, key: str | None = None, normalized_key: str | None = None,
                 sheet_id: str | None = None, source_tab: str | None = None,
                 keys_loaded: int | None = None, present: bool | None = None,
                 nearest: Sequence[str] = (), resolved_value: str | None = None) -> None:
        super().__init__(message)
        self.key = key
        self.normalized_key = normalized_key
        self.sheet_id = sheet_id
        self.source_tab = source_tab
        self.keys_loaded = keys_loaded
        self.present = present
        self.nearest = tuple(nearest)
        self.resolved_value = resolved_value

    @property
    def sheet_id_tail(self) -> str:
        return _tail(self.sheet_id)

    def context(self, *, component: str = "", operation: str = "config_resolve") -> dict[str, Any]:
        return {
            "component": component,
            "operation": operation,
            "config_key": self.key,
            "normalized_config_key": self.normalized_key,
            "sheet_id_tail": self.sheet_id_tail,
            "config_source_tab": self.source_tab,
            "config_keys_loaded": self.keys_loaded,
            "config_key_present": self.present,
            "nearest_config_keys": self.nearest,
            "resolved_config_value": self.resolved_value,
            "exception_type": type(self).__name__,
            "exception_message": str(self),
        }


class MilestonesSheetIdUnavailable(MilestonesConfigError):
    reason = "sheet_id_unavailable"


class MilestonesConfigSourceUnavailable(MilestonesConfigError):
    reason = "config_source_unavailable"


class MilestonesConfigLoadFailed(MilestonesConfigError):
    reason = "config_load_failed"


class MilestonesConfigEmpty(MilestonesConfigError):
    reason = "config_loaded_with_no_keys"


class MilestonesConfigKeyMissing(MilestonesConfigError):
    reason = "key_missing"


class MilestonesConfigValueBlank(MilestonesConfigError):
    reason = "key_value_blank"


class MilestonesResolvedTabUnavailable(MilestonesConfigError):
    reason = "resolved_tab_unavailable"


def _source_tab() -> str:
    tab = (os.getenv("MILESTONES_CONFIG_TAB") or "").strip()
    if not tab:
        raise MilestonesConfigSourceUnavailable("milestones Config source unavailable")
    return tab


def _tail(value: str | None) -> str:
    text = str(value or "").strip()
    return text[-6:] if text else "unavailable"


def normalize_key(key: object) -> str:
    return str(key or "").strip().upper()


def _parse(rows: Sequence[Mapping[str, object]]) -> dict[str, str]:
    out: dict[str, str] = {}
    for row in rows or []:
        found_key = ""
        found_value: object = ""
        has_value_column = False
        for col, raw in row.items():
            col_norm = str(col or "").strip().lower()
            text = str(raw or "").strip()
            if col_norm == "key":
                found_key = text
            elif col_norm in {"value", "val"}:
                found_value = raw
                has_value_column = True
        norm = normalize_key(found_key)
        if norm:
            out[norm] = str(found_value or "").strip() if has_value_column else ""
    return out


def _sheet_id() -> str:
    sheet_id = (get_milestones_sheet_id() or "").strip()
    if not sheet_id:
        raise MilestonesSheetIdUnavailable("milestones sheet ID unavailable")
    return sheet_id


def _missing(key: str, config: Mapping[str, str], sheet_id: str, tab: str) -> MilestonesConfigKeyMissing:
    norm = normalize_key(key)
    nearest = difflib.get_close_matches(norm, list(config.keys()), n=3, cutoff=0.55)
    return MilestonesConfigKeyMissing(
        f"{norm} missing in milestones Config tab",
        key=key, normalized_key=norm, sheet_id=sheet_id, source_tab=tab,
        keys_loaded=len(config), present=False, nearest=nearest,
    )


def load_values() -> tuple[str, str, dict[str, str]]:
    sheet_id = _sheet_id()
    tab = _source_tab()
    try:
        rows = sheets_core.fetch_records(sheet_id, tab)
    except Exception as exc:
        raise MilestonesConfigLoadFailed(
            f"Milestones Config load failed: {type(exc).__name__}: {exc}",
            sheet_id=sheet_id, source_tab=tab,
        ) from exc
    config = _parse(rows)
    if not config:
        raise MilestonesConfigEmpty(
            "Milestones Config loaded with no keys", sheet_id=sheet_id, source_tab=tab, keys_loaded=0
        )
    return sheet_id, tab, config


async def aload_values() -> tuple[str, str, dict[str, str]]:
    sheet_id = _sheet_id()
    tab = _source_tab()
    try:
        rows = await async_core.afetch_records(sheet_id, tab)
    except Exception as exc:
        raise MilestonesConfigLoadFailed(
            f"Milestones Config load failed: {type(exc).__name__}: {exc}",
            sheet_id=sheet_id, source_tab=tab,
        ) from exc
    config = _parse(rows)
    if not config:
        raise MilestonesConfigEmpty(
            "Milestones Config loaded with no keys", sheet_id=sheet_id, source_tab=tab, keys_loaded=0
        )
    return sheet_id, tab, config


def require_value(key: str) -> str:
    sheet_id, tab, config = load_values()
    norm = normalize_key(key)
    if norm not in config:
        raise _missing(key, config, sheet_id, tab)
    value = str(config[norm] or "").strip()
    if not value:
        raise MilestonesConfigValueBlank(
            f"{norm} value blank in milestones Config tab", key=key, normalized_key=norm,
            sheet_id=sheet_id, source_tab=tab, keys_loaded=len(config), present=True, resolved_value=value,
        )
    return value


async def arequire_value(key: str) -> str:
    sheet_id, tab, config = await aload_values()
    norm = normalize_key(key)
    if norm not in config:
        raise _missing(key, config, sheet_id, tab)
    value = str(config[norm] or "").strip()
    if not value:
        raise MilestonesConfigValueBlank(
            f"{norm} value blank in milestones Config tab", key=key, normalized_key=norm,
            sheet_id=sheet_id, source_tab=tab, keys_loaded=len(config), present=True, resolved_value=value,
        )
    return value


async def arequire_tab_values(key: str) -> tuple[str, str, list[list[Any]]]:
    sheet_id = _sheet_id()
    tab_name = await arequire_value(key)
    try:
        values = await async_core.afetch_values(sheet_id, tab_name)
    except Exception as exc:
        raise MilestonesResolvedTabUnavailable(
            f"resolved tab unavailable for {normalize_key(key)}: {tab_name}",
            key=key, normalized_key=normalize_key(key), sheet_id=sheet_id,
            source_tab=_source_tab(), resolved_value=tab_name,
        ) from exc
    return sheet_id, tab_name, values
