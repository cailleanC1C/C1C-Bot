"""Keep the fully decorated Live Arena organizer view within Discord limits."""

from __future__ import annotations

import logging

from modules.community.live_arena.hall_of_fame import OrganizerPlayerHistoryButton

log = logging.getLogger("c1c.community.live_arena.organizer_capacity_guard")
_installed = False
_MAX_VIEW_COMPONENTS = 25


def _closure_values(func) -> dict[str, object]:
    code = getattr(func, "__code__", None)
    closure = getattr(func, "__closure__", None)
    if code is None or not closure:
        return {}
    return {
        name: cell.cell_contents
        for name, cell in zip(code.co_freevars, closure)
    }


def _history_base_view(view):
    """Return the view wrapped by Hall of Fame's organizer-history decorator."""
    values = _closure_values(view)
    base_view = values.get("base_view")
    if not callable(base_view):
        return None
    return base_view


def _capacity_safe_history_view(manager, base_view):
    """Append Player History only when Discord's 25-component cap allows it."""

    def view(status=None):
        result = base_view(status)
        children = getattr(result, "children", ())
        add_item = getattr(result, "add_item", None)
        labels = {str(getattr(child, "label", "") or "") for child in children}
        if (
            callable(add_item)
            and "Player History" not in labels
            and len(children) < _MAX_VIEW_COMPONENTS
        ):
            add_item(OrganizerPlayerHistoryButton(manager))
        return result

    return view


def install() -> None:
    """Replace Hall of Fame's unsafe 26th-button wrapper after it is installed."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import qualification_panel

    original_install = qualification_panel.install_qualification

    def install_with_capacity_guard(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_organizer_capacity_guard_installed", False):
            return True

        # Hall of Fame is the final qualification installer before this guard. Its
        # wrapper closes over the pre-history view as `base_view`. Replacing only
        # that outer wrapper preserves every earlier tournament control while
        # preventing Player History from becoming an invalid 26th component.
        base_view = _history_base_view(manager.view)
        if base_view is None:
            log.error("Live Arena organizer capacity guard could not resolve history view")
            return False

        manager.view = _capacity_safe_history_view(manager, base_view)
        manager._organizer_capacity_guard_installed = True
        return True

    qualification_panel.install_qualification = install_with_capacity_guard
