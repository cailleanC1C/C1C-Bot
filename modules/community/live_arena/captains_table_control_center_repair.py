"""Small final guard for Captain's Table tiebreak state.

Keeps the playable tiebreak row compatible with the existing auditable resolution
format and makes the same sync immediately recognize a newly completed tiebreak.
"""

from __future__ import annotations

import json

from modules.community.live_arena import captains_table_control_center as control
from modules.community.live_arena.competition import MATCH_TERMINAL_STATUSES
from modules.community.live_arena.service import _text

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    def tiebreak_complete(self) -> bool:
        if self.tiebreak_resolved:
            return True
        if not self.tiebreak_required or self.unsupported_tie:
            return False
        return bool(self.tiebreak_matches) and all(
            _text(row.get("status")) in MATCH_TERMINAL_STATUSES
            and _text(row.get("final_winner_discord_user_id"))
            for row in self.tiebreak_matches
        )

    control.ControlState.tiebreak_complete = property(tiebreak_complete)

    original_new_round = control._new_round

    def new_round_with_resolution_payload(tid: str, now: str):
        row = original_new_round(tid, now)
        row["notes"] = json.dumps(
            {"resolutions": []}, sort_keys=True, separators=(",", ":")
        )
        return row

    control._new_round = new_round_with_resolution_payload

    original_ensure_flow = control._ensure_tiebreak_flow

    async def ensure_flow_with_same_pass_resolution(manager):
        state = await original_ensure_flow(manager)
        if state.tiebreak_required and state.tiebreak_complete and not state.tiebreak_resolved:
            state.tiebreak_resolved = True
            manager._qualification_tiebreak_required = False
        return state

    control._ensure_tiebreak_flow = ensure_flow_with_same_pass_resolution
