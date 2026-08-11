"""Deterministic Swiss pairing and preview lifecycle for Live Arena Q2/Q3."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from uuid import uuid4

from modules.community.live_arena.competition import calculate_qualification_standings
from modules.community.live_arena.organizer import OrganizerService
from modules.community.live_arena.qualification import (
    ROUND_HEADERS,
    QualificationRepository,
    QualificationSnapshot,
    _blank,
    _enabled_availability,
    _match_row,
    _match_sort_key,
    _shared_slots,
    _slot_rank,
)
from modules.community.live_arena.registration import RegistrationError, _locks, utc_iso
from modules.community.live_arena.repository import LiveArenaRepository
from modules.community.live_arena.service import _text, load_config

log = logging.getLogger("c1c.community.live_arena.swiss")

_SWISS_ROUNDS = {2, 3}
_SOURCE_NOTE_PREFIX = "swiss_source_fingerprint="


@dataclass(frozen=True)
class SwissPlayer:
    user_id: str
    display_name: str
    wins: int
    losses: int
    rank: int
    ranking_index: int

    @property
    def record(self) -> tuple[int, int]:
        return self.wins, self.losses

    @property
    def record_label(self) -> str:
        return f"{self.wins}-{self.losses}"


@dataclass(frozen=True)
class SwissPair:
    player_a: SwissPlayer
    player_b: SwissPlayer
    rationale: str


class SwissPairingError(RegistrationError):
    """Raised when the hard Swiss constraints have no valid complete solution."""


class SwissQualificationService:
    """Own Q2/Q3 preview generation, validation, approval, and publication."""

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

    async def context(self):
        return await OrganizerService(
            self.sheet_id,
            repository=self.registration_repository,
            clock=self.clock,
        ).context()

    async def snapshot(self, round_number: int) -> QualificationSnapshot:
        _require_swiss_round(round_number)
        config = await load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        round_id = f"{tid}-Q{round_number}"
        rounds = await self.repository.rounds()
        matches = await self.repository.matches()
        round_rows = [
            dict(row)
            for row in rounds
            if _text(row.get("tournament_id")) == tid
            and _text(row.get("round_id")) == round_id
        ]
        if len(round_rows) > 1:
            raise RegistrationError(f"ROUNDS contains duplicate {round_id}")
        qmatches = tuple(
            sorted(
                (
                    dict(row)
                    for row in matches
                    if _text(row.get("tournament_id")) == tid
                    and _text(row.get("round_id")) == round_id
                ),
                key=_match_sort_key,
            )
        )
        return QualificationSnapshot(round_rows[0] if round_rows else None, qmatches)

    async def generate_preview(self, actor_id: str, round_number: int, *, regenerate: bool = False) -> QualificationSnapshot:
        _require_swiss_round(round_number)
        config = await load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            previous_round = _single_round(old_rounds, tid, round_number - 1)
            if previous_round is None:
                raise RegistrationError(
                    f"Qualification Round {round_number - 1} must exist before Q{round_number} can be previewed"
                )
            if _text(previous_round.get("status")) in {"preview", "approved", "proposed"}:
                raise RegistrationError(
                    f"Qualification Round {round_number - 1} has not opened yet"
                )

            existing = _single_round(old_rounds, tid, round_number)
            if existing is not None and not regenerate:
                raise RegistrationError(
                    f"Q{round_number} already has a preview; use deterministic regeneration instead"
                )
            if existing is not None and _text(existing.get("status")) != "preview":
                raise RegistrationError(
                    f"Q{round_number} can only be regenerated while it is a preview"
                )

            participants = await self.registration_repository.participants()
            roster = [
                dict(row)
                for row in participants
                if _text(row.get("tournament_id")) == tid
                and _text(row.get("status")) == "confirmed"
            ]
            if len(roster) < 2 or len(roster) % 2:
                raise RegistrationError(
                    "Q2/Q3 Swiss preview currently requires an even confirmed roster; withdrawal/byes are handled in 6B-4"
                )

            standings = calculate_qualification_standings(old_matches, tid)
            players = _players_from_roster(roster, standings)
            history = _opponent_history(old_matches, tid, before_round=round_number)
            pairs = pair_swiss(players, history)

            availability = await self.registration_repository.availability()
            _, _, _, slots = await self.context()
            selected = _enabled_availability(availability, slots, tid)
            slot_rank = _slot_rank(slots)

            round_id = f"{tid}-Q{round_number}"
            generated_matches = []
            roster_by_id = {_text(row["discord_user_id"]): row for row in roster}
            for index, pair in enumerate(pairs, 1):
                shared = _shared_slots(
                    pair.player_a.user_id,
                    pair.player_b.user_id,
                    selected,
                    slot_rank,
                )
                row = _match_row(
                    tid,
                    round_id,
                    index,
                    roster_by_id[pair.player_a.user_id],
                    roster_by_id[pair.player_b.user_id],
                    shared,
                )
                row["status"] = "preview"
                row["notes"] = pair.rationale
                generated_matches.append(row)

            now = utc_iso(self.clock().astimezone(UTC))
            fingerprint = source_fingerprint(old_matches, tid, before_round=round_number)
            round_row = _blank(ROUND_HEADERS)
            if existing is not None:
                round_row.update(dict(existing))
            round_row.update(
                tournament_id=tid,
                round_id=round_id,
                round_name=f"Qualification Round {round_number}",
                round_stage="qualification",
                round_number=str(round_number),
                status="preview",
                opens_at_utc="",
                deadline_at_utc="",
                published_at_utc="",
                overview_message_id="",
                completed_at_utc="",
                generated_at_utc=now,
                generated_by_discord_user_id=str(actor_id),
                approved_at_utc="",
                approved_by_discord_user_id="",
                notes=f"{_SOURCE_NOTE_PREFIX}{fingerprint}",
            )
            rounds = [
                dict(row)
                for row in old_rounds
                if not (
                    _text(row.get("tournament_id")) == tid
                    and _text(row.get("round_id")) == round_id
                )
            ] + [round_row]
            matches = [
                dict(row)
                for row in old_matches
                if not (
                    _text(row.get("tournament_id")) == tid
                    and _text(row.get("round_id")) == round_id
                )
            ] + generated_matches
            await self.repository.persist_state(
                rounds,
                matches,
                previous_rounds=old_rounds,
                previous_matches=old_matches,
            )
            await self._audit(
                tid,
                str(actor_id),
                "swiss_preview_regenerated" if regenerate else "swiss_preview_generated",
                {
                    "round_number": round_number,
                    "match_count": len(generated_matches),
                    "source_fingerprint": fingerprint,
                },
                now,
            )
            return QualificationSnapshot(round_row, tuple(generated_matches))

    async def approve_preview(self, actor_id: str, round_number: int) -> QualificationSnapshot:
        _require_swiss_round(round_number)
        config = await load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            previous_round = _single_round(old_rounds, tid, round_number - 1)
            target = _single_round(old_rounds, tid, round_number)
            if previous_round is None or _text(previous_round.get("status")) != "closed":
                raise RegistrationError(
                    f"Q{round_number - 1} must be closed before Q{round_number} can be approved"
                )
            if target is None or _text(target.get("status")) != "preview":
                raise RegistrationError(f"Q{round_number} must have a preview before approval")
            expected = _source_fingerprint_from_notes(_text(target.get("notes")))
            current = source_fingerprint(old_matches, tid, before_round=round_number)
            if not expected or expected != current:
                raise RegistrationError(
                    "This Swiss preview is stale because finalized qualification results changed. Regenerate it before approval."
                )
            _validate_persisted_draw(old_matches, tid, round_number)
            now = utc_iso(self.clock().astimezone(UTC))
            rounds = [dict(row) for row in old_rounds]
            approved = next(
                row
                for row in rounds
                if _text(row.get("tournament_id")) == tid
                and _text(row.get("round_id")) == f"{tid}-Q{round_number}"
            )
            approved.update(
                status="approved",
                approved_at_utc=now,
                approved_by_discord_user_id=str(actor_id),
            )
            await self.repository.persist_rounds(rounds, previous_rounds=old_rounds)
            await self._audit(
                tid,
                str(actor_id),
                "swiss_draw_approved",
                {"round_number": round_number, "source_fingerprint": current},
                now,
            )
            matches = tuple(
                sorted(
                    (
                        dict(row)
                        for row in old_matches
                        if _text(row.get("tournament_id")) == tid
                        and _text(row.get("round_id")) == f"{tid}-Q{round_number}"
                    ),
                    key=_match_sort_key,
                )
            )
            return QualificationSnapshot(approved, matches)

    async def publish_approved(self, actor_id: str, round_number: int) -> QualificationSnapshot:
        _require_swiss_round(round_number)
        config = await load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            target = _single_round(old_rounds, tid, round_number)
            previous_round = _single_round(old_rounds, tid, round_number - 1)
            if previous_round is None or _text(previous_round.get("status")) != "closed":
                raise RegistrationError("The previous round must remain closed before publication")
            if target is None or _text(target.get("status")) != "approved":
                raise RegistrationError(f"Q{round_number} must be approved before publication")
            expected = _source_fingerprint_from_notes(_text(target.get("notes")))
            current = source_fingerprint(old_matches, tid, before_round=round_number)
            if not expected or expected != current:
                raise RegistrationError(
                    "This approved draw no longer matches canonical finalized results. Organizer review is required."
                )
            _validate_persisted_draw(old_matches, tid, round_number)
            now_dt = self.clock().astimezone(UTC)
            now = utc_iso(now_dt)
            deadline = utc_iso(now_dt + timedelta(days=6))
            rounds = [dict(row) for row in old_rounds]
            opened = next(
                row
                for row in rounds
                if _text(row.get("tournament_id")) == tid
                and _text(row.get("round_id")) == f"{tid}-Q{round_number}"
            )
            opened.update(
                status="open",
                opens_at_utc=now,
                published_at_utc=now,
                deadline_at_utc=deadline,
            )
            matches = [dict(row) for row in old_matches]
            for row in matches:
                if (
                    _text(row.get("tournament_id")) == tid
                    and _text(row.get("round_id")) == f"{tid}-Q{round_number}"
                ):
                    row.update(
                        status="published",
                        published_at_utc=now,
                        deadline_at_utc=deadline,
                    )
            await self.repository.persist_state(
                rounds,
                matches,
                previous_rounds=old_rounds,
                previous_matches=old_matches,
            )
            await self._audit(
                tid,
                str(actor_id),
                "swiss_draw_published",
                {"round_number": round_number, "deadline_at_utc": deadline},
                now,
            )
            published = tuple(
                sorted(
                    (
                        dict(row)
                        for row in matches
                        if _text(row.get("tournament_id")) == tid
                        and _text(row.get("round_id")) == f"{tid}-Q{round_number}"
                    ),
                    key=_match_sort_key,
                )
            )
            return QualificationSnapshot(opened, published)

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
            log.exception("Live Arena Swiss audit append failed • event=%s", event)


def pair_swiss(players: list[SwissPlayer], opponent_history: set[frozenset[str]]) -> list[SwissPair]:
    if len(players) % 2:
        raise SwissPairingError("Swiss pairing requires an even player count until bye support lands in 6B-4")
    ordered = sorted(players, key=lambda p: (p.ranking_index, p.user_id))
    group_members: dict[tuple[int, int], list[SwissPlayer]] = {}
    for player in ordered:
        group_members.setdefault(player.record, []).append(player)
    group_pos: dict[str, tuple[int, int]] = {}
    for members in group_members.values():
        for pos, player in enumerate(members):
            group_pos[player.user_id] = (pos, len(members))

    def edge_cost(a: SwissPlayer, b: SwissPlayer) -> tuple[int, int, int]:
        cross = int(a.record != b.record)
        float_penalty = 0
        high_low_penalty = 0
        if cross:
            stronger = a if a.wins > b.wins else b
            float_penalty = len(ordered) - stronger.ranking_index
        else:
            pos_a, size = group_pos[a.user_id]
            pos_b, _ = group_pos[b.user_id]
            high_low_penalty = abs((pos_a + pos_b) - (size - 1))
        return cross, float_penalty, high_low_penalty

    def valid(a: SwissPlayer, b: SwissPlayer) -> bool:
        if frozenset((a.user_id, b.user_id)) in opponent_history:
            return False
        return abs(a.wins - b.wins) <= 1

    @lru_cache(maxsize=None)
    def solve(mask: int):
        if mask == 0:
            return (0, 0, 0), ()
        first_bit = mask & -mask
        i = first_bit.bit_length() - 1
        remaining = mask ^ (1 << i)
        best = None
        bits = remaining
        while bits:
            bit = bits & -bits
            j = bit.bit_length() - 1
            bits ^= bit
            a, b = ordered[i], ordered[j]
            if not valid(a, b):
                continue
            sub = solve(remaining ^ (1 << j))
            if sub is None:
                continue
            edge = edge_cost(a, b)
            total = tuple(edge[k] + sub[0][k] for k in range(3))
            pair_key = tuple(sorted((a.user_id, b.user_id)))
            pairs = (pair_key,) + sub[1]
            candidate = (total, tuple(sorted(pairs)))
            if best is None or candidate < best:
                best = candidate
        return best

    solved = solve((1 << len(ordered)) - 1)
    if solved is None:
        raise SwissPairingError(_conflict_report(ordered, opponent_history))

    by_id = {player.user_id: player for player in ordered}
    result = []
    for a_id, b_id in solved[1]:
        a, b = by_id[a_id], by_id[b_id]
        if (b.ranking_index, b.user_id) < (a.ranking_index, a.user_id):
            a, b = b, a
        if a.record == b.record:
            rationale = (
                f"Swiss same-group pairing · record {a.record_label} · "
                f"ranking order #{a.rank} vs #{b.rank} · high-vs-low preference"
            )
        else:
            stronger, weaker = (a, b) if a.wins > b.wins else (b, a)
            rationale = (
                f"Swiss adjacent-group float · {stronger.display_name} ({stronger.record_label}) "
                f"floated down to {weaker.record_label} · no-rematch constraint preserved"
            )
        result.append(SwissPair(a, b, rationale))
    return sorted(result, key=lambda pair: min(pair.player_a.ranking_index, pair.player_b.ranking_index))


def source_fingerprint(matches, tournament_id: str, *, before_round: int) -> str:
    rows = []
    for row in matches:
        if _text(row.get("tournament_id")) != str(tournament_id):
            continue
        round_number = _round_number(_text(row.get("round_id")))
        if round_number is None or round_number >= before_round:
            continue
        if _text(row.get("status")) not in {"finalized", "forfeit", "double_forfeit", "bye"}:
            continue
        rows.append(
            (
                _text(row.get("round_id")),
                _text(row.get("match_id")),
                _text(row.get("status")),
                _text(row.get("final_result_type")),
                _text(row.get("final_score_a")),
                _text(row.get("final_score_b")),
                _text(row.get("final_winner_discord_user_id")),
            )
        )
    payload = json.dumps(sorted(rows), separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _players_from_roster(roster, standings) -> list[SwissPlayer]:
    standing_by_id = {entry.discord_user_id: entry for entry in standings}
    result = []
    for row in roster:
        uid = _text(row.get("discord_user_id"))
        standing = standing_by_id.get(uid)
        if standing is None:
            raise RegistrationError(f"Cannot Swiss-pair {uid}: no qualification standing exists yet")
        result.append(
            SwissPlayer(
                user_id=uid,
                display_name=_text(row.get("display_name_at_signup")) or uid,
                wins=standing.match_wins,
                losses=standing.match_losses,
                rank=standing.rank,
                ranking_index=0,
            )
        )
    result.sort(
        key=lambda p: (
            standing_by_id[p.user_id].rank,
            -standing_by_id[p.user_id].game_differential,
            -standing_by_id[p.user_id].strength_of_opponents,
            p.user_id,
        )
    )
    return [
        SwissPlayer(
            user_id=p.user_id,
            display_name=p.display_name,
            wins=p.wins,
            losses=p.losses,
            rank=p.rank,
            ranking_index=index,
        )
        for index, p in enumerate(result)
    ]


def _opponent_history(matches, tid: str, *, before_round: int) -> set[frozenset[str]]:
    history = set()
    for row in matches:
        if _text(row.get("tournament_id")) != tid:
            continue
        number = _round_number(_text(row.get("round_id")))
        if number is None or number >= before_round:
            continue
        a = _text(row.get("player_a_discord_user_id"))
        b = _text(row.get("player_b_discord_user_id"))
        if a and b:
            history.add(frozenset((a, b)))
    return history


def _validate_persisted_draw(matches, tid: str, round_number: int) -> None:
    round_id = f"{tid}-Q{round_number}"
    current = [
        row
        for row in matches
        if _text(row.get("tournament_id")) == tid
        and _text(row.get("round_id")) == round_id
    ]
    if not current:
        raise RegistrationError(f"Q{round_number} has no persisted matches")
    history = _opponent_history(matches, tid, before_round=round_number)
    seen = set()
    for row in current:
        a = _text(row.get("player_a_discord_user_id"))
        b = _text(row.get("player_b_discord_user_id"))
        if not a or not b or a == b:
            raise RegistrationError("Swiss draw contains an invalid matchup")
        if a in seen or b in seen:
            raise RegistrationError("Swiss draw contains a player more than once")
        seen.update((a, b))
        if frozenset((a, b)) in history:
            raise RegistrationError("Swiss draw contains a rematch and cannot be approved")


def _single_round(rounds, tid: str, round_number: int):
    round_id = f"{tid}-Q{round_number}"
    found = [
        row
        for row in rounds
        if _text(row.get("tournament_id")) == tid
        and _text(row.get("round_id")) == round_id
    ]
    if len(found) > 1:
        raise RegistrationError(f"ROUNDS contains duplicate {round_id}")
    return found[0] if found else None


def _round_number(round_id: str) -> int | None:
    marker = str(round_id or "").upper().rsplit("-Q", 1)
    if len(marker) != 2:
        return None
    try:
        return int(marker[1])
    except ValueError:
        return None


def _require_swiss_round(round_number: int) -> None:
    if int(round_number) not in _SWISS_ROUNDS:
        raise RegistrationError("Swiss pairing is only used for Qualification Rounds 2 and 3")


def _source_fingerprint_from_notes(notes: str) -> str:
    for line in str(notes or "").splitlines():
        if line.startswith(_SOURCE_NOTE_PREFIX):
            return line[len(_SOURCE_NOTE_PREFIX) :].strip()
    return ""


def _conflict_report(players, history) -> str:
    groups: dict[str, list[str]] = {}
    for player in players:
        groups.setdefault(player.record_label, []).append(player.display_name)
    group_text = "; ".join(f"{record}: {', '.join(names)}" for record, names in groups.items())
    blocked = 0
    for i, a in enumerate(players):
        for b in players[i + 1 :]:
            if frozenset((a.user_id, b.user_id)) in history:
                blocked += 1
    return (
        "No valid rematch-free Swiss pairing satisfies the adjacent-record-group rules. "
        f"Record groups: {group_text}. Prior-opponent blocks considered: {blocked}. "
        "Organizer review is required; hard constraints were not relaxed."
    )
