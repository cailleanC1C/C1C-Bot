"""Config-tab cache service for Sheets-backed modules."""

from __future__ import annotations

import logging
import os
from typing import Iterable, Mapping

from shared.sheets.async_core import afetch_records

log = logging.getLogger("c1c.sheets.config_service")

# Cache settings mirror the general Sheets cache defaults.
_CACHE_TTL = int(os.getenv("SHEETS_CONFIG_CACHE_TTL_SEC", os.getenv("SHEETS_CACHE_TTL_SEC", "900")))

# Mapping of bucket → environment variable candidates for the sheet ID.
BUCKETS: dict[str, list[str]] = {
    "leagues": ["LEAGUES_SHEET_ID"],
}

# Optional per-bucket Config tab overrides.
_CONFIG_TABS: dict[str, str] = {
    "leagues": os.getenv("LEAGUES_CONFIG_TAB", "Config"),
}

# Only allow known keys through to the cached snapshot.
_ALLOWED_KEYS: set[str] = {
    "LEAGUE_LEGENDARY_TAB",
    "LEAGUE_LEGENDARY_HEADER",
    *(f"LEAGUE_LEGENDARY_{index}" for index in range(1, 10)),
    "LEAGUE_RISING_TAB",
    "LEAGUE_RISING_HEADER",
    *(f"LEAGUE_RISING_{index}" for index in range(1, 8)),
    "LEAGUE_STORM_TAB",
    "LEAGUE_STORM_HEADER",
    *(f"LEAGUE_STORM_{index}" for index in range(1, 5)),
}


def _resolve_sheet_id(env_keys: Iterable[str]) -> str:
    for env_key in env_keys:
        value = os.getenv(env_key, "").strip()
        if value:
            return value
    joined = ", ".join(env_keys)
    raise RuntimeError(f"sheet id not configured; tried: {joined}")


def _resolve_tab(bucket: str) -> str:
    return _CONFIG_TABS.get(bucket, "Config")


def _normalize_key(row: Mapping[str, object]) -> str:
    for key in ("SPEC_KEY", "KEY", "NAME"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().upper()
    return ""


def _runtime_config_columns(row: Mapping[str, object]) -> dict[str, object]:
    """Return only key/value fields from a Config row.

    Additional Config columns (for example ``description``) are operator
    documentation only and must not influence runtime feature behavior.
    """

    allowed = {"spec_key", "key", "name", "value", "val"}
    return {
        str(column): value
        for column, value in row.items()
        if str(column or "").strip().lower() in allowed
    }


def _filter_rows(rows: Iterable[Mapping[str, object]]) -> dict[str, Mapping[str, object]]:
    config: dict[str, Mapping[str, object]] = {}
    for row in rows or []:
        key = _normalize_key(row)
        if not key:
            continue
        if _ALLOWED_KEYS and key not in _ALLOWED_KEYS:
            continue
        config[key] = _runtime_config_columns(row)
    return config


async def _load_bucket(name: str) -> dict[str, object]:
    env_keys = BUCKETS.get(name)
    if not env_keys:
        raise KeyError(name)

    sheet_id = _resolve_sheet_id(env_keys)
    tab_name = _resolve_tab(name)
    rows = await afetch_records(sheet_id, tab_name)
    filtered = _filter_rows(rows or [])

    return {
        "sheet_id": sheet_id,
        "tab": tab_name,
        "config": filtered,
    }


def register_cache_buckets() -> None:
    from shared.sheets.cache_service import cache

    for bucket, env_keys in BUCKETS.items():
        if cache.get_bucket(bucket) is not None:
            continue
        log.info("registering sheet config bucket: %s", bucket)
        cache.register(bucket, _CACHE_TTL, lambda name=bucket: _load_bucket(name))


async def load(name: str) -> dict[str, object]:
    from shared.sheets.cache_service import cache

    bucket = name.strip()
    if not bucket:
        raise ValueError("bucket name is required")

    register_cache_buckets()

    payload = await cache.get(bucket)
    if payload is None:
        await cache.refresh_now(bucket, actor="config")
        payload = await cache.get(bucket)

    if isinstance(payload, dict):
        return payload
    return {}
