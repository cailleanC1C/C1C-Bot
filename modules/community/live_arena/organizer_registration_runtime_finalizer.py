"""Finalize the Live Arena organizer view after all runtime decorators are installed."""

from __future__ import annotations

import logging

from modules.community.live_arena.hall_of_fame import OrganizerPlayerHistoryButton
from modules.community.live_arena.organizer_registration_hardening import (
    _prune_registration_controls,
)

log = logging.getLogger(
    "c1c.community.live_arena.organizer_registration_runtime_finalizer"
)
_installed = False


def _closure_values(func) -> dict[str, object]:
    code = getattr(func, "__code__", None)
    closure = getattr(func, "__closure__", None)
    if code is None or not closure:
        return {}
    return {
        name: cell.cell_contents
        for name, cell in zip(code.co_freevars, closure)
    }


def _unwrap_runtime_views(view):
    """Return the pre-history view under the final roster-lock wrapper.

    The organizer view is assembled through intentional decorator functions. The
    Hall of Fame layer currently adds the 26th organizer component, which prevents
    Discord persistent-view registration from completing. The roster-lock layer is
    installed after qualification setup, so unwrap those two known wrappers and
    rebuild their behavior in a safe order: construct <=25 components, prune by
    lifecycle state, then add Player History only when capacity remains.
    """
    outer = _closure_values(view)
    history_view = outer.get("base_view")
    if not callable(history_view):
        return None

    history = _closure_values(history_view)
    pre_history = history.get("base_view")
    if not callable(pre_history):
        return None

    module = str(getattr(history_view, "__module__", "") or "")
    if module != "modules.community.live_arena.hall_of_fame":
        return None
    return pre_history


def _finalize_manager_view(manager) -> bool:
    if getattr(manager, "_organizer_runtime_finalized", False):
        return True

    pre_history_view = _unwrap_runtime_views(manager.view)
    if pre_history_view is None:
        log.error(
            "Live Arena organizer runtime finalizer could not resolve decorator chain"
        )
        return False

    from modules.community.live_arena.qualification_lock import _cached_roster_locked

    def view(status=None):
        result = pre_history_view(status)

        # Preserve the qualification roster-lock behavior that normally wraps the
        # Hall of Fame view.
        if _cached_roster_locked(manager):
            for child in result.children:
                if getattr(child, "custom_id", None) == "live_arena:organizer:reopen":
                    child.disabled = True

        # Registration/Q1 panels must be phase-aware before optional controls are
        # appended. This also keeps the persistent registration view under Discord's
        # 25-component ceiling.
        result = _prune_registration_controls(result, manager, status)

        labels = {str(getattr(child, "label", "") or "") for child in result.children}
        if "Player History" not in labels and len(result.children) < 25:
            result.add_item(OrganizerPlayerHistoryButton(manager))
        return result

    manager.view = view
    manager._organizer_runtime_finalized = True
    return True


def install() -> None:
    """Finalize organizer construction after qualification roster-lock installation."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import qualification_lock

    original_install = qualification_lock.install_qualification_roster_lock

    def install_with_finalizer(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if not _finalize_manager_view(manager):
            return False
        return True

    qualification_lock.install_qualification_roster_lock = install_with_finalizer
