"""Scheduling escalation, round extensions, withdrawals, and forfeits for Live Arena PR 6B-4."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from modules.community.live_arena.competition import (
    MATCH_OPEN_STATUSES,
    MATCH_TERMINAL_STATUSES,
    ROUND_OPEN_STATUSES,
    _append_note,
    _mark_round_ready_if_complete,
    _parse_utc,
    _single_match,
    _single_round,
)
from modules.community.live_arena.qualification import QualificationRepository
from modules.community.live_arena.registration import RegistrationError, _locks, utc_iso
from modules.community.live_arena.repository import LiveArenaRepository
from modules.community.live_arena.service import _text, load_config

_SCHEDULING_GRACE = timedelta(hours=24)


class CompetitionOperationsService:
    """Competition-side operational mutations that do not need new Sheet schema."""

    def __init__(
        self,
        sheet_id: str,
        registration_repository=None,
        qualification_repository=None,
        clock=None,
    ) -> None:
        self.sheet_id = sheet_id
        self.registration_repository = registration_repository or LiveArenaRepository(sheet_id)
        self.repository = qualification_repository or QualificationRepository(sheet_id)
        self.clock = clock or (lambda: datetime.now(UTC))

    async def initialize(self) -> None:
        await self.registration_repository.initialize()
        await self.repository.initialize()

    async def report_scheduling_problem(self, actor_id: str, match_id: str) -> dict[str, object]:
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            rounds = [dict(row) for row in old_rounds]
            matches = [dict(row) for row in old_matches]
            match = _single_match(matches, tid, match_id)
            round_row = _single_round(rounds, tid, _text(match["round_id"]))
            if _text(round_row.get("status")) not in ROUND_OPEN_STATUSES:
                raise RegistrationError("This round is not open for scheduling escalation")
            if _text(match.get("status")) not in MATCH_OPEN_STATUSES:
                raise RegistrationError("This matchup is not open for scheduling escalation")
            actor = str(actor_id)
            players = {
                _text(match.get("player_a_discord_user_id")),
                _text(match.get("player_b_discord_user_id")),
            }
            if actor not in players:
                raise RegistrationError("Only a player in this matchup can report a scheduling problem")
            if _text(match.get("scheduling_problem_reported_at_utc")):
                raise RegistrationError("A scheduling problem has already been reported for this matchup")

            now = utc_iso(self.clock().astimezone(UTC))
            match.update(
                scheduling_problem_reported_by_discord_user_id=actor,
                scheduling_problem_reported_at_utc=now,
            )
            conflict_note = _text(match.get("scheduling_conflict_notes"))
            match["scheduling_conflict_notes"] = _append_note(
                conflict_note,
                f"Scheduling problem reported at {now}; organizer review becomes due after 24 hours",
            )
            await self.repository.persist_matches(matches, previous_matches=old_matches)
            await self._audit(
                tid,
                actor,
                "match_scheduling_problem_reported",
                {"match_id": match_id, "review_due_at_utc": utc_iso(self.clock().astimezone(UTC) + _SCHEDULING_GRACE)},
                now,
            )
            return dict(match)

    async def scheduling_review_queue(self) -> list[dict[str, object]]:
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        now = self.clock().astimezone(UTC)
        matches = await self.repository.matches()
        result = []
        for row in matches:
            if _text(row.get("tournament_id")) != tid:
                continue
            if _text(row.get("status")) in MATCH_TERMINAL_STATUSES:
                continue
            reported = _parse_utc(_text(row.get("scheduling_problem_reported_at_utc")))
            if reported is None:
                continue
            if now >= reported + _SCHEDULING_GRACE:
                result.append(dict(row))
        return result

    async def extend_round(
        self,
        actor_id: str,
        round_id: str,
        new_deadline_at_utc: str,
        *,
        reason: str,
    ) -> dict[str, object]:
        reason = str(reason or "").strip()
        if not reason:
            raise RegistrationError("A round extension reason is required")
        new_deadline = _parse_utc(new_deadline_at_utc)
        if new_deadline is None:
            raise RegistrationError("A new round deadline is required")
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            rounds = [dict(row) for row in old_rounds]
            matches = [dict(row) for row in old_matches]
            round_row = _single_round(rounds, tid, round_id)
            if _text(round_row.get("status")) not in ROUND_OPEN_STATUSES:
                raise RegistrationError("Only an open round can be extended")
            current = _parse_utc(_text(round_row.get("deadline_at_utc")))
            if current is None or new_deadline <= current:
                raise RegistrationError("The new round deadline must be later than the current deadline")
            if new_deadline <= self.clock().astimezone(UTC):
                raise RegistrationError("The new round deadline must be in the future")

            now = utc_iso(self.clock().astimezone(UTC))
            deadline_text = utc_iso(new_deadline)
            round_row["deadline_at_utc"] = deadline_text
            round_row["notes"] = _append_note(
                _text(round_row.get("notes")),
                f"Organizer extension by {actor_id}: {reason} -> {deadline_text}",
            )
            touched = 0
            for match in matches:
                if _text(match.get("tournament_id")) != tid or _text(match.get("round_id")) != round_id:
                    continue
                if _text(match.get("status")) in MATCH_TERMINAL_STATUSES:
                    continue
                match["deadline_at_utc"] = deadline_text
                try:
                    count = int(_text(match.get("extension_count")) or 0)
                except ValueError:
                    count = 0
                match["extension_count"] = str(count + 1)
                touched += 1
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
                    "round_id": round_id,
                    "new_deadline_at_utc": deadline_text,
                    "open_matches_extended": touched,
                    "reason": reason,
                },
                now,
            )
            return dict(round_row)

    async def impose_mandatory_time(
        self,
        actor_id: str,
        match_id: str,
        mandatory_at_utc: str,
        *,
        reason: str,
    ) -> dict[str, object]:
        reason = str(reason or "").strip()
        if not reason:
            raise RegistrationError("A mandatory-time reason is required")
        mandatory = _parse_utc(mandatory_at_utc)
        if mandatory is None:
            raise RegistrationError("A mandatory match time is required")
        if mandatory <= self.clock().astimezone(UTC):
            raise RegistrationError("The mandatory match time must be in the future")
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            rounds = [dict(row) for row in old_rounds]
            matches = [dict(row) for row in old_matches]
            match = _single_match(matches, tid, match_id)
            round_row = _single_round(rounds, tid, _text(match.get("round_id")))
            if _text(match.get("status")) not in MATCH_OPEN_STATUSES:
                raise RegistrationError("Only an unresolved open matchup can receive a mandatory time")
            deadline = _parse_utc(_text(round_row.get("deadline_at_utc")))
            if deadline is None:
                raise RegistrationError("The round deadline is missing")
            if mandatory > deadline:
                raise RegistrationError(
                    "Extend the round first. A mandatory match time cannot be later than the round deadline."
                )
            now = utc_iso(self.clock().astimezone(UTC))
            mandatory_text = utc_iso(mandatory)
            match["scheduling_conflict_notes"] = _append_note(
                _text(match.get("scheduling_conflict_notes")),
                f"MANDATORY_TIME={mandatory_text} | organizer={actor_id} | reason={reason}",
            )
            await self.repository.persist_matches(matches, previous_matches=old_matches)
            await self._audit(
                tid,
                str(actor_id),
                "match_mandatory_time_imposed",
                {"match_id": match_id, "mandatory_at_utc": mandatory_text, "reason": reason},
                now,
            )
            return dict(match)

    async def resolve_scheduling(
        self,
        actor_id: str,
        match_id: str,
        action: str,
        *,
        reason: str,
    ) -> dict[str, object]:
        if action not in {"forfeit_a", "forfeit_b", "double_forfeit"}:
            raise RegistrationError("Unknown scheduling ruling")
        reason = str(reason or "").strip()
        if not reason:
            raise RegistrationError("A scheduling ruling reason is required")
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            rounds = [dict(row) for row in old_rounds]
            matches = [dict(row) for row in old_matches]
            match = _single_match(matches, tid, match_id)
            round_row = _single_round(rounds, tid, _text(match.get("round_id")))
            if _text(round_row.get("status")) not in ROUND_OPEN_STATUSES:
                raise RegistrationError("This round is not open for a scheduling ruling")
            if _text(match.get("status")) not in MATCH_OPEN_STATUSES:
                raise RegistrationError("This matchup is not unresolved")

            now_dt = self.clock().astimezone(UTC)
            reported = _parse_utc(_text(match.get("scheduling_problem_reported_at_utc")))
            deadline = _parse_utc(_text(round_row.get("deadline_at_utc")))
            mandatory = _mandatory_time(_text(match.get("scheduling_conflict_notes")))
            review_due = reported is not None and now_dt >= reported + _SCHEDULING_GRACE
            deadline_due = deadline is not None and now_dt >= deadline
            mandatory_due = mandatory is not None and now_dt >= mandatory
            if not (review_due or deadline_due or mandatory_due):
                raise RegistrationError(
                    "The 24-hour scheduling grace period has not elapsed and no imposed deadline is due yet"
                )

            now = utc_iso(now_dt)
            if action == "double_forfeit":
                match.update(
                    status="double_forfeit",
                    final_result_type="double_forfeit",
                    final_winner_discord_user_id="",
                )
            else:
                winner = (
                    _text(match.get("player_b_discord_user_id"))
                    if action == "forfeit_a"
                    else _text(match.get("player_a_discord_user_id"))
                )
                match.update(
                    status="forfeit",
                    final_result_type="forfeit",
                    final_winner_discord_user_id=winner,
                )
            match.update(
                final_score_a="",
                final_score_b="",
                finalized_by_discord_user_id=str(actor_id),
                finalized_at_utc=now,
                confirmed_at_utc=now,
            )
            match["notes"] = _append_note(
                _text(match.get("notes")),
                f"Organizer scheduling ruling ({action}): {reason}",
            )
            _mark_round_ready_if_complete(rounds, matches, tid, _text(match.get("round_id")))
            await self.repository.persist_state(
                rounds,
                matches,
                previous_rounds=old_rounds,
                previous_matches=old_matches,
            )
            await self._audit(
                tid,
                str(actor_id),
                "match_scheduling_organizer_resolved",
                {"match_id": match_id, "action": action, "reason": reason},
                now,
            )
            return dict(match)

    async def withdraw_active_participant(
        self,
        actor_id: str,
        target_user_id: str,
        *,
        reason: str,
    ) -> dict[str, object]:
        """Withdraw an active participant while preserving completed competition truth."""
        reason = str(reason or "").strip()
        if not reason:
            raise RegistrationError("A withdrawal reason is required")
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            participants = await self.registration_repository.participants()
            target = next(
                (
                    row
                    for row in participants
                    if _text(row.get("tournament_id")) == tid
                    and _text(row.get("discord_user_id")) == str(target_user_id)
                ),
                None,
            )
            if target is None or _text(target.get("status")) != "confirmed":
                raise RegistrationError("The participant is not currently confirmed")

            # Tournament status comes from the regular registration repository context.
            tournaments = await self.registration_repository.tournaments()
            tournament = next(
                (row for row in tournaments if _text(row.get("tournament_id")) == tid),
                None,
            )
            if tournament is None or _text(tournament.get("status")) != "active":
                raise RegistrationError("Post-start withdrawals are only available while the tournament is active")

            old_participants = [dict(row) for row in participants]
            now = utc_iso(self.clock().astimezone(UTC))
            target.update(
                status="withdrawn",
                withdrawn_at_utc=now,
                withdrawal_reason=reason,
                updated_at_utc=now,
            )
            await self.registration_repository.persist_participants(
                participants,
                previous_participants=old_participants,
            )

            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            rounds = [dict(row) for row in old_rounds]
            matches = [dict(row) for row in old_matches]
            affected = []
            for match in matches:
                if _text(match.get("tournament_id")) != tid:
                    continue
                if _text(match.get("status")) in MATCH_TERMINAL_STATUSES:
                    continue
                a = _text(match.get("player_a_discord_user_id"))
                b = _text(match.get("player_b_discord_user_id"))
                if str(target_user_id) not in {a, b}:
                    continue
                round_row = _single_round(rounds, tid, _text(match.get("round_id")))
                round_status = _text(round_row.get("status"))
                if round_status in {"preview", "proposed", "approved"}:
                    # Unpublished draws must be regenerated from the remaining confirmed roster.
                    round_row["notes"] = _append_note(
                        _text(round_row.get("notes")),
                        f"INVALIDATED_BY_WITHDRAWAL={target_user_id}@{now}",
                    )
                    affected.append({"match_id": _text(match.get("match_id")), "action": "preview_invalidated"})
                    continue
                if round_status not in ROUND_OPEN_STATUSES:
                    continue
                opponent = b if a == str(target_user_id) else a
                if opponent:
                    match.update(
                        status="forfeit",
                        final_result_type="forfeit",
                        final_score_a="",
                        final_score_b="",
                        final_winner_discord_user_id=opponent,
                        finalized_by_discord_user_id=str(actor_id),
                        finalized_at_utc=now,
                        confirmed_at_utc=now,
                    )
                    match["notes"] = _append_note(
                        _text(match.get("notes")),
                        f"Withdrawal forfeit: <@{target_user_id}> withdrew. Reason: {reason}",
                    )
                    affected.append({"match_id": _text(match.get("match_id")), "action": "forfeit", "winner": opponent})
                    _mark_round_ready_if_complete(rounds, matches, tid, _text(match.get("round_id")))

            await self.repository.persist_state(
                rounds,
                matches,
                previous_rounds=old_rounds,
                previous_matches=old_matches,
            )
            await self._audit(
                tid,
                str(actor_id),
                "active_participant_withdrawn",
                {"target_user_id": str(target_user_id), "reason": reason, "affected_matches": affected},
                now,
                target_user_id=str(target_user_id),
            )
            return dict(target)

    async def _audit(self, tid, actor, event, details, now, *, target_user_id=""):
        try:
            await self.registration_repository.append_audit(
                dict(
                    event_id=str(uuid4()),
                    tournament_id=tid,
                    event_type=event,
                    actor_discord_user_id=str(actor),
                    target_discord_user_id=str(target_user_id or ""),
                    details=json.dumps(details, sort_keys=True, separators=(",", ":")),
                    created_at_utc=now,
                )
            )
        except Exception:
            import logging

            logging.getLogger("c1c.community.live_arena.competition_operations").exception(
                "Live Arena operations audit append failed • event=%s", event
            )


def _mandatory_time(notes: str) -> datetime | None:
    for line in str(notes or "").splitlines():
        if "MANDATORY_TIME=" not in line:
            continue
        value = line.split("MANDATORY_TIME=", 1)[1].split("|", 1)[0].strip()
        parsed = _parse_utc(value)
        if parsed is not None:
            return parsed
    return None
