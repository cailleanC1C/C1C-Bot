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


def components_snapshot(include_required: bool = True) -> dict[str, Mapping[str, float | bool]]:
    """Return a snapshot of component states with timestamps."""

    snapshot: dict[str, Mapping[str, float | bool]] = {
        key: {"ok": value, "ts": _updated_at.get(key, 0.0)} for key, value in _components.items()
    }
    if include_required:
        for name in _required_components:
            if name not in snapshot:
                snapshot[name] = {"ok": False, "ts": 0.0}
    return snapshot


def overall_ready() -> bool:
    """Return ``True`` when every required component is marked healthy."""

    return all(_components.get(name, False) for name in _required_components)
