"""Organizer result resolution and late-report handling for Live Arena PR 6B-1."""

from __future__ import annotations

import json
from datetime import UTC
from uuid import uuid4

from modules.community.live_arena.competition import (
    MATCH_OPEN_STATUSES,
    MATCH_TERMINAL_STATUSES,
    ROUND_OPEN_STATUSES,
    LiveArenaCompetitionService,
    _append_note,
    _finalize_played_result,
    _parse_utc,
    _round_matches,
    _single_match,
    _single_round,
    _text,
    _validate_played_score,
)
from modules.community.live_arena.registration import RegistrationError, _locks, utc_iso
from modules.community.live_arena.service import load_config

_REVIEWABLE_RESULT_STATUSES = {"disputed", "late_review"}
_REPLAYABLE_ROUND_STATUSES = ROUND_OPEN_STATUSES | {"correction_in_progress"}


class CompetitionResolutionService(LiveArenaCompetitionService):
    """Complete result workflow layered over the 6B-1 competition core."""

    async def report_result(
        self,
        actor_id: str,
        match_id: str,
        score_a: int,
        score_b: int,
        *,
        screenshot_present: bool,
    ) -> dict[str, object]:
        if not screenshot_present:
            raise RegistrationError(
                "Post at least one result screenshot in the Duelling Deck thread before reporting the score."
            )
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            rounds = [dict(row) for row in old_rounds]
            matches = [dict(row) for row in old_matches]
            match = _single_match(matches, tid, match_id)
            round_row = _single_round(rounds, tid, _text(match["round_id"]))
            round_status = _text(round_row.get("status"))
            if round_status not in _REPLAYABLE_ROUND_STATUSES:
                raise RegistrationError("This round is not open for match reporting")
            if _text(match["status"]) not in MATCH_OPEN_STATUSES:
                raise RegistrationError("This match is not open for a new result report")

            actor = str(actor_id)
            player_a = _text(match["player_a_discord_user_id"])
            player_b = _text(match["player_b_discord_user_id"])
            if actor not in {player_a, player_b}:
                raise RegistrationError("Only a player in this matchup can report its result")
            _validate_played_score(round_row, score_a, score_b)

            now_dt = self.clock().astimezone(UTC)
            deadline_dt = _parse_utc(_text(round_row["deadline_at_utc"]))
            late = deadline_dt is not None and now_dt > deadline_dt
            confirm_due = None if late else now_dt + __import__("datetime").timedelta(hours=24)
            if confirm_due is not None and deadline_dt is not None and confirm_due > deadline_dt:
                confirm_due = deadline_dt
            now = utc_iso(now_dt)
            match.update(
                status="late_review" if late else "pending_confirmation",
                reported_by_discord_user_id=actor,
                reported_score_a=str(score_a),
                reported_score_b=str(score_b),
                reported_at_utc=now,
                confirm_due_at_utc="" if confirm_due is None else utc_iso(confirm_due),
                confirmed_by_discord_user_id="",
                confirmed_at_utc="",
                disputed_by_discord_user_id="",
                disputed_at_utc="",
                final_result_type="",
                final_score_a="",
                final_score_b="",
                final_winner_discord_user_id="",
                finalized_by_discord_user_id="",
                finalized_at_utc="",
            )
            await self.repository.persist_matches(matches, previous_matches=old_matches)
            await self._audit_resolution(
                tid,
                actor,
                "match_result_late_reported" if late else "match_result_reported",
                {
                    "match_id": match_id,
                    "score_a": int(score_a),
                    "score_b": int(score_b),
                    "confirm_due_at_utc": _text(match["confirm_due_at_utc"]),
                },
                now,
            )
            return dict(match)

    async def reviewable_matches(self) -> list[dict[str, object]]:
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        rounds = await self.repository.rounds()
        matches = await self.repository.matches()
        correction_rounds = {
            _text(row["round_id"])
            for row in rounds
            if _text(row.get("tournament_id")) == tid
            and _text(row.get("status")) == "correction_in_progress"
        }
        return [
            dict(row)
            for row in matches
            if _text(row.get("tournament_id")) == tid
            and (
                _text(row.get("status")) in _REVIEWABLE_RESULT_STATUSES
                or (
                    _text(row.get("round_id")) in correction_rounds
                    and _text(row.get("status")) in MATCH_TERMINAL_STATUSES
                )
            )
        ]

    async def resolve_match(
        self,
        actor_id: str,
        match_id: str,
        action: str,
        *,
        reason: str,
        score_a: int | None = None,
        score_b: int | None = None,
    ) -> dict[str, object]:
        reason = str(reason or "").strip()
        if not reason:
            raise RegistrationError("An organizer ruling reason is required")
        if action not in {
            "accept",
            "correct",
            "replay",
            "forfeit_a",
            "forfeit_b",
            "double_forfeit",
        }:
            raise RegistrationError("Unknown organizer result ruling")

        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            rounds = [dict(row) for row in old_rounds]
            matches = [dict(row) for row in old_matches]
            match = _single_match(matches, tid, match_id)
            round_row = _single_round(rounds, tid, _text(match["round_id"]))
            match_status = _text(match["status"])
            round_status = _text(round_row["status"])

            issue = match_status in _REVIEWABLE_RESULT_STATUSES
            correction = (
                round_status == "correction_in_progress"
                and match_status in MATCH_TERMINAL_STATUSES
            )
            if not issue and not correction:
                raise RegistrationError("This match is not awaiting organizer result review")
            if action == "accept" and not issue:
                raise RegistrationError("Only a disputed or late reported result can be accepted")

            now = utc_iso(self.clock().astimezone(UTC))
            if action == "accept":
                if not _text(match.get("reported_score_a")) or not _text(match.get("reported_score_b")):
                    raise RegistrationError("The reported score is missing")
                _validate_played_score(
                    round_row,
                    int(_text(match["reported_score_a"])),
                    int(_text(match["reported_score_b"])),
                )
                _finalize_played_result(match, now, str(actor_id))
            elif action == "correct":
                if score_a is None or score_b is None:
                    raise RegistrationError("A corrected score is required")
                _validate_played_score(round_row, score_a, score_b)
                match.update(
                    status="finalized",
                    final_result_type="played",
                    final_score_a=str(int(score_a)),
                    final_score_b=str(int(score_b)),
                    final_winner_discord_user_id=(
                        _text(match["player_a_discord_user_id"])
                        if int(score_a) > int(score_b)
                        else _text(match["player_b_discord_user_id"])
                    ),
                    finalized_by_discord_user_id=str(actor_id),
                    finalized_at_utc=now,
                    confirmed_at_utc=now,
                )
            elif action == "replay":
                match.update(
                    status="published",
                    reported_by_discord_user_id="",
                    reported_score_a="",
                    reported_score_b="",
                    reported_at_utc="",
                    confirm_due_at_utc="",
                    confirmed_by_discord_user_id="",
                    confirmed_at_utc="",
                    disputed_by_discord_user_id="",
                    disputed_at_utc="",
                    final_result_type="",
                    final_score_a="",
                    final_score_b="",
                    final_winner_discord_user_id="",
                    finalized_by_discord_user_id="",
                    finalized_at_utc="",
                )
            elif action in {"forfeit_a", "forfeit_b"}:
                winner = (
                    _text(match["player_b_discord_user_id"])
                    if action == "forfeit_a"
                    else _text(match["player_a_discord_user_id"])
                )
                match.update(
                    status="forfeit",
                    final_result_type="forfeit",
                    final_score_a="",
                    final_score_b="",
                    final_winner_discord_user_id=winner,
                    finalized_by_discord_user_id=str(actor_id),
                    finalized_at_utc=now,
                    confirmed_at_utc=now,
                )
            else:
                match.update(
                    status="double_forfeit",
                    final_result_type="double_forfeit",
                    final_score_a="",
                    final_score_b="",
                    final_winner_discord_user_id="",
                    finalized_by_discord_user_id=str(actor_id),
                    finalized_at_utc=now,
                    confirmed_at_utc=now,
                )

            match["notes"] = _append_note(
                _text(match.get("notes")),
                f"Organizer ruling ({action}): {reason}",
            )
            self._mark_ready(rounds, matches, tid, _text(match["round_id"]))
            await self.repository.persist_state(
                rounds,
                matches,
                previous_rounds=old_rounds,
                previous_matches=old_matches,
            )
            await self._audit_resolution(
                tid,
                str(actor_id),
                "match_result_organizer_resolved",
                {
                    "match_id": match_id,
                    "action": action,
                    "reason": reason,
                    "score_a": score_a,
                    "score_b": score_b,
                },
                now,
            )
            return dict(match)

    @staticmethod
    def _mark_ready(rounds, matches, tid: str, round_id: str) -> None:
        round_row = _single_round(rounds, tid, round_id)
        if _text(round_row["status"]) not in ROUND_OPEN_STATUSES:
            return
        round_matches = _round_matches(matches, tid, round_id)
        if round_matches and all(
            _text(row["status"]) in MATCH_TERMINAL_STATUSES for row in round_matches
        ):
            round_row["status"] = "ready_to_close"

    async def _audit_resolution(self, tid, actor, event, details, now):
        try:
            await self.registration_repository.append_audit(
                dict(
                    event_id=str(uuid4()),
                    tournament_id=tid,
                    event_type=event,
                    actor_discord_user_id=str(actor),
                    target_discord_user_id="",
                    details=json.dumps(details, sort_keys=True, separators=(",", ":")),
                    created_at_utc=now,
                )
            )
        except Exception:
            # Competition mutations remain authoritative even if audit delivery fails.
            import logging

            logging.getLogger("c1c.community.live_arena.competition_resolution").exception(
                "Live Arena competition audit append failed • tournament=%s • event=%s",
                tid,
                event,
            )
