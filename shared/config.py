"""Runtime configuration helpers for the unified bot."""

from __future__ import annotations

import logging
import os
import re
import sys
from typing import Dict, Iterable, Mapping, Optional, Sequence, Set

from config import runtime as _runtime
from shared.redaction import mask_secret, mask_service_account, sanitize_text

__all__ = [
    "cfg",
    "reload_config",
    "get_config_snapshot",
    "get_env_name",
    "get_bot_name",
    "get_watchdog_check_sec",
    "get_watchdog_stall_sec",
    "get_watchdog_disconnect_grace_sec",
    "get_command_prefix",
    "get_discord_token",
    "get_allowed_guild_ids",
    "is_guild_allowed",
    "get_log_channel_id",
    "get_onboarding_log_channel_id",
    "get_notify_channel_id",
    "get_notify_ping_role_id",
    "get_recruiters_channel_id",
    "get_recruiters_thread_id",
    "get_report_recruiters_dest_id",
    "get_welcome_general_channel_id",
    "get_welcome_channel_id",
    "get_promo_channel_id",
    "get_role_map_channel_id",
    "get_who_we_are_channel_id",
    "get_admin_audit_dest_id",
    "get_refresh_times",
    "get_refresh_timezone",
    "get_gspread_credentials",
    "get_recruitment_sheet_id",
    "get_onboarding_sheet_id",
    "get_milestones_sheet_id",
    "get_onboarding_questions_tab",
    "get_onboarding_sessions_tab",
    "resolve_onboarding_tab",
    "resolve_onboarding_sessions_tab",
    "get_admin_role_ids",
    "get_staff_role_ids",
    "get_recruiter_role_ids",
    "get_clan_role_ids",
    "get_raid_role_id",
    "get_wandering_souls_role_id",
    "get_visitor_role_id",
    "get_recruitment_coordinator_role_ids",
    "get_guardian_knight_role_ids",
    "get_lead_role_ids",
    "get_feature_toggles",
    "get_strict_probe",
    "get_search_results_soft_cap",
    "get_clan_tags_cache_ttl_sec",
    "get_cleanup_interval_hours",
    "get_keepalive_channel_ids",
    "get_keepalive_thread_ids",
    "get_keepalive_interval_hours",
    "get_perms_blacklist_channel_ids",
    "get_perms_blacklist_category_ids",
    "get_onboarding_cleanup_after_summary",
    "get_panel_thread_mode",
    "get_panel_fixed_thread_id",
    "get_shard_mercy_tab",
    "get_shard_mercy_channel_id",
    "get_shard_panel_overview_emoji",
    "get_shard_emoji_ancient",
    "get_shard_emoji_void",
    "get_shard_emoji_sacred",
    "get_shard_emoji_primal",
    "get_public_base_url",
    "get_render_external_url",
    "get_emoji_max_bytes",
    "get_emoji_pad_size",
    "get_emoji_pad_box",
    "get_tag_badge_px",
    "get_tag_badge_box",
    "get_strict_emoji_proxy",
    "get_server_map_channel_id",
    "get_server_map_refresh_days",
    "redact_token",
    "redact_ids",
    "redact_value",
    "merge_onboarding_config_early",
    "onboarding_config_merge_count",
    "get_ticket_tool_bot_id",
    "features",
    "update_feature_flags_snapshot",
]

# Port helper lives in shared.ports. Import there where needed.

log = logging.getLogger("c1c.config")

# ===== Config Schema (authoritative) =====
_REQUIRED_ENV = (
    "DISCORD_TOKEN",
    "GSPREAD_CREDENTIALS",
    "RECRUITMENT_SHEET_ID",
)

def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or str(value).strip() == "":
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


for _name in _REQUIRED_ENV:
    _require_env(_name)

_SECRET_VALUE = "set"
_MISSING_VALUE = "—"
_INT_RE = re.compile(r"\d+")

_log_channel_warning_emitted = False

_CONFIG: Dict[str, object] = {}
LOG_CHANNEL_ID: Optional[int] | None = None

_LAST_ONBOARDING_CONFIG_KEYS = 0

_SECRET_KEYS = {
    "DISCORD_TOKEN",
    "GSPREAD_CREDENTIALS",
    "GOOGLE_SERVICE_ACCOUNT_JSON",
}


def _redact_value(key: str, value: object) -> str:
    """Best-effort redaction for import-time logging."""

    key_upper = str(key).upper()

    if (
        key_upper in _SECRET_KEYS
        or "TOKEN" in key_upper
        or "CREDENTIAL" in key_upper
        or "SERVICE_ACCOUNT" in key_upper
        or key_upper.endswith("_SECRET")
    ):
        if value in (None, "", [], (), {}):
            return _MISSING_VALUE
        text = str(value)
        stripped = text.strip()
        if not stripped:
            return _MISSING_VALUE
        masked = sanitize_text(text)
        if isinstance(masked, str) and masked != text:
            return masked
        if "service_account" in stripped and "private_key" in stripped:
            return mask_service_account(stripped)
        return mask_secret(stripped)

    if value in (None, "", [], (), {}):
        return _MISSING_VALUE

    redacted = sanitize_text(value)
    return str(redacted)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _first_int(raw: str | None) -> Optional[int]:
    if not raw:
        return None
    for match in _INT_RE.finditer(raw):
        try:
            return int(match.group(0))
        except (TypeError, ValueError):
            continue
    return None


