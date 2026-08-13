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
        # With no trustworthy round-state read, do not expose draw mutations.
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
            tournament_lifecycle,
        )

        base_view = manager.view
        manager._captains_table_allowed = None

        def view(status=None):
            result = base_view(status)
            # status=None is the persistent callback-registration surface. Keep the
            # complete callback set registered even though the visible message is
            # compact.
            if status is None:
                return result
            allowed = getattr(manager, "_captains_table_allowed", None)
            if not allowed:
                allowed = _safe_panel_actions(manager, status)
            return full_set_scoring._finalize_visible_view(result, set(allowed))

        async def sync():
            # One read scope for lifecycle-state calculation plus the real panel
            # render. Repeated CONFIG/tournament/round reads inside this operation
            # are therefore reused by the Sheets scope instead of becoming new
            # physical requests.
            with sheet_read_scope():
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

                # Never fail open to the 25-control mega-view. If the lifecycle
                # lookup is throttled or otherwise unavailable, manager.view()
                # derives a small safe set from the already-known tournament
                # status passed by the lifecycle renderer.
                manager._captains_table_allowed = set(allowed or ()) or None

                # Call the real lifecycle sync directly. This bypasses the stacked
                # simulation/full-set sync wrappers that each performed their own
                # lifecycle lookup and caused duplicate Sheets reads.
                return await tournament_lifecycle._sync_organizer_panel(manager)

        manager.view = view
        manager.sync = sync
        return True

    qualification_panel.install_qualification = install_quota_safe
