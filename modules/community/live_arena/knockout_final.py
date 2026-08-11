"""Final-specific result confirmation rules for Live Arena PR 6B-3."""

from __future__ import annotations

from modules.community.live_arena.registration import _locks
from modules.community.live_arena.service import _text

_installed = False


def install() -> None:
    """Make Final reports organizer-reviewed instead of timeout-finalized."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import competition_resolution

    competition_resolution._REVIEWABLE_RESULT_STATUSES.add("organizer_review")
    service_cls = competition_resolution.CompetitionResolutionService
    original_report = service_cls.report_result

    async def report_result_with_final_confirmation(
        self,
        actor_id: str,
        match_id: str,
        score_a: int,
        score_b: int,
        *,
        screenshot_present: bool,
    ):
        updated = await original_report(
            self,
            actor_id,
            match_id,
            score_a,
            score_b,
            screenshot_present=screenshot_present,
        )
        tid = _text(updated.get("tournament_id"))
        round_id = _text(updated.get("round_id"))
        if not tid or not round_id:
            return updated

        old_rounds = await self.repository.rounds()
        round_row = next(
            (
                row
                for row in old_rounds
                if _text(row.get("tournament_id")) == tid
                and _text(row.get("round_id")) == round_id
            ),
            None,
        )
        if round_row is None or _text(round_row.get("round_stage")).lower() != "final":
            return updated

        async with _locks[(self.sheet_id, tid)]:
            old_matches = await self.repository.matches()
            matches = [dict(row) for row in old_matches]
            target = next(
                (
                    row
                    for row in matches
                    if _text(row.get("tournament_id")) == tid
                    and _text(row.get("match_id")) == str(match_id)
                ),
                None,
            )
            if target is None:
                return updated
            # Late Final reports are already organizer-reviewed. Timely Final reports
            # are deliberately converted to the same explicit organizer queue.
            if _text(target.get("status")) == "pending_confirmation":
                target["status"] = "organizer_review"
                target["confirm_due_at_utc"] = ""
                await self.repository.persist_matches(
                    matches,
                    previous_matches=old_matches,
                )
            return dict(target)

    service_cls.report_result = report_result_with_final_confirmation
