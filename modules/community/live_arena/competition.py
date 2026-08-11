"""Result handling, qualification standings, and round lifecycle for Live Arena."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from modules.community.live_arena.qualification import QualificationRepository
from modules.community.live_arena.registration import RegistrationError, _locks, utc_iso
from modules.community.live_arena.repository import LiveArenaRepository
from modules.community.live_arena.service import _text, load_config

log = logging.getLogger("c1c.community.live_arena.competition")

ROUND_OPEN_STATUSES = {"active", "published", "open", "published/open"}
ROUND_CLOSABLE_STATUSES = {"ready_to_close", "correction_in_progress"}
MATCH_OPEN_STATUSES = {"published", "open"}
MATCH_TERMINAL_STATUSES = {"finalized", "forfeit", "double_forfeit", "bye"}


@dataclass(frozen=True)
class StandingEntry:
    discord_user_id: str
    display_name: str
    match_wins: int
    match_losses: int
    game_wins: int
    game_losses: int
    game_differential: int
    strength_of_opponents: int
    rank: int
    tied: bool = False

    @property
    def match_record(self) -> str:
        return f"{self.match_wins}-{self.match_losses}"


class LiveArenaCompetitionService:
    """Own match results and derived round/qualification state.

    This service deliberately derives standings from MATCHES instead of persisting a
    second standings table. ROUNDS and MATCHES remain the competition source of truth.
    """

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
            _require_round_open(round_row)
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
            if deadline_dt is not None and now_dt > deadline_dt:
                raise RegistrationError(
                    "The current round deadline has passed. An organizer must review this late result."
                )
            confirm_due = now_dt + timedelta(hours=24)
            if deadline_dt is not None and confirm_due > deadline_dt:
                confirm_due = deadline_dt
            now = utc_iso(now_dt)
            match.update(
                status="pending_confirmation",
                reported_by_discord_user_id=actor,
                reported_score_a=str(score_a),
                reported_score_b=str(score_b),
                reported_at_utc=now,
                confirm_due_at_utc=utc_iso(confirm_due),
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
            await self._audit(
                tid,
                actor,
                "match_result_reported",
                {
                    "match_id": match_id,
                    "score_a": score_a,
                    "score_b": score_b,
                    "confirm_due_at_utc": _text(match["confirm_due_at_utc"]),
                },
                now,
            )
            return dict(match)

    async def dispute_result(self, actor_id: str, match_id: str) -> dict[str, object]:
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_matches = await self.repository.matches()
            matches = [dict(row) for row in old_matches]
            match = _single_match(matches, tid, match_id)
            if _text(match["status"]) != "pending_confirmation":
                raise RegistrationError("Only a pending reported result can be disputed")
            actor = str(actor_id)
            reporter = _text(match["reported_by_discord_user_id"])
            players = {
                _text(match["player_a_discord_user_id"]),
                _text(match["player_b_discord_user_id"]),
            }
            if actor not in players or actor == reporter:
                raise RegistrationError("Only the non-reporting opponent can dispute this result")
            due = _parse_utc(_text(match["confirm_due_at_utc"]))
            now_dt = self.clock().astimezone(UTC)
            if due is not None and now_dt > due:
                raise RegistrationError("The dispute window has already expired")
            now = utc_iso(now_dt)
            match.update(
                status="disputed",
                disputed_by_discord_user_id=actor,
                disputed_at_utc=now,
            )
            await self.repository.persist_matches(matches, previous_matches=old_matches)
            await self._audit(
                tid,
                actor,
                "match_result_disputed",
                {"match_id": match_id, "reported_by": reporter},
                now,
            )
            return dict(match)

    async def finalize_match_if_due(self, match_id: str) -> dict[str, object] | None:
        """Finalize one undisputed result if its objection window has expired."""
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            rounds = [dict(row) for row in old_rounds]
            matches = [dict(row) for row in old_matches]
            match = _single_match(matches, tid, match_id)
            if _text(match["status"]) != "pending_confirmation":
                return None
            due = _parse_utc(_text(match["confirm_due_at_utc"]))
            now_dt = self.clock().astimezone(UTC)
            if due is None or now_dt < due:
                return None
            _finalize_played_result(match, utc_iso(now_dt), "system")
            _mark_round_ready_if_complete(rounds, matches, tid, _text(match["round_id"]))
            await self.repository.persist_state(
                rounds,
                matches,
                previous_rounds=old_rounds,
                previous_matches=old_matches,
            )
            now = utc_iso(now_dt)
            await self._audit(
                tid,
                "system",
                "match_result_auto_finalized",
                {"match_id": match_id},
                now,
            )
            return dict(match)

    async def finalize_due_results(self) -> list[str]:
        """Idempotent reconciliation for startup and targeted scheduler recovery."""
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        rows = await self.repository.matches()
        now_dt = self.clock().astimezone(UTC)
        due_ids = [
            _text(row["match_id"])
            for row in rows
            if _text(row["tournament_id"]) == tid
            and _text(row["status"]) == "pending_confirmation"
            and (_parse_utc(_text(row["confirm_due_at_utc"])) or now_dt + timedelta(days=1)) <= now_dt
        ]
        finalized: list[str] = []
        for match_id in due_ids:
            if await self.finalize_match_if_due(match_id) is not None:
                finalized.append(match_id)
        return finalized

    async def close_round(self, actor_id: str, round_id: str) -> dict[str, object]:
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_rounds = await self.repository.rounds()
            matches = await self.repository.matches()
            rounds = [dict(row) for row in old_rounds]
            round_row = _single_round(rounds, tid, round_id)
            status = _text(round_row["status"])
            if status not in ROUND_CLOSABLE_STATUSES:
                raise RegistrationError("This round is not ready to close")
            round_matches = _round_matches(matches, tid, round_id)
            if not round_matches or any(
                _text(row["status"]) not in MATCH_TERMINAL_STATUSES for row in round_matches
            ):
                raise RegistrationError("Every match must be finalized before the round can close")
            now = utc_iso(self.clock().astimezone(UTC))
            round_row.update(status="closed", completed_at_utc=now)
            await self.repository.persist_rounds(rounds, previous_rounds=old_rounds)
            await self._audit(
                tid,
                str(actor_id),
                "round_closed",
                {"round_id": round_id},
                now,
            )
            return dict(round_row)

    async def reopen_round(self, actor_id: str, round_id: str) -> dict[str, object]:
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_rounds = await self.repository.rounds()
            rounds = [dict(row) for row in old_rounds]
            round_row = _single_round(rounds, tid, round_id)
            if _text(round_row["status"]) != "closed":
                raise RegistrationError("Only a closed round can be reopened for correction")
            current_number = _safe_int(round_row.get("round_number"), 0)
            later_live = [
                row
                for row in rounds
                if _text(row["tournament_id"]) == tid
                and _safe_int(row.get("round_number"), 0) > current_number
                and _text(row["status"]) in ROUND_OPEN_STATUSES | {"ready_to_close", "closed"}
            ]
            if later_live:
                raise RegistrationError(
                    "This round is competitively final because the next round has already opened."
                )
            now = utc_iso(self.clock().astimezone(UTC))
            round_row.update(status="correction_in_progress", completed_at_utc="")
            await self.repository.persist_rounds(rounds, previous_rounds=old_rounds)
            await self._audit(
                tid,
                str(actor_id),
                "round_reopened_for_correction",
                {"round_id": round_id},
                now,
            )
            return dict(round_row)

    async def correct_final_result(
        self,
        actor_id: str,
        match_id: str,
        score_a: int,
        score_b: int,
        *,
        reason: str,
    ) -> dict[str, object]:
        reason = str(reason or "").strip()
        if not reason:
            raise RegistrationError("A correction reason is required")
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_matches = await self.repository.matches()
            rounds = await self.repository.rounds()
            matches = [dict(row) for row in old_matches]
            match = _single_match(matches, tid, match_id)
            round_row = _single_round(rounds, tid, _text(match["round_id"]))
            if _text(round_row["status"]) != "correction_in_progress":
                raise RegistrationError("Reopen the round before correcting a finalized result")
            if _text(match["status"]) not in MATCH_TERMINAL_STATUSES:
                raise RegistrationError("Only a finalized match can be corrected here")
            _validate_played_score(round_row, score_a, score_b)
            now = utc_iso(self.clock().astimezone(UTC))
            match.update(
                status="finalized",
                final_result_type="played",
                final_score_a=str(score_a),
                final_score_b=str(score_b),
                final_winner_discord_user_id=(
                    _text(match["player_a_discord_user_id"])
                    if score_a > score_b
                    else _text(match["player_b_discord_user_id"])
                ),
                finalized_by_discord_user_id=str(actor_id),
                finalized_at_utc=now,
                notes=_append_note(_text(match.get("notes")), f"Correction: {reason}"),
            )
            await self.repository.persist_matches(matches, previous_matches=old_matches)
            await self._audit(
                tid,
                str(actor_id),
                "match_result_corrected",
                {"match_id": match_id, "score_a": score_a, "score_b": score_b, "reason": reason},
                now,
            )
            return dict(match)

    async def match_for_thread(self, thread_id: str) -> dict[str, object]:
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        matches = await self.repository.matches()
        found = [
            dict(row)
            for row in matches
            if _text(row["tournament_id"]) == tid and _text(row["thread_id"]) == str(thread_id)
        ]
        if len(found) != 1:
            raise RegistrationError("This Duelling Deck thread is not linked to exactly one active tournament match")
        return found[0]

    async def standings(self) -> list[StandingEntry]:
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        matches = await self.repository.matches()
        return calculate_qualification_standings(matches, tid)

    async def _audit(self, tid, actor, event, details, now):
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
            log.exception(
                "Live Arena competition audit append failed • tournament=%s • event=%s",
                tid,
                event,
            )


def calculate_qualification_standings(matches, tournament_id: str) -> list[StandingEntry]:
    """Return official qualification order from finalized qualification matches only."""
    tid = str(tournament_id)
    played = [
        row
        for row in matches
        if _text(row.get("tournament_id")) == tid
        and _is_qualification_round(row)
        and _text(row.get("status")) in MATCH_TERMINAL_STATUSES
    ]
    players: dict[str, dict[str, object]] = {}
    opponents: dict[str, list[str]] = {}
    head_to_head: dict[frozenset[str], str] = {}

    for row in played:
        a = _text(row.get("player_a_discord_user_id"))
        b = _text(row.get("player_b_discord_user_id"))
        if a:
            players.setdefault(a, _stats(a, _text(row.get("player_a_display_name"))))
        if b:
            players.setdefault(b, _stats(b, _text(row.get("player_b_display_name"))))
        result_type = _text(row.get("final_result_type"))
        if result_type == "bye" or not b:
            if a:
                players[a]["match_wins"] += 1
                players[a]["game_wins"] += 2
            continue
        if not a or not b or result_type == "double_forfeit":
            continue
        opponents.setdefault(a, []).append(b)
        opponents.setdefault(b, []).append(a)
        if result_type == "forfeit":
            winner = _text(row.get("final_winner_discord_user_id"))
            loser = b if winner == a else a
            if winner in players:
                players[winner]["match_wins"] += 1
            if loser in players:
                players[loser]["match_losses"] += 1
            if winner:
                head_to_head[frozenset((a, b))] = winner
            continue
        try:
            score_a = int(_text(row.get("final_score_a")))
            score_b = int(_text(row.get("final_score_b")))
        except ValueError:
            continue
        players[a]["game_wins"] += score_a
        players[a]["game_losses"] += score_b
        players[b]["game_wins"] += score_b
        players[b]["game_losses"] += score_a
        winner, loser = (a, b) if score_a > score_b else (b, a)
        players[winner]["match_wins"] += 1
        players[loser]["match_losses"] += 1
        head_to_head[frozenset((a, b))] = winner

    for uid, stats in players.items():
        stats["game_differential"] = stats["game_wins"] - stats["game_losses"]
        stats["strength_of_opponents"] = sum(
            int(players.get(opponent, {}).get("match_wins", 0))
            for opponent in opponents.get(uid, [])
        )

    buckets: dict[tuple[int, int, int], list[str]] = {}
    for uid, stats in players.items():
        key = (
            int(stats["match_wins"]),
            int(stats["game_differential"]),
            int(stats["strength_of_opponents"]),
        )
        buckets.setdefault(key, []).append(uid)

    ordered: list[tuple[str, bool]] = []
    for key in sorted(buckets, reverse=True):
        group = sorted(buckets[key])
        if len(group) == 2:
            winner = head_to_head.get(frozenset(group))
            if winner in group:
                loser = group[1] if group[0] == winner else group[0]
                ordered.extend(((winner, False), (loser, False)))
                continue
        tied = len(group) > 1
        ordered.extend((uid, tied) for uid in group)

    result: list[StandingEntry] = []
    position = 1
    index = 0
    while index < len(ordered):
        uid, tied = ordered[index]
        stats = players[uid]
        shared_count = 1
        if tied:
            key = (
                stats["match_wins"],
                stats["game_differential"],
                stats["strength_of_opponents"],
            )
            shared_count = sum(
                1
                for other_uid, other_tied in ordered[index:]
                if other_tied
                and (
                    players[other_uid]["match_wins"],
                    players[other_uid]["game_differential"],
                    players[other_uid]["strength_of_opponents"],
                ) == key
            )
        result.append(
            StandingEntry(
                discord_user_id=uid,
                display_name=str(stats["display_name"]),
                match_wins=int(stats["match_wins"]),
                match_losses=int(stats["match_losses"]),
                game_wins=int(stats["game_wins"]),
                game_losses=int(stats["game_losses"]),
                game_differential=int(stats["game_differential"]),
                strength_of_opponents=int(stats["strength_of_opponents"]),
                rank=position,
                tied=tied,
            )
        )
        if tied and shared_count > 1:
            for offset in range(1, shared_count):
                other_uid, _ = ordered[index + offset]
                other = players[other_uid]
                result.append(
                    StandingEntry(
                        discord_user_id=other_uid,
                        display_name=str(other["display_name"]),
                        match_wins=int(other["match_wins"]),
                        match_losses=int(other["match_losses"]),
                        game_wins=int(other["game_wins"]),
                        game_losses=int(other["game_losses"]),
                        game_differential=int(other["game_differential"]),
                        strength_of_opponents=int(other["strength_of_opponents"]),
                        rank=position,
                        tied=True,
                    )
                )
            position += shared_count
            index += shared_count
        else:
            position += 1
            index += 1
    return result


def _stats(uid: str, name: str) -> dict[str, object]:
    return {
        "discord_user_id": uid,
        "display_name": name or uid,
        "match_wins": 0,
        "match_losses": 0,
        "game_wins": 0,
        "game_losses": 0,
        "game_differential": 0,
        "strength_of_opponents": 0,
    }


def _is_qualification_round(row) -> bool:
    round_id = _text(row.get("round_id")).upper()
    return "-Q1" in round_id or "-Q2" in round_id or "-Q3" in round_id


def _single_match(matches, tid: str, match_id: str):
    found = [
        row
        for row in matches
        if _text(row.get("tournament_id")) == tid and _text(row.get("match_id")) == str(match_id)
    ]
    if len(found) != 1:
        raise RegistrationError("Match could not be resolved uniquely")
    return found[0]


def _single_round(rounds, tid: str, round_id: str):
    found = [
        row
        for row in rounds
        if _text(row.get("tournament_id")) == tid and _text(row.get("round_id")) == str(round_id)
    ]
    if len(found) != 1:
        raise RegistrationError("Round could not be resolved uniquely")
    return found[0]


def _round_matches(matches, tid: str, round_id: str):
    return [
        row
        for row in matches
        if _text(row.get("tournament_id")) == tid and _text(row.get("round_id")) == str(round_id)
    ]


def _require_round_open(round_row) -> None:
    if _text(round_row.get("status")) not in ROUND_OPEN_STATUSES:
        raise RegistrationError("This round is not open for match reporting")


def _validate_played_score(round_row, score_a: int, score_b: int) -> None:
    try:
        a, b = int(score_a), int(score_b)
    except (TypeError, ValueError) as exc:
        raise RegistrationError("Result scores must be whole numbers") from exc
    is_final = _text(round_row.get("round_stage")).lower() == "final"
    wins_required = 3 if is_final else 2
    if max(a, b) != wins_required or min(a, b) < 0 or a == b or min(a, b) >= wins_required:
        format_name = "BO5" if is_final else "BO3"
        valid = "3-0, 3-1, or 3-2" if is_final else "2-0 or 2-1"
        raise RegistrationError(f"{format_name} result must be {valid}")


def _finalize_played_result(match, now: str, actor: str) -> None:
    score_a = int(_text(match["reported_score_a"]))
    score_b = int(_text(match["reported_score_b"]))
    winner = (
        _text(match["player_a_discord_user_id"])
        if score_a > score_b
        else _text(match["player_b_discord_user_id"])
    )
    match.update(
        status="finalized",
        confirmed_at_utc=now,
        final_result_type="played",
        final_score_a=str(score_a),
        final_score_b=str(score_b),
        final_winner_discord_user_id=winner,
        finalized_by_discord_user_id=actor,
        finalized_at_utc=now,
    )


def _mark_round_ready_if_complete(rounds, matches, tid: str, round_id: str) -> None:
    round_row = _single_round(rounds, tid, round_id)
    if _text(round_row["status"]) not in ROUND_OPEN_STATUSES:
        return
    round_matches = _round_matches(matches, tid, round_id)
    if round_matches and all(_text(row["status"]) in MATCH_TERMINAL_STATUSES for row in round_matches):
        round_row["status"] = "ready_to_close"


def _parse_utc(value: str) -> datetime | None:
    value = str(value or "").strip()
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RegistrationError(f"Invalid UTC timestamp: {value}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _safe_int(value, default: int) -> int:
    try:
        return int(_text(value))
    except (TypeError, ValueError):
        return default


def _append_note(existing: str, addition: str) -> str:
    return f"{existing}\n{addition}".strip() if existing else addition
