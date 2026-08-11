"""Keep pending result objection windows consistent with a round extension."""

from __future__ import annotations

from datetime import timedelta

from modules.community.live_arena.competition import _parse_utc
from modules.community.live_arena.competition_operations import CompetitionOperationsService
from modules.community.live_arena.result_views import schedule_match_finalization
from modules.community.live_arena.service import _text, load_config

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    original = CompetitionOperationsService.extend_round

    async def extend_with_confirmation_windows(
        self,
        actor_id: str,
        round_id: str,
        new_deadline_at_utc: str,
        *,
        reason: str,
    ):
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        before_rounds = await self.repository.rounds()
        before_matches = await self.repository.matches()

        row = await original(
            self,
            actor_id,
            round_id,
            new_deadline_at_utc,
            reason=reason,
        )
        deadline = _parse_utc(_text(row.get("deadline_at_utc")))
        if deadline is None:
            return row

        current_matches = await self.repository.matches()
        updated = [dict(match) for match in current_matches]
        reschedule: list[tuple[str, str]] = []
        changed = False
        for match in updated:
            if (
                _text(match.get("tournament_id")) != tid
                or _text(match.get("round_id")) != str(round_id)
                or _text(match.get("status")) != "pending_confirmation"
            ):
                continue
            reported = _parse_utc(_text(match.get("reported_at_utc")))
            if reported is None:
                continue
            desired = min(reported + timedelta(hours=24), deadline)
            desired_text = desired.isoformat(timespec="seconds").replace("+00:00", "Z")
            if _text(match.get("confirm_due_at_utc")) == desired_text:
                continue
            match["confirm_due_at_utc"] = desired_text
            match_id = _text(match.get("match_id"))
            if match_id:
                reschedule.append((match_id, desired_text))
            changed = True

        if not changed:
            return row

        try:
            await self.repository.persist_matches(
                updated,
                previous_matches=current_matches,
            )
        except Exception as update_error:
            try:
                current_rounds = await self.repository.rounds()
                rollback_matches = await self.repository.matches()
                await self.repository.persist_state(
                    before_rounds,
                    before_matches,
                    previous_rounds=current_rounds,
                    previous_matches=rollback_matches,
                )
            except Exception as rollback_error:
                raise RuntimeError(
                    "Round extension saved but objection-window update failed, and rollback also failed: "
                    f"update={update_error!r}; rollback={rollback_error!r}"
                ) from rollback_error
            raise

        for match_id, due in reschedule:
            schedule_match_finalization(self.sheet_id, match_id, due)
        return row

    CompetitionOperationsService.extend_round = extend_with_confirmation_windows
