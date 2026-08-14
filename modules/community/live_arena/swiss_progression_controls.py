"""Keep Captain's Table progression controls visible for Swiss Q2/Q3.

This is intentionally stage-driven, not round-specific: one cache covers both
qualification Swiss rounds and is updated whenever the organizer preview is
created, refreshed, or retired. The quota-safe Captain's Table fallback can
therefore still expose the correct next action even if a lifecycle Sheet read
is temporarily rate-limited.
"""

from __future__ import annotations

import logging

from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.swiss_progression_controls")
_installed = False


def _cache_preview_state(manager, snapshot) -> bool:
    row = getattr(snapshot, "round_row", None)
    if row is None:
        return False
    try:
        number = int(_text(row.get("round_number")) or 0)
    except ValueError:
        return False
    if number not in {2, 3}:
        return False
    status = _text(row.get("status")).lower()
    if status not in {"preview", "approved"}:
        return False
    old = (
        getattr(manager, "_captains_table_swiss_preview_round", None),
        _text(getattr(manager, "_captains_table_swiss_preview_status", "")).lower(),
    )
    manager._captains_table_swiss_preview_round = number
    manager._captains_table_swiss_preview_status = status
    return old != (number, status)


def _clear_preview_state(manager, number: int) -> bool:
    current = getattr(manager, "_captains_table_swiss_preview_round", None)
    if current != number:
        return False
    manager._captains_table_swiss_preview_round = None
    manager._captains_table_swiss_preview_status = ""
    return True


def _swiss_fallback_actions(manager, status: str | None, base_actions: set[str]) -> set[str]:
    """Expose safe Q2/Q3 progression even when the lifecycle lookup hits quota."""
    if _text(status).lower() != "active":
        return base_actions
    number = getattr(manager, "_captains_table_swiss_preview_round", None)
    preview_status = _text(
        getattr(manager, "_captains_table_swiss_preview_status", "")
    ).lower()
    if number not in {2, 3} or preview_status not in {"preview", "approved"}:
        return base_actions
    return {
        "View Standings",
        "Regenerate Swiss Preview",
        "Approve & Publish Swiss",
        "Repair Swiss Conflict",
        "Reopen Closed Round",
        "View Roster",
        "Repair Discord State",
    }


def _apply_dynamic_labels(view, manager):
    number = getattr(manager, "_captains_table_swiss_preview_round", None)
    if number not in {2, 3}:
        return view
    replacements = {
        "Redo Next Round": f"Redo Qualification Round {number}",
        "Publish Next Round": f"Publish Qualification Round {number}",
        "Preview Next Round": f"Preview Qualification Round {number}",
    }
    for item in getattr(view, "children", ()):
        label = _text(getattr(item, "label", ""))
        if label in replacements:
            item.label = replacements[label]
    return view


async def _refresh_previous_closed_overview(manager, preview_number: int) -> None:
    """Best-effort one-shot rerender of the just-finished qualification round."""
    try:
        if preview_number == 2:
            from modules.community.live_arena import qualification_panel

            service = qualification_panel.QualificationService(manager.sheet_id)
            await service.initialize()
            snapshot = await service.snapshot()
            if snapshot.round_row is None or _text(snapshot.status).lower() != "closed":
                return
            await qualification_panel.QualificationPublisher(manager.bot, service).reconcile()
            return

        if preview_number == 3:
            from modules.community.live_arena.swiss import SwissQualificationService
            from modules.community.live_arena.swiss_panel import SwissPublisher

            service = SwissQualificationService(manager.sheet_id)
            await service.initialize()
            snapshot = await service.snapshot(2)
            if snapshot.round_row is None or _text(snapshot.status).lower() != "closed":
                return
            await SwissPublisher(manager.bot, service).reconcile(snapshot)
    except Exception:
        log.exception("Live Arena previous qualification overview refresh failed")


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import (
        captains_table_quota_safe,
        full_set_scoring,
        swiss_runtime,
    )

    original_safe = captains_table_quota_safe._safe_panel_actions

    def safe_with_swiss(manager, status):
        return _swiss_fallback_actions(manager, status, set(original_safe(manager, status)))

    captains_table_quota_safe._safe_panel_actions = safe_with_swiss

    original_finalize = full_set_scoring._finalize_visible_view

    def finalize_with_swiss_labels(view, allowed):
        result = original_finalize(view, allowed)
        manager = None
        for item in getattr(result, "children", ()):
            manager = getattr(item, "manager", None)
            if manager is not None:
                break
        return _apply_dynamic_labels(result, manager) if manager is not None else result

    full_set_scoring._finalize_visible_view = finalize_with_swiss_labels

    original_sync_preview = swiss_runtime._sync_preview_message

    async def sync_preview_and_panel(manager, service, snapshot):
        changed = _cache_preview_state(manager, snapshot)
        await original_sync_preview(manager, service, snapshot)
        if not changed:
            return
        number = getattr(manager, "_captains_table_swiss_preview_round", None)
        # Re-render the round that just finished with the current presentation
        # layer, then expose the Q2/Q3 progression controls immediately.
        if number in {2, 3}:
            await _refresh_previous_closed_overview(manager, number)
        try:
            await manager.sync()
        except Exception:
            log.exception("Live Arena Captain's Table refresh after Swiss preview failed")

    swiss_runtime._sync_preview_message = sync_preview_and_panel

    original_retire = swiss_runtime._retire_preview_message

    async def retire_preview_and_clear(manager, service, number: int):
        await original_retire(manager, service, number)
        _clear_preview_state(manager, number)

    swiss_runtime._retire_preview_message = retire_preview_and_clear
