"""Refresh the Captain's Table when a result mutation makes Q1 closable.

The result reconciliation path already updates the Victory Ledger, but the
organizer panel used to keep its cached Q1 status until some unrelated organizer
refresh happened.  That meant the ledger could say ``ready for organizer
closure`` while Captain's Table still had no Finish Round control.
"""

from __future__ import annotations

import logging

from shared.sheets.async_core import sheet_read_scope

from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.round_finish_refresh")
_installed = False
_CLOSABLE_Q1_STATUSES = {"ready_to_close", "correction_in_progress"}


async def _sync_competition_and_maybe_panel(manager, base_sync):
    """Run normal result reconciliation, then refresh Captain's Table if needed.

    One shared read scope lets the existing publisher refresh and the Q1 status
    refresh reuse identical Sheet reads.  We deliberately do not edit the
    organizer panel after every reported result; it is refreshed when the cached
    Q1 state changes or when Q1 is closable, which is the point at which the
    visible action set must change.
    """

    with sheet_read_scope():
        warnings = list(await base_sync(manager))
        previous_status = _text(
            getattr(manager, "_qualification_q1_status", "")
        ).lower()

        try:
            from modules.community.live_arena import qualification_panel

            snapshot = await qualification_panel.refresh_qualification_state(manager)
        except Exception as exc:
            log.warning(
                "Live Arena Q1 status refresh after result mutation failed • error=%s: %s",
                type(exc).__name__,
                exc,
            )
            return list(dict.fromkeys(warnings))

        current_status = _text(getattr(snapshot, "status", "")).lower()
        should_refresh_panel = (
            current_status != previous_status
            or current_status in _CLOSABLE_Q1_STATUSES
        )
        if not should_refresh_panel:
            return list(dict.fromkeys(warnings))

        try:
            result = await manager.sync()
            if getattr(result, "ok", True) is False:
                warnings.append("organizer panel")
        except Exception:
            log.exception(
                "Live Arena Captain's Table refresh after round-state change failed"
            )
            warnings.append("organizer panel")

        return list(dict.fromkeys(warnings))


def install() -> None:
    """Patch the already-installed post-result competition sync."""

    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import runtime_hooks

    original_sync = runtime_hooks._sync_manager_competition
    if getattr(original_sync, "_round_finish_refresh_installed", False):
        return

    async def sync_with_round_finish_refresh(manager):
        return await _sync_competition_and_maybe_panel(manager, original_sync)

    sync_with_round_finish_refresh._round_finish_refresh_installed = True
    runtime_hooks._sync_manager_competition = sync_with_round_finish_refresh
