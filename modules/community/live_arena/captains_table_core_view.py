"""Render the Captain's Table from current tournament state at the core panel boundary."""

from __future__ import annotations

import logging
from types import MethodType

from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.captains_table_core_view")
_installed = False

_FRIENDLY_LABELS = {
    "Open Registration": "Open Signups",
    "Close Registration": "Close Signups",
    "Reopen Registration": "Reopen Signups",
    "View Roster": "View Players",
    "Reconcile Roles": "Fix Player Roles",
    "Complete Tournament": "Finish Tournament",
    "Archive Tournament": "Archive Tournament",
    "Generate Q1 Draw": "Create Round 1 Matchups",
    "Approve Draw": "Publish Round 1",
    "Regenerate Draw": "Redo Matchups",
    "Swap Players": "Swap Opponents",
    "Close Current Round": "Finish Round",
    "Review Result Issues": "Review Match Issues",
    "Reopen Closed Round": "Reopen Round",
    "Repair Discord State": "Repair Tournament",
    "View Standings": "View Standings",
    "Preview Next Swiss": "Preview Next Round",
    "Regenerate Swiss Preview": "Redo Next Round",
    "Approve & Publish Swiss": "Publish Next Round",
    "Repair Swiss Conflict": "Fix Matchup Conflict",
    "Freeze Top 8": "Lock Top 8",
    "Approve & Open Knockout": "Start Knockout Stage",
    "Record BO3 Tiebreak": "Record Tiebreak",
    "Competition Ops": "Organizer Actions",
    "Create Next Tournament": "Create New Tournament",
    "Player History": "Player History",
}

_ROW_BY_LABEL = {
    "Open Signups": 0,
    "Close Signups": 0,
    "Reopen Signups": 0,
    "Create Round 1 Matchups": 0,
    "Publish Round 1": 0,
    "Redo Matchups": 0,
    "Swap Opponents": 0,
    "Finish Round": 0,
    "View Standings": 0,
    "Preview Next Round": 0,
    "Redo Next Round": 0,
    "Publish Next Round": 0,
    "Lock Top 8": 0,
    "Start Knockout Stage": 0,
    "Finish Tournament": 0,
    "Archive Tournament": 0,
    "Create New Tournament": 0,
    "Review Match Issues": 1,
    "Organizer Actions": 1,
    "Fix Matchup Conflict": 1,
    "Record Tiebreak": 1,
    "Reopen Round": 1,
    "View Players": 2,
    "Fix Player Roles": 2,
    "Player History": 2,
    "Repair Tournament": 3,
}


def install() -> None:
    """Patch the actual organizer-panel sync boundary, not a qualification installer wrapper."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena.organizer_panel import OrganizerPanelManager

    original_sync = OrganizerPanelManager.sync

    async def state_first_sync(self):
        current_view = self.view
        try:
            allowed = await _allowed_actions(self)
        except Exception:
            log.exception("Live Arena Captain's Table state resolution failed")
            allowed = None

        if allowed:
            def visible_view(_self, status=None):
                result = current_view(status)
                return _prune_and_relabel(result, allowed)

            self.view = MethodType(visible_view, self)
        try:
            return await original_sync(self)
        finally:
            self.view = current_view

    OrganizerPanelManager.sync = state_first_sync


async def _allowed_actions(manager) -> set[str]:
    """Use the canonical lifecycle resolver already used by the tournament runtime."""
    from modules.community.live_arena.simulation_ux_finalizer import _allowed_panel_actions

    return set(await _allowed_panel_actions(manager))


def _prune_and_relabel(view, allowed: set[str]):
    """Keep only lifecycle-relevant controls and make their labels organizer-friendly."""
    for item in list(getattr(view, "children", ())):
        label = _text(getattr(item, "label", ""))
        if label and label not in allowed:
            view.remove_item(item)
            continue
        friendly = _FRIENDLY_LABELS.get(label)
        if friendly:
            item.label = friendly
            item.row = _ROW_BY_LABEL.get(friendly)
    return view
