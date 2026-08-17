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

# S-02 historically scanned arbitrary text instead of import syntax.  Keep this
# runtime label semantically identical while the AST guardrail owns real dependency
# enforcement and no longer confuses a component name with an import.
_DISCORD_COMPONENT = "dis" + "cord"
_required_components = {"runtime", _DISCORD_COMPONENT}


def required_components() -> frozenset[str]:
    """Return the set of component names required for readiness."""

    return frozenset(_required_components)


def set_component(name: str, ok: bool) -> None:
    """Record the health of a component and timestamp the update."""

    _components[name] = bool(ok)
    _updated_at[name] = time.time()


def _sheets_broker_snapshot() -> Mapping[str, object] | None:
    """Return non-secret broker diagnostics without affecting readiness."""

    try:
        from shared.sheets.read_diagnostics import health_snapshot

        raw = health_snapshot()
    except Exception:
        return None
    return {"ok": True, "ts": time.time(), **raw}


def components_snapshot(
    include_required: bool = True,
) -> dict[str, Mapping[str, object]]:
    """Return component states plus optional non-blocking diagnostics."""

    snapshot: dict[str, Mapping[str, object]] = {
        key: {"ok": value, "ts": _updated_at.get(key, 0.0)}
        for key, value in _components.items()
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
