from __future__ import annotations

# config/runtime.py
import logging
import os
from typing import Iterable, List, Optional


def get_port(default: int = 10000) -> int:
    """
    Returns the port for the aiohttp health server.
    Render provides $PORT; locally we fall back to 10000.
    """
    try:
        return int(os.getenv("PORT", str(default)))
    except ValueError:
        return default


def get_env_name(default: str = "dev") -> str:
    return os.getenv("ENV_NAME", default)


def get_bot_name(default: str = "C1C-Recruitment") -> str:
    return os.getenv("BOT_NAME", default)


def get_admin_ids() -> List[int]:
    """
    ADMIN_IDS can be:
      - "123,456"
      - " [ 123  ,  456 ] "
      - "123"
    Non-ints are ignored.
    """
    raw = os.getenv("ADMIN_IDS", "")
    # replace common separators with commas, then split
    for ch in ["[", "]", " ", ";", "|"]:
        raw = raw.replace(ch, ",")
    parts = [p for p in raw.split(",") if p.strip()]
    ids: List[int] = []
    for p in parts:
        try:
            ids.append(int(p))
        except ValueError:
            pass
    return ids


def _coerce_int(value: Optional[str], fallback: int) -> int:
    try:
        if value is None:
            raise TypeError
        return int(value)
    except (TypeError, ValueError):
        return fallback


def get_watchdog_check_sec(
    default_prod: int = 360,
    default_nonprod: int = 60,
) -> int:
    """
    Interval (seconds) between watchdog heartbeat checks.

    Priority order:
        1. WATCHDOG_CHECK_SEC (primary)
        2. KEEPALIVE_INTERVAL_SEC (deprecated compatibility shim)
        3. Defaults: prod-like envs → 360s, non-prod envs → 60s
    """

    env = (get_env_name() or "").lower()
    fallback = default_nonprod if env in {"dev", "development", "test", "qa", "stage"} else default_prod

    override = os.getenv("WATCHDOG_CHECK_SEC")
    if override is not None:
        return _coerce_int(override, fallback)

    legacy = os.getenv("KEEPALIVE_INTERVAL_SEC")
    if legacy is not None:
        try:
            value = int(legacy)
        except (TypeError, ValueError):
            pass
        else:
            logging.getLogger("c1c.config").warning(
                "KEEPALIVE_INTERVAL_SEC is deprecated; use WATCHDOG_CHECK_SEC instead. Using legacy value %ss.",
                value,
            )
            return value

    return fallback


def get_watchdog_stall_sec(default: Optional[int] = None) -> int:
    """
    Returns the watchdog stall threshold.

    If WATCHDOG_STALL_SEC is unset we derive it from the keepalive cadence:
        stall = keepalive * 3 + 30 (matches the legacy watchdog heuristics)
    """

    override = os.getenv("WATCHDOG_STALL_SEC")
    if override is not None:
        fallback = default if default is not None else get_watchdog_check_sec() * 3 + 30
        return _coerce_int(override, fallback)

    keepalive = get_watchdog_check_sec()
    derived = keepalive * 3 + 30
    if default is not None:
        return derived if derived else default
    return derived


def get_watchdog_disconnect_grace_sec(default: Optional[int] = None) -> int:
    """Grace window (seconds) while disconnected before forcing a restart."""

    override = os.getenv("WATCHDOG_DISCONNECT_GRACE_SEC")
    if override is not None:
        fallback = default if default is not None else get_watchdog_stall_sec()
        return _coerce_int(override, fallback)

    if default is not None:
        return default
    return get_watchdog_stall_sec()


def get_command_prefix(default: str = "!") -> str:
    """Return the fixed bang prefix for command routing."""

    return "!"
