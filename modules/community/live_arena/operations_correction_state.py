"""Allow 6B-4 operational controls during a reopened correction replay."""

from __future__ import annotations

from modules.community.live_arena import competition_operations, withdrawal_atomic

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    correction = {"correction_in_progress"}
    competition_operations.ROUND_OPEN_STATUSES = (
        set(competition_operations.ROUND_OPEN_STATUSES) | correction
    )
    withdrawal_atomic.ROUND_OPEN_STATUSES = (
        set(withdrawal_atomic.ROUND_OPEN_STATUSES) | correction
    )
