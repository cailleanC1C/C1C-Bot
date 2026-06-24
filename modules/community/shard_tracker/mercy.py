"""Mercy math helpers for shard tracking embeds and commands."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict


@dataclass(frozen=True)
class MercyConfig:
    key: str
    label: str
    base_rate: float  # expressed as 0-1
    threshold: int
    increment: float  # expressed as 0-1


@dataclass(frozen=True)
class MercySnapshot:
    pulls_since: int
    chance: float  # expressed as 0-1
    threshold: int
    increment: float
    base_rate: float
    cap_at: int

    @property
    def percent(self) -> float:
        return self.chance * 100.0


MERCY_CONFIGS: Dict[str, MercyConfig] = {
    "ancient": MercyConfig(
        key="ancient",
        label="Ancient",
        base_rate=0.005,
        threshold=200,
        increment=0.05,
    ),
    "void": MercyConfig(
        key="void",
        label="Void",
        base_rate=0.005,
        threshold=200,
        increment=0.05,
    ),
    "sacred": MercyConfig(
        key="sacred",
        label="Sacred",
        base_rate=0.06,
        threshold=12,
        increment=0.02,
    ),
    "primal": MercyConfig(
        key="primal",
        label="Primal",
        base_rate=0.01,
        threshold=75,
        increment=0.01,
    ),
    "primal_mythic": MercyConfig(
        key="primal_mythic",
        label="Primal Mythical",
        base_rate=0.005,
        threshold=200,
        increment=0.10,
    ),
    "remnant_mythic": MercyConfig(
        key="remnant_mythic",
        label="Remnant Mythical",
        base_rate=0.025,
        threshold=24,
        increment=0.01,
    ),
}


def mercy_state(shard_type: str, pulls_since: int) -> MercySnapshot:
    config = MERCY_CONFIGS[shard_type]
    pulls = max(0, int(pulls_since))
    steps = max(0, pulls - config.threshold)
    chance = min(1.0, config.base_rate + steps * config.increment)
    cap_at = config.threshold
    if config.increment > 0:
        import math

        remaining = max(0.0, 1.0 - config.base_rate)
        steps_to_cap = math.ceil(remaining / config.increment)
        cap_at = config.threshold + max(0, steps_to_cap)
    return MercySnapshot(
        pulls_since=pulls,
        chance=chance,
        threshold=config.threshold,
        increment=config.increment,
        base_rate=config.base_rate,
        cap_at=cap_at,
    )


def format_percent(value: float) -> str:
    pct = max(0.0, min(value, 1.0)) * 100.0
    if pct >= 10:
        return f"{pct:.1f}%"
    return f"{pct:.2f}%"


__all__ = ["MERCY_CONFIGS", "MercyConfig", "MercySnapshot", "mercy_state", "format_percent"]
