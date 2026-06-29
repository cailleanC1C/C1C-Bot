"""Cache bucket registrations for sheet-backed feature tabs not otherwise cached."""

from __future__ import annotations

import os
from typing import Awaitable, Callable

from shared.config import get_milestones_sheet_id
from shared.sheets import async_core
from shared.sheets import recruitment
from shared.sheets.cache_service import cache

_CACHE_TTL = int(os.getenv("SHEETS_CACHE_TTL_SEC", "900"))

Loader = Callable[[], Awaitable[list[list[str]]]]


def _missing_config_key(key: str) -> RuntimeError:
    return RuntimeError(f"missing Config key {key}")


def _recruitment_config_tab_loader(config_key: str) -> Loader:
    async def _load() -> list[list[str]]:
        tab_name = recruitment.get_config_value(config_key, None, force=True)
        if not tab_name:
            raise _missing_config_key(config_key)
        try:
            return await async_core.afetch_values(
                recruitment.get_recruitment_sheet_id(), tab_name
            )
        except Exception as exc:
            raise RuntimeError(f"could not read configured tab {tab_name}") from exc

    return _load


def _milestones_config_tab_loader(config_key: str) -> Loader:
    async def _load() -> list[list[str]]:
        from shared.config import cfg as runtime_config

        getter = getattr(runtime_config, "get", None)
        tab_name = ""
        if callable(getter):
            tab_name = str(getter(config_key, "") or "").strip()
        else:
            tab_name = str(getattr(runtime_config, config_key, "") or "").strip()
        if not tab_name:
            raise _missing_config_key(config_key)
        sheet_id = (get_milestones_sheet_id() or "").strip()
        if not sheet_id:
            raise RuntimeError("MILESTONES_SHEET_ID missing")
        try:
            return await async_core.afetch_values(sheet_id, tab_name)
        except Exception as exc:
            raise RuntimeError(f"could not read configured tab {tab_name}") from exc

    return _load


def _register(name: str, loader: Loader) -> None:
    if cache.get_bucket(name) is None:
        cache.register(name, _CACHE_TTL, loader)


def register_cache_buckets() -> None:
    """Register manually refreshable buckets for sheet-backed feature data."""

    recruitment_buckets: tuple[tuple[str, str], ...] = (
        ("clan_ad_messages", "clan_ad_messages_tab"),
        ("clan_ad_rules", "clan_ad_rules_tab"),
        ("reservations", "reservations_tab"),
        ("recruitment_reports", "reports_tab"),
        ("c1c_ad", "C1C_AD_TAB"),
        ("c1c_ad_text", "C1C_AD_TEXT_TAB"),
        ("cleanup_rules", "HOUSEKEEPING_CLEANUP_TAB"),
        ("keepalive_targets", "HOUSEKEEPING_KEEPALIVE_TAB"),
        ("whoweare_role_map", "rolemap_tab"),
    )
    for bucket, key in recruitment_buckets:
        _register(bucket, _recruitment_config_tab_loader(key))

    milestones_buckets: tuple[tuple[str, str], ...] = (
        ("reset_reminders", "RESET_REMINDER_TAB"),
        ("shard_mercy", "SHARD_MERCY_TAB"),
        ("shard_clans", "SHARD_CLANS_TAB"),
        ("shard_share_copy", "shard_share_copy_tab"),
        ("shard_voice_targets", "shard_share_voice_targets_tab"),
    )
    for bucket, key in milestones_buckets:
        _register(bucket, _milestones_config_tab_loader(key))
