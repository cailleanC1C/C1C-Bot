"""Final post-result boundary for qualification tiebreak completion.

Normal result mutation reconciliation is qualification-round oriented: it repairs the
current qualification round and Victory Ledger. A qualification tiebreak is a real
MATCHES row, but it lives after Q3 and therefore needs one additional final pass.

Run that pass after every result mutation so a just-finalized tiebreak immediately:
- materializes its auditable qualification-order resolution;
- marks the tiebreak round resolved;
- refreshes Captain's Table to unlock Top 8 locking;
- converges any duplicate tiebreak thread without ever recreating a finalized match.

The thread guard owns the authoritative fresh MATCHES read, so this remains correct
even when the result callback is still inside an older ``sheet_read_scope``.
"""

from __future__ import annotations

import logging

from modules.community.live_arena import captains_table_control_center as control
from modules.community.live_arena import runtime_hooks
from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.tiebreak_completion_boundary")
_installed = False


async def _sync_tiebreak_after_result(manager) -> list[str]:
    """Refresh tiebreak state and Captain's Table from the result mutation boundary."""

    warnings: list[str] = []
    try:
        state = await control._ensure_tiebreak_flow(manager)
        await control._render_control_center(manager, state)
        if state.tiebreak_required and state.tiebreak_complete:
            log.info(
                "Live Arena qualification tiebreak completion reconciled • tournament=%s • resolved=%s",
                state.tournament_id,
                bool(state.tiebreak_resolved),
            )
    except Exception as exc:
        log.exception(
            "Live Arena qualification tiebreak post-result reconciliation failed • sheet=%s • error=%s: %s",
            _text(getattr(manager, "sheet_id", "")),
            type(exc).__name__,
            exc,
        )
        warnings.append("qualification tiebreak / Captain's Table")
    return warnings


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    original_sync_manager_competition = runtime_hooks._sync_manager_competition

    async def sync_manager_competition_with_tiebreak(manager):
        warnings = list(await original_sync_manager_competition(manager))
        warnings.extend(await _sync_tiebreak_after_result(manager))
        return list(dict.fromkeys(warnings))

    # Result views register a closure that resolves this module global at call time,
    # so patching the final function here affects report/confirm/dispute/timeout
    # reconciliation without stacking another UI callback wrapper.
    runtime_hooks._sync_manager_competition = sync_manager_competition_with_tiebreak
