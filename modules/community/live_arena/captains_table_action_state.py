"""Keep Captain's Table buttons aligned with the authoritative control-center state.

The quota-safe panel renderer caches an allowed-action set before the later
qualification-tiebreak reconciliation runs.  After the tiebreak is resolved that
cache can still describe the old ``ready_to_close`` state, so the control-center
embed says "Lock the Top 8" while the final view still renders "Finish Round".

This module fixes the existing final render boundary rather than adding another
OrganizerPanelManager.sync wrapper.  The control-center already carries the
fresh tournament state; use that same state to correct the action cache immediately
before its final Discord edit.
"""

from __future__ import annotations

from modules.community.live_arena import captains_table_control_center as control
from modules.community.live_arena import knockout
from modules.community.live_arena.service import _text

_installed = False

_PROGRESSION_ACTIONS = {
    "Close Current Round",
    "Freeze Top 8",
    "Record BO3 Tiebreak",
}


def _post_qualification_action(state: control.ControlState) -> str | None:
    """Return the one valid progression action after Qualification Round 3."""

    q3 = knockout._round_by_id(
        state.rounds, state.tournament_id, f"{state.tournament_id}-Q3"
    )
    if q3 is None or _text(q3.get("status")).lower() != "closed":
        return None

    # Once seeds exist there is nothing left to lock.  Normal knockout preview
    # reconciliation owns the next controls from this point onward.
    if knockout._seed_row(state.rounds, state.tournament_id) is not None:
        return ""

    if state.unsupported_tie:
        # Multi-player ties intentionally have no invented automatic mechanic.
        return ""
    if state.tiebreak_required and not state.tiebreak_complete:
        return "Record BO3 Tiebreak"
    return "Freeze Top 8"


def _apply_final_action_state(manager, state: control.ControlState) -> None:
    """Correct the cached final-view actions from the same state as the embed."""

    action = _post_qualification_action(state)
    if action is None:
        return

    allowed = set(getattr(manager, "_captains_table_allowed", None) or ())
    if not allowed:
        # Fail closed with the established no-read fallback rather than exposing
        # the full raw organizer view when the earlier lifecycle lookup failed.
        from modules.community.live_arena import captains_table_quota_safe

        allowed = set(captains_table_quota_safe._safe_panel_actions(manager, "active"))

    # A qualification tiebreak is not a normal closable round.  After Q3 is
    # closed exactly one progression action may be visible: play the tiebreak,
    # lock the Top 8, or none once Top 8 is already locked.
    allowed.difference_update(_PROGRESSION_ACTIONS)
    if action:
        allowed.add(action)
    manager._captains_table_allowed = allowed


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import full_set_scoring

    # The playable-tiebreak button is renamed after the full-set label map was
    # originally installed.  Teach the final filter its real user-facing name so
    # an unresolved tiebreak is not accidentally removed during pruning.
    full_set_scoring._FRIENDLY_LABELS["Record BO3 Tiebreak"] = "Open Tiebreak Match"
    full_set_scoring._ORIGINAL_LABELS["Open Tiebreak Match"] = "Record BO3 Tiebreak"
    full_set_scoring._FRIENDLY_ROWS["Open Tiebreak Match"] = 0

    original_render = control._render_control_center

    async def render_with_authoritative_actions(manager, state: control.ControlState):
        _apply_final_action_state(manager, state)
        return await original_render(manager, state)

    control._render_control_center = render_with_authoritative_actions
