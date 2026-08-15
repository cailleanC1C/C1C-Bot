"""Final Captain's Table render path with quota-safe lifecycle pruning.

This installer runs after the older UX wrappers. It deliberately bypasses their
stacked sync wrappers so one organizer refresh does not perform the same Sheets
state lookup several times before rendering.
"""

from __future__ import annotations

import logging

from shared.sheets.async_core import sheet_read_scope

from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.captains_table_quota_safe")
_installed = False


_SAFE_MAINTENANCE = {"Repair Discord State"}
_SAFE_ROSTER = {"View Roster"}


def _safe_panel_actions(manager, status: str | None) -> set[str]:
    """Return a small fail-closed action set without any Sheet reads."""
    state = _text(status).lower()
    q1_status = _text(getattr(manager, "_qualification_q1_status", "")).lower()

    if state == "draft":
        return {"Open Registration"} | _SAFE_ROSTER | _SAFE_MAINTENANCE
    if state == "signup_open":
        return {"Close Registration"} | _SAFE_ROSTER | _SAFE_MAINTENANCE
    if state == "signup_closed":
        return {"Reopen Registration"} | _SAFE_ROSTER | _SAFE_MAINTENANCE
    if state == "completed":
        return {
            "Archive Tournament",
            "Create Next Tournament",
            "View Standings",
        } | _SAFE_MAINTENANCE
    if state == "archived":
        return {"Create Next Tournament"} | _SAFE_MAINTENANCE
    if state == "active":
        actions = {
            "View Standings",
            "Review Result Issues",
            "Competition Ops",
        } | _SAFE_ROSTER | _SAFE_MAINTENANCE
        if q1_status in {"ready_to_close", "correction_in_progress"}:
            actions.add("Close Current Round")
        return actions
    return _SAFE_ROSTER | _SAFE_MAINTENANCE


def install() -> None:
    """Install one final, quota-safe organizer render path."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import qualification_panel

    original_install = qualification_panel.install_qualification

    def install_quota_safe(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_captains_table_quota_safe_installed", False):
            return True
        manager._captains_table_quota_safe_installed = True

        from modules.community.live_arena import (
            full_set_scoring,
            simulation_ux_finalizer,
            swiss_panel,
            swiss_runtime,
            tournament_lifecycle,
        )

        base_view = manager.view
        manager._captains_table_allowed = None
        manager._captains_table_rendering = False
        manager._captains_table_stage_reconciling = False

        def view(status=None):
            result = base_view(status)
            # Keep direct/internal construction and the persistent callback
            # registration surface unchanged. Pruning is only for the concrete
            # Discord message render inside this sync path.
            if status is None or not getattr(manager, "_captains_table_rendering", False):
                return result

            allowed = getattr(manager, "_captains_table_allowed", None)
            if not allowed:
                allowed = _safe_panel_actions(manager, status)
            result = full_set_scoring._finalize_visible_view(result, set(allowed))

            preview_round = getattr(manager, "_captains_table_swiss_preview_round", None)
            preview_status = _text(
                getattr(manager, "_captains_table_swiss_preview_status", "")
            ).lower()
            if (
                _text(status).lower() == "active"
                and preview_round in {2, 3}
                and preview_status in {"preview", "approved"}
            ):
                # This is intentionally after every older pruning/label wrapper.
                # Some decorated views rename the Swiss controls before this final
                # pass, causing the generic-label filter to delete them on a second
                # pass. Re-add the real callbacks only at the final visible boundary.
                custom_ids = {
                    _text(getattr(item, "custom_id", ""))
                    for item in getattr(result, "children", ())
                }
                if "live_arena:organizer:swiss:regenerate" not in custom_ids:
                    result.add_item(
                        swiss_panel.SwissActionButton(
                            manager,
                            f"Redo Qualification Round {preview_round}",
                            "regenerate",
                            disabled=False,
                        )
                    )
                if "live_arena:organizer:swiss:publish" not in custom_ids:
                    result.add_item(
                        swiss_panel.SwissActionButton(
                            manager,
                            f"Publish Qualification Round {preview_round}",
                            "publish",
                            disabled=False,
                        )
                    )
            return result

        async def sync():
            with sheet_read_scope():
                # This is the real startup/live render boundary. Reconcile the
                # persisted Q2/Q3 preview here before deciding which controls are
                # visible, rather than relying on an older manager.sync wrapper
                # that this quota-safe path intentionally bypasses.
                manager._captains_table_stage_reconciling = True
                try:
                    await swiss_runtime._reconcile_preview(manager)
                except Exception as exc:
                    log.warning(
                        "Live Arena Swiss stage reconciliation failed at final Captain's Table render • error=%s: %s",
                        type(exc).__name__,
                        exc,
                    )
                finally:
                    manager._captains_table_stage_reconciling = False

                allowed = None
                try:
                    allowed = set(
                        await simulation_ux_finalizer._allowed_panel_actions(manager)
                    )
                except Exception as exc:
                    log.warning(
                        "Live Arena Captain's Table lifecycle lookup failed; using safe compact fallback • error=%s: %s",
                        type(exc).__name__,
                        exc,
                    )

                # If reconciliation just established a Q2/Q3 preview, trust that
                # stage cache over a lifecycle action lookup that may have reused
                # pre-write rows from the surrounding startup read scope.
                preview_round = getattr(manager, "_captains_table_swiss_preview_round", None)
                preview_status = _text(
                    getattr(manager, "_captains_table_swiss_preview_status", "")
                ).lower()
                if preview_round in {2, 3} and preview_status in {"preview", "approved"}:
                    allowed = set(_safe_panel_actions(manager, "active"))

                manager._captains_table_allowed = set(allowed or ()) or None
                manager._captains_table_rendering = True
                try:
                    # Bypass the stacked simulation/full-set sync wrappers that
                    # each performed their own lifecycle lookup. The real lifecycle
                    # renderer now owns the single visible message edit.
                    return await tournament_lifecycle._sync_organizer_panel(manager)
                finally:
                    manager._captains_table_rendering = False

        manager.view = view
        manager.sync = sync
        return True

    qualification_panel.install_qualification = install_quota_safe