def _refresh_log_channel() -> Optional[int]:
    """Refresh the cached log channel identifier and emit warnings once."""

    global LOG_CHANNEL_ID, _log_channel_warning_emitted

    LOG_CHANNEL_ID = _first_int(os.getenv("LOG_CHANNEL_ID"))
    if LOG_CHANNEL_ID is None:
        if not _log_channel_warning_emitted:
            log.warning(
                "Log channel disabled; set LOG_CHANNEL_ID to enable Discord log posting."
            )
            _log_channel_warning_emitted = True
    else:
        _log_channel_warning_emitted = False
    return LOG_CHANNEL_ID


def _int_set(raw: str | None) -> Set[int]:
    values: Set[int] = set()
    if not raw:
        return values
    for match in _INT_RE.finditer(raw):
        try:
            values.add(int(match.group(0)))
        except (TypeError, ValueError):
            continue
    return values


def _parse_schedule(raw: str | None, default: Sequence[str]) -> list[str]:
    parts = []
    if raw:
        for chunk in raw.split(","):
            item = chunk.strip()
            if item:
                parts.append(item)
    if parts:
        return parts
    return [str(item).strip() for item in default if str(item).strip()]


def get_ticket_tool_bot_id() -> Optional[int]:
    """Return the configured Ticket Tool bot identifier when available."""

    return _first_int(os.getenv("TICKET_TOOL_BOT_ID"))


