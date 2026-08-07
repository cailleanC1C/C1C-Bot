"""Environment and dynamically routed workbook configuration."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Mapping
from shared.config import cfg
from modules.community.live_arena_tournament.models import SchemaError, norm

ENV_KEY = "LIVE_ARENA_TOURNAMENT_SHEET_ID"
REQUIRED_TABLE_KEYS = frozenset(
    {
        "tournaments",
        "eligible_clans",
        "roles",
        "destinations",
        "participants",
        "availability_slots",
        "participant_availability",
        "messages",
        "message_components",
        "bot_state",
        "audit_log",
    }
)


@dataclass(frozen=True, slots=True)
class FeatureConfig:
    sheet_id: str
    active_tournament_id: str
    tabs: Mapping[str, str]
    values: Mapping[str, str]


def sheet_id() -> str:
    return str(cfg.get(ENV_KEY, "") or "").strip()


def enabled() -> bool:
    return bool(sheet_id())


def parse_system_config(
    values: list[list[object]], configured_sheet_id: str
) -> FeatureConfig:
    if not values:
        raise SchemaError("System_Config is empty")
    headers = [norm(x) for x in values[0]]
    key_aliases = ("key", "config_key", "setting")
    value_aliases = ("value", "config_value", "setting_value")
    try:
        ki = next(headers.index(k) for k in key_aliases if k in headers)
        vi = next(headers.index(k) for k in value_aliases if k in headers)
    except StopIteration:
        raise SchemaError("System_Config requires normalized key and value headers")
    config = {
        norm(row[ki]): str(row[vi]).strip()
        for row in values[1:]
        if len(row) > max(ki, vi) and str(row[ki]).strip()
    }
    active = config.get("active_tournament_id", "")
    if not active:
        raise SchemaError("System_Config is missing active_tournament_id")
    tabs = {
        k[4:]: v
        for k, v in config.items()
        if (k.startswith("tab_") or k.startswith("tab.")) and v
    }
    missing = sorted(REQUIRED_TABLE_KEYS - tabs.keys())
    if missing:
        raise SchemaError(
            "System_Config is missing required tab.* routing keys: "
            + ", ".join(f"tab.{x}" for x in missing)
        )
    return FeatureConfig(configured_sheet_id, active, tabs, config)
