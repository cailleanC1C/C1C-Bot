"""Keep pending result objection windows consistent with a round extension."""

from __future__ import annotations

from datetime import UTC, timedelta

from modules.community.live_arena import competition_operations
from modules.community.live_arena.competition import (
    MATCH_TERMINAL_STATUSES,
    _append_note,
    _parse_utc,
    _single_round,
)
from modules.community.live_arena.competition_operations import CompetitionOperationsService
from modules.community.live_arena.registration import RegistrationError, _locks, utc_iso
from modules.community.live_arena.result_views import schedule_match_finalization
from modules.community.live_arena.service import _text, load_config

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    async def extend_with_confirmation_windows(
        self,
        actor_id: str,
        round_id: str,
        new_deadline_at_utc: str,
        *,
        reason: str,
    ):
        reason = str(reason or "").strip()
        if not reason:
            raise RegistrationError("A round extension reason is required")
        new_deadline = _parse_utc(new_deadline_at_utc)
        if new_deadline is None:
            raise RegistrationError("A new round deadline is required")

        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        reschedule: list[tuple[str, str]] = []
        async with _locks[(self.sheet_id, tid)]:
            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            rounds = [dict(row) for row in old_rounds]
            matches = [dict(row) for row in old_matches]
            round_row = _single_round(rounds, tid, round_id)
            if _text(round_row.get("status")) not in competition_operations.ROUND_OPEN_STATUSES:
                raise RegistrationError("Only an open round can be extended")
            current = _parse_utc(_text(round_row.get("deadline_at_utc")))
            now_dt = self.clock().astimezone(UTC)
            if current is None or new_deadline <= current:
                raise RegistrationError("The new round deadline must be later than the current deadline")
            if new_deadline <= now_dt:
                raise RegistrationError("The new round deadline must be in the future")

            now = utc_iso(now_dt)
            deadline_text = utc_iso(new_deadline)
            round_row["deadline_at_utc"] = deadline_text
            round_row["notes"] = _append_note(
                _text(round_row.get("notes")),
                f"Organizer extension by {actor_id}: {reason} -> {deadline_text}",
            )
            touched = 0
            for match in matches:
                if (
                    _text(match.get("tournament_id")) != tid
                    or _text(match.get("round_id")) != str(round_id)
                    or _text(match.get("status")) in MATCH_TERMINAL_STATUSES
                ):
                    continue
                match["deadline_at_utc"] = deadline_text
                try:
                    count = int(_text(match.get("extension_count")) or 0)
                except ValueError:
                    count = 0
                match["extension_count"] = str(count + 1)
                touched += 1

                if _text(match.get("status")) != "pending_confirmation":
                    continue
                reported = _parse_utc(_text(match.get("reported_at_utc")))
                if reported is None:
                    continue
                confirm_due = min(reported + timedelta(hours=24), new_deadline)
                confirm_due_text = utc_iso(confirm_due)
                match["confirm_due_at_utc"] = confirm_due_text
                match_id = _text(match.get("match_id"))
                if match_id:
                    reschedule.append((match_id, confirm_due_text))

            await self.repository.persist_state(
                rounds,
                matches,
                previous_rounds=old_rounds,
                previous_matches=old_matches,
            )
            await self._audit(
                tid,
                str(actor_id),
                "round_extended",
                {
                    "round_id": str(round_id),
                    "new_deadline_at_utc": deadline_text,
                    "open_matches_extended": touched,
                    "pending_objection_windows_recalculated": len(reschedule),
                    "reason": reason,
                },
                now,
            )
            saved = dict(round_row)

        for match_id, due in reschedule:
            schedule_match_finalization(self.sheet_id, match_id, due)
        return saved

    CompetitionOperationsService.extend_round = extend_with_confirmation_windows
