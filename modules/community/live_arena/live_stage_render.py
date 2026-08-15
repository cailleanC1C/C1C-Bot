"""Final live-state reconciliation for the organizer and Swiss preview surfaces.

This installer runs last. It does not add another lifecycle model; it makes the
existing final Captain's Table sync hydrate the current Swiss stage first, so a
restart/deploy can render an already-existing Q2/Q3 preview correctly.
"""

from __future__ import annotations

import logging

from shared.sheets.async_core import sheet_read_scope

from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.live_stage_render")
_installed = False


def _preview_signature(snapshot) -> tuple[str, str, str]:
    row = getattr(snapshot, "round_row", None) or {}
    return (
        _text(row.get("round_id")),
        _text(row.get("status")).lower(),
        _text(row.get("generated_at_utc")) or _text(row.get("approved_at_utc")),
    )


async def _current_swiss_preview(manager):
    """Return the current Q2/Q3 preview/approval from Sheet truth, if one exists."""
    from modules.community.live_arena.swiss import SwissQualificationService

    service = SwissQualificationService(manager.sheet_id)
    await service.initialize()
    for number in (3, 2):
        snapshot = await service.snapshot(number)
        if snapshot.round_row is None:
            continue
        if _text(snapshot.status).lower() in {"preview", "approved"}:
            return service, snapshot, number
    return service, None, None


async def _hydrate_live_swiss_stage(manager) -> None:
    """Hydrate preview cache and existing Discord messages before panel rendering."""
    from modules.community.live_arena import swiss_progression_controls, swiss_runtime

    service, snapshot, number = await _current_swiss_preview(manager)
    if snapshot is None or number is None:
        for candidate in (2, 3):
            swiss_progression_controls._clear_preview_state(manager, candidate)
        manager._live_stage_render_signature = None
        return

    # Seed the cache before calling the existing preview reconciler. The PR1116
    # wrapper therefore sees an already-known state and does not recurse into
    # manager.sync().
    swiss_progression_controls._cache_preview_state(manager, snapshot)
    signature = _preview_signature(snapshot)
    if signature == getattr(manager, "_live_stage_render_signature", None):
        return

    await swiss_runtime._sync_preview_message(manager, service, snapshot)
    await swiss_progression_controls._refresh_previous_closed_overview(manager, number)
    manager._live_stage_render_signature = signature


def install() -> None:
    """Wrap the final installed organizer sync after all older UX installers."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import qualification_panel

    original_install = qualification_panel.install_qualification

    def install_final_live_stage(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_live_stage_render_installed", False):
            return True
        manager._live_stage_render_installed = True

        base_sync = manager.sync

        async def sync_with_live_stage():
            # The final visible Captain's Table render happens in base_sync. Load
            # the Q2/Q3 stage immediately before that render so both the normal
            # lifecycle path and quota-safe fallback see the same stage.
            try:
                with sheet_read_scope():
                    await _hydrate_live_swiss_stage(manager)
            except Exception:
                log.exception(
                    "Live Arena live-stage hydration failed before organizer render"
                )
            return await base_sync()

        manager.sync = sync_with_live_stage
        return True

    qualification_panel.install_qualification = install_final_live_stage
