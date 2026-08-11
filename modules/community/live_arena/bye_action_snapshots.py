"""Keep organizer publish confirmations from counting a bye as a played matchup."""

from __future__ import annotations

from modules.community.live_arena import qualification, swiss
from modules.community.live_arena.bye_support import _is_bye
from modules.community.live_arena.qualification import QualificationSnapshot

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    q1_approve = qualification.QualificationService.approve_draw
    swiss_approve = swiss.SwissQualificationService.approve_preview

    async def q1_approve_playable_snapshot(self, actor_id: str):
        snapshot = await q1_approve(self, actor_id)
        return _playable(snapshot)

    async def swiss_approve_playable_snapshot(self, actor_id: str, round_number: int):
        snapshot = await swiss_approve(self, actor_id, round_number)
        return _playable(snapshot)

    qualification.QualificationService.approve_draw = q1_approve_playable_snapshot
    swiss.SwissQualificationService.approve_preview = swiss_approve_playable_snapshot


def _playable(snapshot: QualificationSnapshot) -> QualificationSnapshot:
    return QualificationSnapshot(
        snapshot.round_row,
        tuple(row for row in snapshot.matches if not _is_bye(row)),
    )
