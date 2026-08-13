"""Render the Captain's Table from the live lifecycle state at the final sync boundary.

This intentionally sits at OrganizerPanelManager.sync rather than another
qualification installer wrapper. Every feature decorator can still register its
persistent callbacks, while the message sent to Discord is reduced to the few
actions that make sense for the current tournament state.
"""

from __future__ import annotations

import logging

from shared.sheets.async_core import sheet_read_scope
from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.captains_table_render")
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

_CANONICAL_BY_VISIBLE = {friendly: original for original, friendly in _FRIENDLY_LABELS.items()}

_ROWS = {
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
    "Repair Tournament": 3,
}


def install() -> None:
    """Patch the concrete organizer sync boundary once."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import organizer_panel, simulation_ux_finalizer

    original_sync = organizer_panel.OrganizerPanelManager.sync

    async def sync_state_first(self):
        # Resolve lifecycle actions immediately before the base panel edits Discord.
        # Any earlier feature installer may have decorated self.view; we deliberately
        # canonicalize the fully-built view after all of those decorators have run.
        try:
            with sheet_read_scope():
                allowed = set(await simulation_ux_finalizer._allowed_panel_actions(self))
        except Exception:
            log.exception("Live Arena Captain's Table lifecycle resolution failed")
            allowed = set()

        current_view = self.view

        def visible_view(status=None):
            view = current_view(status)
            if status is None or not allowed:
                return view
            return _canonicalize_view(view, allowed)

        self.view = visible_view
        try:
            return await original_sync(self)
        finally:
            self.view = current_view

    organizer_panel.OrganizerPanelManager.sync = sync_state_first


def _canonicalize_view(view, allowed: set[str]):
    """Keep only lifecycle-relevant controls and apply human-facing labels/rows."""
    # Player History is useful, but it is not a current-stage action. Keeping it off
    # the live control panel avoids turning every stage back into a utility dashboard.
    visible_allowed = set(allowed)
    visible_allowed.discard("Player History")

    for item in list(getattr(view, "children", ())):
        label = _text(getattr(item, "label", ""))
        if not label:
            continue
        canonical = _CANONICAL_BY_VISIBLE.get(label, label)
        if canonical not in visible_allowed:
            view.remove_item(item)
            continue
        friendly = _FRIENDLY_LABELS.get(canonical, canonical)
        item.label = friendly
        if friendly in _ROWS:
            item.row = _ROWS[friendly]
    return view
