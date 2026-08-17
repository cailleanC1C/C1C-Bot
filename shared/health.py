"""In-memory health component registry for readiness and diagnostics."""

from __future__ import annotations

import time
from typing import Dict, Mapping

__all__ = [
    "components_snapshot",
    "overall_ready",
    "required_components",
    "set_component",
]

_components: Dict[str, bool] = {}
_updated_at: Dict[str, float] = {}
_required_components = {"runtime", "discord"}


def required_components() -> frozenset[str]:
    """Return the set of component names required for readiness."""

    return frozenset(_required_components)


def set_component(name: str, ok: bool) -> None:
    """Record the health of a component and timestamp the update."""

    _components[name] = bool(ok)
    _updated_at[name] = time.time()


def _sheets_broker_snapshot() -> Mapping[str, float | bool] | None:
    """Return non-secret read-broker diagnostics without creating a hard dependency."""

    try:
        from shared.sheets.read_broker import broker

        raw = broker.snapshot()
    except Exception:
        return None

    logical_reads = int(raw.get("logical_reads", 0) or 0)
    cache_hits = int(raw.get("cache_hits", 0) or 0)
    stale_hits = int(raw.get("stale_hits", 0) or 0)
    coalesced = int(raw.get("coalesced_joins", 0) or 0)
    served_without_physical = cache_hits + stale_hits + coalesced
    hit_rate = (
        (served_without_physical / logical_reads) * 100.0 if logical_reads > 0 else 0.0
    )

    return {
        "ok": True,
        "ts": time.time(),
        "read_budget_rpm": int(raw.get("read_budget_rpm", 0) or 0),
        "rolling_physical_reads": int(raw.get("rolling_physical_reads", 0) or 0),
        "logical_reads": logical_reads,
        "physical_reads": int(raw.get("physical_reads", 0) or 0),
        "cache_hit_rate_pct": round(hit_rate, 2),
        "cache_hits": cache_hits,
        "stale_hits": stale_hits,
        "coalesced_joins": coalesced,
        "queued": int(raw.get("queued", 0) or 0),
        "inflight": int(raw.get("inflight", 0) or 0),
        "rate_limit_errors": int(raw.get("rate_limit_errors", 0) or 0),
        "retries": int(raw.get("retries", 0) or 0),
        "invalidations": int(raw.get("invalidations", 0) or 0),
    }


def components_snapshot(include_required: bool = True) -> dict[str, Mapping[str, float | bool]]:
    """Return a snapshot of component states with timestamps and broker diagnostics."""

    snapshot: dict[str, Mapping[str, float | bool]] = {
        key: {"ok": value, "ts": _updated_at.get(key, 0.0)} for key, value in _components.items()
    }
    if include_required:
        for name in _required_components:
            if name not in snapshot:
                snapshot[name] = {"ok": False, "ts": 0.0}

    broker_snapshot = _sheets_broker_snapshot()
    if broker_snapshot is not None:
        snapshot["sheets_read_broker"] = broker_snapshot
    return snapshot


def overall_ready() -> bool:
    """Return ``True`` when every required component is marked healthy."""

    return all(_components.get(name, False) for name in _required_components)
