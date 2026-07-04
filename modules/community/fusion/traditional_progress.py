"""Shared traditional-fusion rare progress calculations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from shared.sheets import fusion as fusion_sheets

_RARE_REWARD_TYPES = {"rare", "rares"}


@dataclass(frozen=True, slots=True)
class TraditionalRareProgress:
    required: int
    available_sources: int
    acquired: int
    skipped: int
    missed: int
    to_go: int


def is_rare_reward(event: fusion_sheets.FusionEventRow) -> bool:
    return str(event.reward_type or "").strip().casefold() in _RARE_REWARD_TYPES


def _reward_count(event: fusion_sheets.FusionEventRow, *, include_bonus: bool = False) -> int:
    amount = max(0.0, float(event.reward_amount or 0))
    if include_bonus:
        amount += max(0.0, float(event.bonus or 0))
    return max(0, int(amount))


def calculate_traditional_rare_progress(
    *,
    target: fusion_sheets.FusionRow,
    events: Sequence[fusion_sheets.FusionEventRow],
    progress_by_event: Mapping[str, str] | None = None,
    effective_status_by_event: Mapping[str, str] | None = None,
    partial_by_event: Mapping[str, float] | None = None,
) -> TraditionalRareProgress:
    """Calculate required rare progress separately from available rare source count.

    Done and done_bonus rare rewards are acquired. Skipped and missed are tracked
    separately. In-progress rare rewards only count when an explicit partial amount
    is recorded.
    """

    required = max(0, int(target.needed))
    acquired = 0
    skipped = 0
    missed = 0
    available_sources = max(0, int(target.available))
    partials = partial_by_event or {}
    raw_statuses = progress_by_event or {}
    effective_statuses = effective_status_by_event or raw_statuses

    for event in events:
        if not is_rare_reward(event):
            continue
        status = str(effective_statuses.get(event.event_id, "not_started") or "").strip().casefold()
        if status == "done_bonus":
            acquired += _reward_count(event, include_bonus=True)
        elif status == "done":
            acquired += _reward_count(event)
        elif status == "skipped":
            skipped += _reward_count(event, include_bonus=True)
        elif status == "missed":
            missed += _reward_count(event, include_bonus=True)
        elif status == "in_progress":
            acquired += max(0, int(float(partials.get(event.event_id, 0) or 0)))

    acquired = min(acquired, required) if required else acquired
    skipped = min(skipped, required) if required else skipped
    missed = min(missed, required) if required else missed
    return TraditionalRareProgress(
        required=required,
        available_sources=available_sources,
        acquired=acquired,
        skipped=skipped,
        missed=missed,
        to_go=max(0, required - acquired),
    )


__all__ = ["TraditionalRareProgress", "calculate_traditional_rare_progress", "is_rare_reward"]