def _int_env(
    key: str,
    default: int,
    *,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    """Parse an optional integer environment variable defensively."""

    raw = os.getenv(key)
    if raw is None:
        return default

    text = str(raw).strip()
    if not text:
        return default

    try:
        value = int(text)
    except Exception:  # pragma: no cover - defensive logging path
        logging.warning("config: %s='%s' invalid; using default %s", key, text, default)
        return default

    if min_value is not None and value < min_value:
        logging.warning("config: %s=%s < min %s; clamping", key, value, min_value)
        value = min_value

    if max_value is not None and value > max_value:
        logging.warning("config: %s=%s > max %s; clamping", key, value, max_value)
        value = max_value

    return value


def _float_env(
    key: str,
    default: float,
    *,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    """Parse an optional float environment variable defensively."""

    raw = os.getenv(key)
    if raw is None:
        return default

    text = str(raw).strip()
    if not text:
        return default

    try:
        value = float(text)
    except Exception:  # pragma: no cover - defensive logging path
        logging.warning("config: %s='%s' invalid; using default %s", key, text, default)
        return default

    if min_value is not None and value < min_value:
        logging.warning("config: %s=%s < min %s; clamping", key, value, min_value)
        value = min_value

    if max_value is not None and value > max_value:
        logging.warning("config: %s=%s > max %s; clamping", key, value, max_value)
        value = max_value

    return value


def _log_snapshot(snapshot: Dict[str, object]) -> None:
    redacted = {key: _redact_value(key, value) for key, value in snapshot.items()}
    log.info("config loaded", extra={"config": redacted})


def _load_onboarding_config_values() -> tuple[str, Dict[str, str]]:
    """Return onboarding config values keyed by upper-case strings."""

    sheet_id = (os.getenv("ONBOARDING_SHEET_ID") or "").strip()
    if not sheet_id:
        raise RuntimeError("ONBOARDING_SHEET_ID not set")

    onboarding_sheets = sys.modules.get("shared.sheets.onboarding")
    if onboarding_sheets is None:
        from shared.sheets import onboarding as onboarding_sheets  # type: ignore

    raw_config = onboarding_sheets._read_onboarding_config(sheet_id)  # type: ignore[attr-defined]
    normalized: Dict[str, str] = {}
    for key, value in raw_config.items():
        key_norm = (key or "").strip().upper()
        if not key_norm:
            continue
        text = "" if value is None else str(value).strip()
        normalized[key_norm] = text
    return sheet_id, normalized


def _merge_onboarding_tab(config: Dict[str, object]) -> None:
    """Merge the onboarding questions tab name from sheet config."""

    try:
        sheet_id, values = _load_onboarding_config_values()
    except RuntimeError:
        log.debug("config: onboarding sheet id not configured; skipping tab merge")
        return
    except Exception as exc:  # pragma: no cover - network or credential failures
        log.warning("config: failed to load onboarding Config tab: %s", exc)
        return

    if not values:
        return

    config.update(values)

    global _LAST_ONBOARDING_CONFIG_KEYS
    _LAST_ONBOARDING_CONFIG_KEYS = len(values)


def _parse_sheet_config(rows: Sequence[Mapping[str, object]]) -> Dict[str, str]:
    config: Dict[str, str] = {}
    for row in rows:
        key_value: str | None = None
        stored_value: str | None = None
        fallback: str | None = None
        for column, raw_value in row.items():
            column_norm = str(column or "").strip().lower()
            text = str(raw_value or "").strip()
            if column_norm == "key":
                key_value = text
            elif column_norm in {"value", "val"}:
                stored_value = text
            elif fallback is None and text:
                fallback = text
        if key_value:
            config[key_value.strip().upper()] = stored_value or fallback or ""
    return config


def _load_milestones_config_values() -> tuple[str, Dict[str, str]]:
    """Return Milestones config values keyed by upper-case strings."""

    sheet_id = (os.getenv("MILESTONES_SHEET_ID") or "").strip()
    if not sheet_id:
        raise RuntimeError("MILESTONES_SHEET_ID not set")

    tab_name = (os.getenv("MILESTONES_CONFIG_TAB") or "Config").strip() or "Config"
    from shared.sheets import core as sheets_core  # type: ignore

    rows = sheets_core.fetch_records(sheet_id, tab_name)
    return sheet_id, _parse_sheet_config(rows)


def _merge_milestones_tab(config: Dict[str, object]) -> None:
    """Merge shard tracker settings from the milestones Config tab."""

    try:
        _, values = _load_milestones_config_values()
    except RuntimeError:
        log.debug("config: milestones sheet id not configured; skipping tab merge")
        return
    except Exception as exc:  # pragma: no cover - network or credential failures
        log.warning("config: failed to load milestones Config tab: %s", exc)
        return

    if not values:
        return

    config.update(values)


def merge_onboarding_config_early() -> int:
    """Merge onboarding Config tab values into the live config mapping."""

    sheet_id, values = _load_onboarding_config_values()

    merged = 0
    for key, value in values.items():
        _CONFIG[key] = value
        merged += 1

    global _LAST_ONBOARDING_CONFIG_KEYS
    _LAST_ONBOARDING_CONFIG_KEYS = len(values)

    tail = sheet_id[-6:] if len(sheet_id) >= 6 else sheet_id
    display = f"…{tail}" if len(sheet_id) > len(tail) else tail
    result = "ok" if merged else "empty"
    log.info(
        "🧩 Config — merged onboarding tab • sheet=%s • keys=%d • result=%s",
        display,
        len(values),
        result,
        extra={"sheet_tail": tail, "keys": len(values), "result": result},
    )
    return len(values)


def onboarding_config_merge_count() -> int:
    """Return the number of onboarding config keys merged most recently."""

    return _LAST_ONBOARDING_CONFIG_KEYS


def _load_config() -> Dict[str, object]:
    keepalive = _runtime.get_watchdog_check_sec()
    stall = _runtime.get_watchdog_stall_sec()
    grace = _runtime.get_watchdog_disconnect_grace_sec(stall)

    # LOG_CHANNEL_ID handling (PR1: disabled if empty; warn once; keep behavior)
    log_channel = _refresh_log_channel()

    refresh_default = ("02:00", "10:00", "18:00")

    config: Dict[str, object] = {
        "PORT": _runtime.get_port(),
        "BOT_NAME": _runtime.get_bot_name(),
        "DISCORD_TOKEN": os.getenv("DISCORD_TOKEN", ""),
        "ENV_NAME": _runtime.get_env_name(),
        "GUILD_IDS": _int_set(os.getenv("GUILD_IDS")),
        "TIMEZONE": (os.getenv("TIMEZONE") or "Europe/Vienna").strip() or "Europe/Vienna",
        "REFRESH_TIMES": _parse_schedule(os.getenv("REFRESH_TIMES"), refresh_default),
        "GSPREAD_CREDENTIALS": os.getenv("GSPREAD_CREDENTIALS", ""),
        "RECRUITMENT_SHEET_ID": (os.getenv("RECRUITMENT_SHEET_ID") or "").strip(),
        "ONBOARDING_SHEET_ID": (os.getenv("ONBOARDING_SHEET_ID") or "").strip(),
        "MILESTONES_SHEET_ID": (os.getenv("MILESTONES_SHEET_ID") or "").strip(),
        "LEAGUES_SHEET_ID": (os.getenv("LEAGUES_SHEET_ID") or "").strip(),
        "ONBOARDING_TAB": (os.getenv("ONBOARDING_TAB") or "").strip(),
        "ONBOARDING_SESSIONS_TAB": (os.getenv("ONBOARDING_SESSIONS_TAB") or "").strip(),
        "ADMIN_ROLE_IDS": _int_set(os.getenv("ADMIN_ROLE_IDS")),
        "STAFF_ROLE_IDS": _int_set(os.getenv("STAFF_ROLE_IDS")),
        "RECRUITER_ROLE_IDS": _int_set(os.getenv("RECRUITER_ROLE_IDS")),
        "CLAN_ROLE_IDS": _int_set(os.getenv("CLAN_ROLE_IDS")),
        "RAID_ROLE_ID": _first_int(os.getenv("RAID_ROLE_ID")),
        "WANDERING_SOULS_ROLE_ID": _first_int(os.getenv("WANDERING_SOULS_ROLE_ID")),
        "VISITOR_ROLE_ID": _first_int(os.getenv("VISITOR_ROLE_ID")),
        "LEAD_ROLE_IDS": _int_set(os.getenv("LEAD_ROLE_IDS")),
        "CLAN_LEAD_IDS": _int_set(os.getenv("CLAN_LEAD_IDS")),
        "RECRUITERS_THREAD_ID": _first_int(os.getenv("RECRUITERS_THREAD_ID")),
        "RECRUITERS_CHANNEL_ID": _first_int(os.getenv("RECRUITERS_CHANNEL_ID")),
        "REPORT_RECRUITERS_DEST_ID": _first_int(os.getenv("REPORT_RECRUITERS_DEST_ID")),
        "LOGGING_CHANNEL_ID": _first_int(os.getenv("LOGGING_CHANNEL_ID")),
        "ADMIN_AUDIT_DEST_ID": _first_int(os.getenv("ADMIN_AUDIT_DEST_ID")),
        "RECRUITMENT_INTERACT_CHANNEL": _first_int(
            os.getenv("RECRUITMENT_INTERACT_CHANNEL")
        ),
        "WELCOME_GENERAL_CHANNEL_ID": _first_int(os.getenv("WELCOME_GENERAL_CHANNEL_ID")),
        "WELCOME_CHANNEL_ID": _first_int(os.getenv("WELCOME_CHANNEL_ID")),
        "PROMO_CHANNEL_ID": _first_int(os.getenv("PROMO_CHANNEL_ID")),
        "ROLEMAP_CHANNEL_ID": _first_int(os.getenv("ROLEMAP_CHANNEL_ID")),
        "WHO_WE_ARE_CHANNEL_ID": _first_int(os.getenv("WHO_WE_ARE_CHANNEL_ID")),
        "LOG_CHANNEL_ID": log_channel,
        "ONBOARDING_LOG_CHANNEL_ID": _first_int(os.getenv("ONBOARDING_LOG_CHANNEL_ID")),
        "NOTIFY_CHANNEL_ID": _first_int(os.getenv("NOTIFY_CHANNEL_ID")),
        "NOTIFY_PING_ROLE_ID": _first_int(os.getenv("NOTIFY_PING_ROLE_ID")),
        "STRICT_PROBE": _env_bool("STRICT_PROBE", False),
        "SEARCH_RESULTS_SOFT_CAP": _int_env("SEARCH_RESULTS_SOFT_CAP", 25, min_value=1),
        "WATCHDOG_CHECK_SEC": keepalive,
        "WATCHDOG_STALL_SEC": stall,
        "WATCHDOG_DISCONNECT_GRACE_SEC": grace,
        "CLAN_TAGS_CACHE_TTL_SEC": _int_env("CLAN_TAGS_CACHE_TTL_SEC", 3600, min_value=60),
        "CLEANUP_INTERVAL_HOURS": _int_env(
            "CLEANUP_INTERVAL_HOURS", 24, min_value=1
        ),
        "KEEPALIVE_CHANNEL_IDS": _int_set(os.getenv("KEEPALIVE_CHANNEL_IDS")),
        "KEEPALIVE_THREAD_IDS": _int_set(os.getenv("KEEPALIVE_THREAD_IDS")),
        "KEEPALIVE_INTERVAL_HOURS": _int_env(
            "KEEPALIVE_INTERVAL_HOURS", 144, min_value=1
        ),
        "PERMS_BLACKLIST_CHANNEL_IDS": _int_set(
            os.getenv("PERMS_BLACKLIST_CHANNEL_IDS")
        ),
        "PERMS_BLACKLIST_CATEGORY_IDS": _int_set(
            os.getenv("PERMS_BLACKLIST_CATEGORY_IDS")
        ),
        "PANEL_THREAD_MODE": (os.getenv("PANEL_THREAD_MODE") or "same").strip().lower() or "same",
        "PANEL_FIXED_THREAD_ID": _first_int(os.getenv("PANEL_FIXED_THREAD_ID")),
        "BOT_VERSION": os.getenv("BOT_VERSION", "dev"),
        "LOG_LEVEL": os.getenv("LOG_LEVEL", "INFO"),
        "PUBLIC_BASE_URL": (os.getenv("PUBLIC_BASE_URL") or "").strip(),
        "RENDER_EXTERNAL_URL": (os.getenv("RENDER_EXTERNAL_URL") or "").strip(),
        "EMOJI_MAX_BYTES": _int_env("EMOJI_MAX_BYTES", 2_000_000, min_value=1),
        "EMOJI_PAD_SIZE": _int_env("EMOJI_PAD_SIZE", 256, min_value=64, max_value=512),
        "EMOJI_PAD_BOX": _float_env("EMOJI_PAD_BOX", 0.85, min_value=0.2, max_value=0.95),
        "TAG_BADGE_PX": _int_env("TAG_BADGE_PX", 128, min_value=32, max_value=512),
        "TAG_BADGE_BOX": _float_env("TAG_BADGE_BOX", 0.90, min_value=0.2, max_value=0.95),
        "STRICT_EMOJI_PROXY": _env_bool("STRICT_EMOJI_PROXY", True),
        "SERVER_MAP_CHANNEL_ID": _first_int(os.getenv("SERVER_MAP_CHANNEL_ID")),
        "SERVER_MAP_REFRESH_DAYS": _int_env("SERVER_MAP_REFRESH_DAYS", 30, min_value=1),
    }

    if os.getenv("ENABLE_WELCOME_WATCHER") not in (None, ""):
        log.warning(
            "Legacy ENABLE_WELCOME_WATCHER detected; set ENABLE_WELCOME_HOOK and remove the old key."
        )

    _merge_onboarding_tab(config)
    _merge_milestones_tab(config)

    return config


def reload_config() -> Dict[str, object]:
    """Reload configuration from environment and return a snapshot."""

    for _name in _REQUIRED_ENV:
        _require_env(_name)

    snapshot = _load_config()

    global _CONFIG
    _CONFIG = snapshot
    _log_snapshot(snapshot)
    return dict(_CONFIG)


reload_config()


def _normalise_key(name: object) -> Optional[str]:
    if name is None:
        return None

    text = str(name).strip()
    if not text:
        return None

    mapped = re.sub(r"[^A-Za-z0-9_.]", "_", text)
    mapped = mapped.replace(".", "_")
    mapped = re.sub(r"__+", "_", mapped)
    mapped = mapped.strip("_")
    if not mapped:
        return None
    return mapped.upper()


class _ConfigFacade:
    __slots__ = ()

    def get(self, key: object, default: object | None = None) -> object | None:
        normalised = _normalise_key(key)
        if not normalised:
            return default
        return _CONFIG.get(normalised, default)

    def __contains__(self, key: object) -> bool:  # pragma: no cover - convenience
        normalised = _normalise_key(key)
        if not normalised:
            return False
        return normalised in _CONFIG

    def items(self):  # pragma: no cover - convenience
        return _CONFIG.items()

    def keys(self):  # pragma: no cover - convenience
        return _CONFIG.keys()

    def values(self):  # pragma: no cover - convenience
        return _CONFIG.values()

    def __getattr__(self, name: str):
        normalised = _normalise_key(name)
        if normalised and normalised in _CONFIG:
            return _CONFIG[normalised]
        target = globals().get(name)
        if target is None:
            raise AttributeError(name)
        return target


cfg = _ConfigFacade()


class _ConfigFeatures:
    __slots__ = (
        "housekeeping_enabled",
        "mirralith_overview_enabled",
        "ops_permissions_enabled",
        "promo_watcher_enabled",
        "resume_command_enabled",
        "shard_tracker_enabled",
        "welcome_watcher_enabled",
    )

    def __init__(self) -> None:
        self.housekeeping_enabled = True
        self.mirralith_overview_enabled = True
        self.ops_permissions_enabled = True
        self.promo_watcher_enabled = True
        self.resume_command_enabled = True
        self.shard_tracker_enabled = False
        self.welcome_watcher_enabled = True

    def update(self, toggles: Mapping[str, bool] | None = None) -> None:
        values = toggles or {}
        self.housekeeping_enabled = bool(values.get("housekeeping_enabled", True))
        self.mirralith_overview_enabled = bool(
            values.get("mirralith_overview_enabled", True)
        )
        self.ops_permissions_enabled = bool(values.get("ops_permissions_enabled", True))
        self.promo_watcher_enabled = bool(values.get("promo_watcher_enabled", True))
        self.resume_command_enabled = bool(values.get("resume_command_enabled", True))
        candidates = ("shardtracker", "shard_tracker", "shard_tracker_enabled")
        self.shard_tracker_enabled = any(bool(values.get(key)) for key in candidates)
        self.welcome_watcher_enabled = bool(values.get("welcome_watcher_enabled", True))


features = _ConfigFeatures()


def update_feature_flags_snapshot(toggles: Mapping[str, bool] | None) -> None:
    """Refresh the cached feature toggle state for config consumers."""

    features.update(toggles)


def get_config_snapshot() -> Dict[str, object]:
    """Return a shallow copy of the cached config values."""

    return dict(_CONFIG)

def get_env_name(default: str = "dev") -> str:
    value = _CONFIG.get("ENV_NAME")
    return str(value) if isinstance(value, str) and value else default


def get_bot_name(default: str = "C1C-Recruitment") -> str:
    value = _CONFIG.get("BOT_NAME")
    return str(value) if isinstance(value, str) and value else default


def get_watchdog_check_sec(default_prod: int = 360, default_nonprod: int = 60) -> int:
    fallback = _runtime.get_watchdog_check_sec(default_prod, default_nonprod)
    try:
        return int(_CONFIG.get("WATCHDOG_CHECK_SEC", fallback))
    except (TypeError, ValueError):
        return fallback


def get_watchdog_stall_sec(default: Optional[int] = None) -> int:
    value = _CONFIG.get("WATCHDOG_STALL_SEC")
    if isinstance(value, int):
        return value
    if default is not None:
        return default
    return _runtime.get_watchdog_stall_sec(default)


def get_watchdog_disconnect_grace_sec(default: Optional[int] = None) -> int:
    value = _CONFIG.get("WATCHDOG_DISCONNECT_GRACE_SEC")
    if isinstance(value, int):
        return value
    if default is not None:
        return default
    return _runtime.get_watchdog_disconnect_grace_sec(default)


def get_command_prefix(default: str = "!") -> str:
    return "!"


def get_discord_token() -> str:
    token = _CONFIG.get("DISCORD_TOKEN", "")
    return str(token)


def get_allowed_guild_ids() -> Set[int]:
    raw = _CONFIG.get("GUILD_IDS", set())
    if isinstance(raw, set):
        return set(raw)
    if isinstance(raw, (list, tuple)):
        result: Set[int] = set()
        for value in raw:
            try:
                result.add(int(value))
            except (TypeError, ValueError):
                continue
        return result
    return set()


def is_guild_allowed(guild_id: int) -> bool:
    allowed = get_allowed_guild_ids()
    if not allowed:
        return True
    try:
        value = int(guild_id)
    except (TypeError, ValueError):
        return False
    return value in allowed


def _coerce_int(value: object, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _optional_id(key: str) -> Optional[int]:
    value = _CONFIG.get(key)
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def get_log_channel_id() -> Optional[int]:
    return _optional_id("LOG_CHANNEL_ID")


def get_onboarding_log_channel_id() -> Optional[int]:
    return _optional_id("ONBOARDING_LOG_CHANNEL_ID")


def get_logging_channel_id() -> Optional[int]:
    return _optional_id("LOGGING_CHANNEL_ID")


def get_notify_channel_id() -> Optional[int]:
    return _optional_id("NOTIFY_CHANNEL_ID")


def get_notify_ping_role_id() -> Optional[int]:
    return _optional_id("NOTIFY_PING_ROLE_ID")


def get_admin_audit_dest_id() -> Optional[int]:
    return _optional_id("ADMIN_AUDIT_DEST_ID")


def get_recruiters_channel_id() -> Optional[int]:
    return _optional_id("RECRUITERS_CHANNEL_ID")


def get_recruiters_thread_id() -> Optional[int]:
    return _optional_id("RECRUITERS_THREAD_ID")


def get_report_recruiters_dest_id() -> Optional[int]:
    return _optional_id("REPORT_RECRUITERS_DEST_ID")


def get_recruitment_interact_channel_id() -> Optional[int]:
    return _optional_id("RECRUITMENT_INTERACT_CHANNEL")


def get_welcome_general_channel_id() -> Optional[int]:
    return _optional_id("WELCOME_GENERAL_CHANNEL_ID")


def get_welcome_channel_id() -> Optional[int]:
    return _optional_id("WELCOME_CHANNEL_ID")


def get_promo_channel_id() -> Optional[int]:
    return _optional_id("PROMO_CHANNEL_ID")


def get_role_map_channel_id() -> Optional[int]:
    return _optional_id("ROLEMAP_CHANNEL_ID")


def get_who_we_are_channel_id() -> Optional[int]:
    return _optional_id("WHO_WE_ARE_CHANNEL_ID")


def get_refresh_times(default: Iterable[str] = ("02:00", "10:00", "18:00")) -> list[str]:
    raw = _CONFIG.get("REFRESH_TIMES")
    if isinstance(raw, (list, tuple, set)):
        values = [str(part).strip() for part in raw if str(part).strip()]
        if values:
            return values
    return [str(x).strip() for x in default if str(x).strip()]


def get_refresh_timezone(default: str = "Europe/Vienna") -> str:
    raw = _CONFIG.get("TIMEZONE")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return default


def get_gspread_credentials() -> str:
    value = _CONFIG.get("GSPREAD_CREDENTIALS", "")
    return str(value)


def get_recruitment_sheet_id() -> str:
    return str(_CONFIG.get("RECRUITMENT_SHEET_ID", ""))


def get_onboarding_sheet_id() -> str:
    return str(_CONFIG.get("ONBOARDING_SHEET_ID", ""))


def get_milestones_sheet_id() -> str:
    return str(_CONFIG.get("MILESTONES_SHEET_ID", ""))


def get_onboarding_questions_tab() -> str:
    try:
        return resolve_onboarding_tab(cfg)
    except KeyError:
        return ""


def resolve_onboarding_tab(config: Mapping[str, object] | object) -> str:
    """
    Returns the sheet tab name for onboarding questions.
    Raises KeyError if the key is missing or empty.
    """

    source = cfg if config is None else config
    getter = getattr(source, "get", None)
    if getter is None and isinstance(source, Mapping):
        getter = source.get
    if getter is None:
        raise TypeError("config must provide a get() method")

    def _lookup(key: str) -> str:
        try:
            value = getter(key, None)  # type: ignore[misc]
        except Exception:
            return ""
        if value is None:
            return ""
        return str(value).strip()

    tab = _lookup("ONBOARDING_TAB")
    if not tab:
        raise KeyError("missing config key: ONBOARDING_TAB")
    return tab


def get_onboarding_sessions_tab() -> str:
    try:
        return resolve_onboarding_sessions_tab(cfg)
    except KeyError:
        return ""


def resolve_onboarding_sessions_tab(config: Mapping[str, object] | object) -> str:
    """
    Resolve the tab containing onboarding session rows (user_id, thread_id, answers, etc.).
    """

    source = cfg if config is None else config
    getter = getattr(source, "get", None)
    if getter is None and isinstance(source, Mapping):
        getter = source.get
    if getter is None:
        raise TypeError("config must provide a get() method")

    def _lookup(key: str) -> str:
        try:
            value = getter(key, None)  # type: ignore[misc]
        except Exception:
            return ""
        if value is None:
            return ""
        return str(value).strip()

    tab = _lookup("ONBOARDING_SESSIONS_TAB")
    if not tab:
        raise KeyError("missing config key: ONBOARDING_SESSIONS_TAB")
    return tab


def _role_set(key: str) -> Set[int]:
    raw = _CONFIG.get(key, set())
    if isinstance(raw, set):
        return set(raw)
    if isinstance(raw, (list, tuple)):
        result: Set[int] = set()
        for value in raw:
            try:
                result.add(int(value))
            except (TypeError, ValueError):
                continue
        return result
    return set()


def get_admin_role_ids() -> Set[int]:
    return _role_set("ADMIN_ROLE_IDS")


def get_staff_role_ids() -> Set[int]:
    return _role_set("STAFF_ROLE_IDS")


def get_recruiter_role_ids() -> Set[int]:
    return _role_set("RECRUITER_ROLE_IDS")


def get_clan_role_ids() -> Set[int]:
    return _role_set("CLAN_ROLE_IDS")


def get_raid_role_id() -> Optional[int]:
    return _optional_id("RAID_ROLE_ID")


def get_wandering_souls_role_id() -> Optional[int]:
    return _optional_id("WANDERING_SOULS_ROLE_ID")


def get_visitor_role_id() -> Optional[int]:
    return _optional_id("VISITOR_ROLE_ID")


def get_recruitment_coordinator_role_ids() -> Set[int]:
    return _role_set("RECRUITMENT_COORDINATOR_ROLE_IDS")


def get_guardian_knight_role_ids() -> Set[int]:
    return _role_set("GUARDIAN_KNIGHT_ROLE_IDS")


def get_lead_role_ids() -> Set[int]:
    return _role_set("LEAD_ROLE_IDS")


def get_clan_lead_ids() -> Set[int]:
    return _role_set("CLAN_LEAD_IDS")


def get_feature_toggles() -> Dict[str, bool]:
    """Return the merged feature toggles from the runtime loader."""

    try:
        from shared import features  # Local import to avoid circular dependency.
    except Exception:
        return {}

    values = getattr(features, "_FEATURE_VALUES", None)
    if isinstance(values, dict):
        toggles: Dict[str, bool] = {}
        for key, raw_value in values.items():
            name = str(key).strip()
            if not name:
                continue
            toggles[name] = bool(raw_value)
        return toggles

    return {}


def get_strict_probe() -> bool:
    return bool(_CONFIG.get("STRICT_PROBE", False))


def get_search_results_soft_cap(default: int = 25) -> int:
    value = _CONFIG.get("SEARCH_RESULTS_SOFT_CAP", default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_clan_tags_cache_ttl_sec(default: int = 3600) -> int:
    value = _CONFIG.get("CLAN_TAGS_CACHE_TTL_SEC", default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_cleanup_interval_hours(default: int = 24) -> int:
    value = _CONFIG.get("CLEANUP_INTERVAL_HOURS", default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_keepalive_channel_ids() -> Set[int]:
    value = _CONFIG.get("KEEPALIVE_CHANNEL_IDS")
    if isinstance(value, set):
        return set(value)
    return set()


def get_keepalive_thread_ids() -> Set[int]:
    value = _CONFIG.get("KEEPALIVE_THREAD_IDS")
    if isinstance(value, set):
        return set(value)
    return set()


def get_keepalive_interval_hours(default: int = 144) -> int:
    value = _CONFIG.get("KEEPALIVE_INTERVAL_HOURS", default)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_perms_blacklist_channel_ids() -> Set[int]:
    value = _CONFIG.get("PERMS_BLACKLIST_CHANNEL_IDS")
    if isinstance(value, set):
        return set(value)
    return set()


def get_perms_blacklist_category_ids() -> Set[int]:
    value = _CONFIG.get("PERMS_BLACKLIST_CATEGORY_IDS")
    if isinstance(value, set):
        return set(value)
    return set()


def get_onboarding_cleanup_after_summary() -> bool:
    raw = os.getenv("ONB_CLEANUP_AFTER_SUMMARY")
    if raw is None:
        return True
    value = raw.strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return True


def get_panel_thread_mode(default: str = "same") -> str:
    value = _CONFIG.get("PANEL_THREAD_MODE", default)
    text = str(value).strip().lower()
    return text or default


def get_panel_fixed_thread_id() -> Optional[int]:
    return _optional_id("PANEL_FIXED_THREAD_ID")


def get_shard_mercy_tab(default: str = "") -> str:
    value = _CONFIG.get("SHARD_MERCY_TAB", default)
    if isinstance(value, str):
        trimmed = value.strip()
        if trimmed:
            return trimmed
    return default


def get_shard_mercy_channel_id() -> int:
    value = _CONFIG.get("SHARD_MERCY_CHANNEL_ID")
    try:
        if value is None:
            return 0
        return int(str(value).strip())
    except (TypeError, ValueError):
        return 0


def _clean_emoji_value(key: str, default: str) -> str:
    value = _CONFIG.get(key, default)
    if isinstance(value, str):
        text = value.strip()
        if text:
            return text
    return default


def get_shard_panel_overview_emoji(default: str = "c1c") -> str:
    return _clean_emoji_value("SHARD_PANEL_OVERVIEW_EMOJI", default)


def get_shard_emoji_ancient(default: str = "ancient") -> str:
    return _clean_emoji_value("SHARD_EMOJI_ANCIENT", default)


def get_shard_emoji_void(default: str = "void") -> str:
    return _clean_emoji_value("SHARD_EMOJI_VOID", default)


def get_shard_emoji_sacred(default: str = "sacred") -> str:
    return _clean_emoji_value("SHARD_EMOJI_SACRED", default)


def get_shard_emoji_primal(default: str = "primal") -> str:
    return _clean_emoji_value("SHARD_EMOJI_PRIMAL", default)


def get_public_base_url() -> str | None:
    value = _CONFIG.get("PUBLIC_BASE_URL")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def get_render_external_url() -> str | None:
    value = _CONFIG.get("RENDER_EXTERNAL_URL")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def get_emoji_max_bytes(default: int = 2_000_000) -> int:
    value = _CONFIG.get("EMOJI_MAX_BYTES", default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def get_emoji_pad_size(default: int = 256) -> int:
    value = _CONFIG.get("EMOJI_PAD_SIZE", default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(64, min(512, parsed))


def get_emoji_pad_box(default: float = 0.85) -> float:
    value = _CONFIG.get("EMOJI_PAD_BOX", default)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.2, min(0.95, parsed))


def get_tag_badge_px(default: int = 128) -> int:
    value = _CONFIG.get("TAG_BADGE_PX", default)
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(32, min(512, parsed))


def get_tag_badge_box(default: float = 0.9) -> float:
    value = _CONFIG.get("TAG_BADGE_BOX", default)
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(0.2, min(0.95, parsed))


def get_strict_emoji_proxy(default: bool = True) -> bool:
    value = _CONFIG.get("STRICT_EMOJI_PROXY")
    if isinstance(value, bool):
        return value
    return default


def get_server_map_channel_id() -> Optional[int]:
    value = _CONFIG.get("SERVER_MAP_CHANNEL_ID")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_server_map_refresh_days(default: int = 30) -> int:
    try:
        value = int(_CONFIG.get("SERVER_MAP_REFRESH_DAYS", default))
    except (TypeError, ValueError):
        return default
    return value if value >= 1 else default


def redact_token(token: Optional[str]) -> str:
    token = (token or "").strip()
    if not token:
        return _MISSING_VALUE
    masked = sanitize_text(token)
    if isinstance(masked, str) and masked != token:
        return masked
    return mask_secret(token)


def redact_ids(values: Iterable[int]) -> str:
    uniq = sorted({int(v) for v in values if isinstance(v, int)})
    if not uniq:
        return _MISSING_VALUE
    if len(uniq) <= 3:
        return ", ".join(str(v) for v in uniq)
    return f"{len(uniq)} ids"


def redact_value(key: str, value: object) -> str:
    key_upper = str(key).upper()

    if (
        key_upper in _SECRET_KEYS
        or "TOKEN" in key_upper
        or "CREDENTIAL" in key_upper
        or "SERVICE_ACCOUNT" in key_upper
        or key_upper.endswith("_SECRET")
    ):
        return _redact_value(key, value)

    if key_upper in {
        "ADMIN_IDS",
        "ADMIN_ROLE_IDS",
        "STAFF_ROLE_IDS",
        "RECRUITER_ROLE_IDS",
        "LEAD_ROLE_IDS",
        "GUILD_IDS",
    }:
        try:
            iterable = list(value)  # type: ignore[arg-type]
        except TypeError:
            iterable = []
        return redact_ids(int(v) for v in iterable if isinstance(v, int))

    return _redact_value(key, value)
    "get_logging_channel_id",
