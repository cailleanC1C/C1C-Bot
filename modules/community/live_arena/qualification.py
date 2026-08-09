"""Qualification-round draw, persistence, and publication state for Live Arena."""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from uuid import uuid4

from shared.sheets.async_core import acall_with_backoff, afetch_values, aget_worksheet

from modules.community.live_arena.organizer import OrganizerService
from modules.community.live_arena.registration import RegistrationError, _locks, utc_iso
from modules.community.live_arena.repository import LiveArenaRepository
from modules.community.live_arena.service import (
    CONFIG_HEADERS,
    CONFIG_TAB,
    LiveArenaConfigError,
    _enabled,
    _rows,
    _text,
    load_config,
)

log = logging.getLogger("c1c.community.live_arena.qualification")

QUALIFICATION_CONFIG_KEYS = (
    "ROUNDS_TAB",
    "MATCHES_TAB",
    "MATCH_FORUM_CHANNEL_ID",
    "ROUND_OVERVIEW_CHANNEL_ID",
)
ROUND_HEADERS = (
    "tournament_id",
    "round_id",
    "round_name",
    "round_stage",
    "round_number",
    "status",
    "opens_at_utc",
    "deadline_at_utc",
    "published_at_utc",
    "overview_message_id",
    "completed_at_utc",
    "generated_at_utc",
    "generated_by_discord_user_id",
    "approved_at_utc",
    "approved_by_discord_user_id",
    "notes",
)
MATCH_HEADERS = (
    "tournament_id",
    "round_id",
    "match_id",
    "match_number",
    "player_a_discord_user_id",
    "player_a_display_name",
    "player_b_discord_user_id",
    "player_b_display_name",
    "status",
    "shared_slot_ids_csv",
    "has_scheduling_conflict",
    "scheduling_conflict_notes",
    "published_at_utc",
    "deadline_at_utc",
    "thread_id",
    "reported_by_discord_user_id",
    "reported_score_a",
    "reported_score_b",
    "reported_at_utc",
    "confirm_due_at_utc",
    "confirmed_by_discord_user_id",
    "confirmed_at_utc",
    "disputed_by_discord_user_id",
    "disputed_at_utc",
    "scheduling_problem_reported_by_discord_user_id",
    "scheduling_problem_reported_at_utc",
    "extension_count",
    "final_result_type",
    "final_score_a",
    "final_score_b",
    "final_winner_discord_user_id",
    "finalized_by_discord_user_id",
    "finalized_at_utc",
    "notes",
)


@dataclass(frozen=True)
class QualificationSnapshot:
    round_row: dict[str, object] | None
    matches: tuple[dict[str, object], ...]

    @property
    def status(self) -> str:
        return _text(self.round_row.get("status")) if self.round_row else ""


async def load_qualification_config(sheet_id: str) -> dict[str, str]:
    """Load only the frozen qualification routing contract from CONFIG."""
    matrix = await afetch_values(sheet_id, CONFIG_TAB) or []
    rows = _rows(matrix, CONFIG_HEADERS, CONFIG_TAB)
    result: dict[str, str] = {}
    for key in QUALIFICATION_CONFIG_KEYS:
        matches = [row for row in rows if _text(row["Key"]) == key]
        if len(matches) != 1:
            raise LiveArenaConfigError(f"CONFIG: key {key} must occur exactly once")
        result[key] = _text(matches[0]["Value"])
        if not result[key]:
            raise LiveArenaConfigError(f"CONFIG: missing required key {key}")
    try:
        int(result["MATCH_FORUM_CHANNEL_ID"])
        int(result["ROUND_OVERVIEW_CHANNEL_ID"])
    except ValueError as exc:
        raise LiveArenaConfigError(
            "CONFIG: qualification Discord channel IDs must be numeric"
        ) from exc
    return result


class QualificationRepository:
    """Exact-schema workbook adapter for ROUNDS and MATCHES."""

    def __init__(self, sheet_id: str) -> None:
        self.sheet_id = sheet_id
        self.config: dict[str, str] = {}

    async def initialize(self) -> None:
        self.config = await load_qualification_config(self.sheet_id)
        await self.rounds()
        await self.matches()

    async def _read(self, key: str, headers: tuple[str, ...]):
        tab = self.config[key]
        return _rows(await afetch_values(self.sheet_id, tab) or [], headers, tab)

    async def rounds(self) -> list[dict[str, object]]:
        return await self._read("ROUNDS_TAB", ROUND_HEADERS)

    async def matches(self) -> list[dict[str, object]]:
        return await self._read("MATCHES_TAB", MATCH_HEADERS)

    async def persist_state(
        self,
        rounds,
        matches,
        *,
        previous_rounds,
        previous_matches,
    ) -> None:
        await self._persist_tables(
            (
                ("ROUNDS_TAB", ROUND_HEADERS, rounds, previous_rounds),
                ("MATCHES_TAB", MATCH_HEADERS, matches, previous_matches),
            )
        )

    async def persist_rounds(self, rounds, *, previous_rounds) -> None:
        await self._persist_tables(
            (("ROUNDS_TAB", ROUND_HEADERS, rounds, previous_rounds),)
        )

    async def persist_matches(self, matches, *, previous_matches) -> None:
        await self._persist_tables(
            (("MATCHES_TAB", MATCH_HEADERS, matches, previous_matches),)
        )

    async def _persist_tables(self, tables) -> None:
        first_key = tables[0][0]
        worksheet = await aget_worksheet(self.sheet_id, self.config[first_key])
        spreadsheet = worksheet.spreadsheet
        update = self._batch_body(tables, use_previous=False)
        rollback = self._batch_body(tables, use_previous=True)
        try:
            await acall_with_backoff(spreadsheet.values_batch_update, body=update)
        except Exception as write_error:
            try:
                await acall_with_backoff(spreadsheet.values_batch_update, body=rollback)
            except Exception as rollback_error:
                raise QualificationPersistenceError(
                    write_error=write_error, rollback_error=rollback_error
                ) from rollback_error
            raise

    def _batch_body(self, tables, *, use_previous: bool):
        data = []
        for key, headers, current, previous in tables:
            rows = previous if use_previous else current
            row_count = max(len(current), len(previous), 1)
            values = [
                [str(row.get(header, "") or "") for header in headers] for row in rows
            ]
            values.extend([[""] * len(headers) for _ in range(row_count - len(values))])
            tab = self.config[key].replace("'", "''")
            data.append(
                {
                    "range": f"'{tab}'!A2:{_column(len(headers))}{row_count + 1}",
                    "values": values,
                }
            )
        return {"valueInputOption": "RAW", "data": data}


def _column(number: int) -> str:
    if number <= 0:
        raise LiveArenaConfigError("Live Arena table width must be positive")
    result = ""
    while number:
        number, remainder = divmod(number - 1, 26)
        result = chr(65 + remainder) + result
    return result


@dataclass
class QualificationPersistenceError(RuntimeError):
    write_error: Exception
    rollback_error: Exception

    def __str__(self) -> str:
        return (
            "Live Arena qualification persistence and compensation both failed: "
            f"write={self.write_error!r}; rollback={self.rollback_error!r}"
        )


class QualificationService:
    def __init__(
        self,
        sheet_id: str,
        registration_repository=None,
        qualification_repository=None,
        clock=None,
        rng=None,
    ) -> None:
        self.sheet_id = sheet_id
        self.registration_repository = registration_repository or LiveArenaRepository(sheet_id)
        self.repository = qualification_repository or QualificationRepository(sheet_id)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.rng = rng or random.SystemRandom()

    async def initialize(self) -> None:
        await self.registration_repository.initialize()
        await self.repository.initialize()

    async def context(self):
        service = OrganizerService(
            self.sheet_id,
            repository=self.registration_repository,
            clock=self.clock,
        )
        return await service.context()

    async def snapshot(self) -> QualificationSnapshot:
        config = await load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        rounds = await self.repository.rounds()
        matches = await self.repository.matches()
        round_row = _single_q1_round(rounds, tid)
        round_id = f"{tid}-Q1"
        qmatches = tuple(
            dict(row)
            for row in matches
            if _text(row["tournament_id"]) == tid
            and _text(row["round_id"]) == round_id
        )
        qmatches = tuple(sorted(qmatches, key=_match_sort_key))
        return QualificationSnapshot(dict(round_row) if round_row else None, qmatches)

    async def generate_draw(self, actor_id: str) -> QualificationSnapshot:
        return await self._generate(actor_id, regenerate=False)

    async def regenerate_draw(self, actor_id: str) -> QualificationSnapshot:
        return await self._generate(actor_id, regenerate=True)

    async def _generate(self, actor_id: str, *, regenerate: bool) -> QualificationSnapshot:
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            _, (_, tournament), _, slots = await self.context()
            participants = await self.registration_repository.participants()
            roster = _confirmed_roster(participants, tid)
            _validate_draw_roster(tournament, roster)

            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            existing = _single_q1_round(old_rounds, tid)
            if regenerate:
                if existing is None or _text(existing["status"]) != "proposed":
                    raise RegistrationError(
                        "Q1 can only be regenerated while its draw is proposed"
                    )
            elif existing is not None:
                if _text(existing["status"]) in {"active", "completed"}:
                    raise RegistrationError("Q1 has already started")
                raise RegistrationError(
                    "Q1 draw already exists; use Regenerate Draw instead"
                )

            availability = await self.registration_repository.availability()
            pairings = self._optimal_pairings(roster, availability, slots, tid)
            now = utc_iso(self.clock())
            round_id = f"{tid}-Q1"
            round_row = _blank(ROUND_HEADERS)
            if existing is not None:
                round_row.update(dict(existing))
            round_row.update(
                tournament_id=tid,
                round_id=round_id,
                round_name="Qualification Round 1",
                round_stage="qualification",
                round_number="1",
                status="proposed",
                opens_at_utc="",
                deadline_at_utc="",
                published_at_utc="",
                overview_message_id="",
                completed_at_utc="",
                generated_at_utc=now,
                generated_by_discord_user_id=str(actor_id),
                approved_at_utc="",
                approved_by_discord_user_id="",
            )
            new_rounds = [
                dict(row)
                for row in old_rounds
                if not (
                    _text(row["tournament_id"]) == tid
                    and _text(row["round_id"]) == round_id
                )
            ]
            new_rounds.append(round_row)
            generated_matches = [
                _match_row(tid, round_id, index, player_a, player_b, shared)
                for index, (player_a, player_b, shared) in enumerate(pairings, 1)
            ]
            new_matches = [
                dict(row)
                for row in old_matches
                if not (
                    _text(row["tournament_id"]) == tid
                    and _text(row["round_id"]) == round_id
                )
            ] + generated_matches
            await self.repository.persist_state(
                new_rounds,
                new_matches,
                previous_rounds=old_rounds,
                previous_matches=old_matches,
            )
            await self._audit(
                tid,
                actor_id,
                "q1_draw_regenerated" if regenerate else "q1_draw_generated",
                {
                    "match_count": len(generated_matches),
                    "scheduling_conflicts": sum(
                        _text(row["has_scheduling_conflict"]).lower() == "true"
                        for row in generated_matches
                    ),
                },
                now,
            )
            return QualificationSnapshot(round_row, tuple(generated_matches))

    def _optimal_pairings(self, roster, availability, slots, tid):
        enabled = {
            _text(row["slot_id"])
            for row in slots
            if _enabled(row["enabled"])
        }
        rank = {
            _text(row["slot_id"]): (
                _safe_int(row.get("sort_order")),
                _text(row["slot_id"]),
            )
            for row in slots
            if _text(row["slot_id"]) in enabled
        }
        selected: dict[str, set[str]] = {
            _text(player["discord_user_id"]): set() for player in roster
        }
        for row in availability:
            if _text(row["tournament_id"]) != tid:
                continue
            uid = _text(row["discord_user_id"])
            slot_id = _text(row["slot_id"])
            if uid in selected and slot_id in enabled:
                selected[uid].add(slot_id)

        shared: dict[tuple[int, int], tuple[str, ...]] = {}
        for i in range(len(roster)):
            for j in range(i + 1, len(roster)):
                ids = selected[_text(roster[i]["discord_user_id"])] & selected[
                    _text(roster[j]["discord_user_id"])
                ]
                shared[(i, j)] = tuple(sorted(ids, key=lambda slot: rank[slot]))

        @lru_cache(maxsize=None)
        def best(mask: int) -> int:
            if not mask:
                return 0
            low = mask & -mask
            i = low.bit_length() - 1
            remaining = mask ^ (1 << i)
            score = len(roster) + 1
            bits = remaining
            while bits:
                low_j = bits & -bits
                j = low_j.bit_length() - 1
                pair = (i, j) if i < j else (j, i)
                conflict = 0 if shared[pair] else 1
                score = min(score, conflict + best(remaining ^ (1 << j)))
                bits ^= low_j
            return score

        def choose(mask: int):
            if not mask:
                return []
            low = mask & -mask
            i = low.bit_length() - 1
            remaining = mask ^ (1 << i)
            target = best(mask)
            candidates = []
            bits = remaining
            while bits:
                low_j = bits & -bits
                j = low_j.bit_length() - 1
                pair = (i, j) if i < j else (j, i)
                conflict = 0 if shared[pair] else 1
                if conflict + best(remaining ^ (1 << j)) == target:
                    candidates.append(j)
                bits ^= low_j
            j = self.rng.choice(candidates)
            pair = (i, j) if i < j else (j, i)
            return [(roster[i], roster[j], shared[pair])] + choose(
                remaining ^ (1 << j)
            )

        return choose((1 << len(roster)) - 1)

    async def swap_players(
        self, actor_id: str, first_user_id: str, second_user_id: str
    ) -> QualificationSnapshot:
        if str(first_user_id) == str(second_user_id):
            raise RegistrationError("Choose two different players to swap")
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        round_id = f"{tid}-Q1"
        async with _locks[(self.sheet_id, tid)]:
            _, (_, tournament), _, slots = await self.context()
            if _text(tournament["status"]) != "signup_closed":
                raise RegistrationError("Q1 draw changes require closed registration")
            rounds = await self.repository.rounds()
            round_row = _single_q1_round(rounds, tid)
            if round_row is None or _text(round_row["status"]) != "proposed":
                raise RegistrationError("Players can only be swapped in a proposed Q1 draw")
            old_matches = await self.repository.matches()
            matches = [dict(row) for row in old_matches]
            q_indices = [
                index
                for index, row in enumerate(matches)
                if _text(row["tournament_id"]) == tid
                and _text(row["round_id"]) == round_id
            ]
            first = _find_player_location(matches, q_indices, str(first_user_id))
            second = _find_player_location(matches, q_indices, str(second_user_id))
            if first is None or second is None:
                raise RegistrationError(
                    "Both selected players must belong to the proposed Q1 draw"
                )
            if first[0] == second[0]:
                raise RegistrationError("Choose players from two different matches")
            _swap_match_player(
                matches[first[0]], first[1], matches[second[0]], second[1]
            )

            availability = await self.registration_repository.availability()
            selected = _enabled_availability(availability, slots, tid)
            rank = _slot_rank(slots)
            for index in {first[0], second[0]}:
                _refresh_match_availability(matches[index], selected, rank)

            await self.repository.persist_matches(
                matches, previous_matches=old_matches
            )
            now = utc_iso(self.clock())
            await self._audit(
                tid,
                actor_id,
                "q1_players_swapped",
                {"player_1": str(first_user_id), "player_2": str(second_user_id)},
                now,
            )
            qmatches = tuple(
                sorted((matches[index] for index in q_indices), key=_match_sort_key)
            )
            return QualificationSnapshot(dict(round_row), qmatches)

    async def approve_draw(self, actor_id: str) -> QualificationSnapshot:
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        round_id = f"{tid}-Q1"
        async with _locks[(self.sheet_id, tid)]:
            _, (_, tournament), _, _ = await self.context()
            participants = await self.registration_repository.participants()
            roster = _confirmed_roster(participants, tid)
            _validate_draw_roster(tournament, roster)

            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            round_row = _single_q1_round(old_rounds, tid)
            if round_row is None or _text(round_row["status"]) != "proposed":
                raise RegistrationError("Only a proposed Q1 draw can be approved")
            qmatches = [
                dict(row)
                for row in old_matches
                if _text(row["tournament_id"]) == tid
                and _text(row["round_id"]) == round_id
            ]
            proposed_ids = [
                _text(row[key])
                for row in qmatches
                for key in (
                    "player_a_discord_user_id",
                    "player_b_discord_user_id",
                )
            ]
            roster_ids = {_text(row["discord_user_id"]) for row in roster}
            if (
                len(proposed_ids) != len(roster_ids)
                or len(set(proposed_ids)) != len(proposed_ids)
                or set(proposed_ids) != roster_ids
            ):
                raise RegistrationError(
                    "The confirmed roster changed after this draw was generated. "
                    "Regenerate Q1 before approving it."
                )

            now_dt = self.clock().astimezone(UTC)
            now = utc_iso(now_dt)
            deadline = utc_iso(now_dt + timedelta(days=6))
            new_rounds = [dict(row) for row in old_rounds]
            target = next(
                row
                for row in new_rounds
                if _text(row["tournament_id"]) == tid
                and _text(row["round_id"]) == round_id
            )
            target.update(
                status="active",
                opens_at_utc=now,
                deadline_at_utc=deadline,
                published_at_utc=now,
                approved_at_utc=now,
                approved_by_discord_user_id=str(actor_id),
            )
            new_matches = [dict(row) for row in old_matches]
            for row in new_matches:
                if (
                    _text(row["tournament_id"]) == tid
                    and _text(row["round_id"]) == round_id
                ):
                    row.update(
                        status="published",
                        published_at_utc=now,
                        deadline_at_utc=deadline,
                    )
            await self.repository.persist_state(
                new_rounds,
                new_matches,
                previous_rounds=old_rounds,
                previous_matches=old_matches,
            )
            await self._audit(
                tid,
                actor_id,
                "q1_draw_approved",
                {"match_count": len(qmatches), "deadline_at_utc": deadline},
                now,
            )
            published = tuple(
                sorted(
                    (
                        row
                        for row in new_matches
                        if _text(row["tournament_id"]) == tid
                        and _text(row["round_id"]) == round_id
                    ),
                    key=_match_sort_key,
                )
            )
            return QualificationSnapshot(target, published)

    async def record_thread_id(
        self, match_id: str, thread_id: str
    ) -> dict[str, object]:
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_matches = await self.repository.matches()
            matches = [dict(row) for row in old_matches]
            targets = [
                row
                for row in matches
                if _text(row["tournament_id"]) == tid
                and _text(row["match_id"]) == str(match_id)
            ]
            if len(targets) != 1:
                raise RegistrationError("Published match could not be resolved")
            targets[0]["thread_id"] = str(thread_id)
            await self.repository.persist_matches(
                matches, previous_matches=old_matches
            )
            return targets[0]

    async def record_overview_message_id(
        self, round_id: str, message_id: str
    ) -> dict[str, object]:
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_rounds = await self.repository.rounds()
            rounds = [dict(row) for row in old_rounds]
            targets = [
                row
                for row in rounds
                if _text(row["tournament_id"]) == tid
                and _text(row["round_id"]) == str(round_id)
            ]
            if len(targets) != 1:
                raise RegistrationError("Published round could not be resolved")
            targets[0]["overview_message_id"] = str(message_id)
            await self.repository.persist_rounds(rounds, previous_rounds=old_rounds)
            return targets[0]

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
                "❌ Live Arena qualification audit append failed • tournament=%s • event=%s",
                tid,
                event,
            )


def _blank(headers):
    return {header: "" for header in headers}


def _single_q1_round(rounds, tid):
    round_id = f"{tid}-Q1"
    matches = [
        row
        for row in rounds
        if _text(row["tournament_id"]) == tid
        and _text(row["round_id"]) == round_id
    ]
    if len(matches) > 1:
        raise LiveArenaConfigError(f"ROUNDS: duplicate round_id {round_id}")
    return matches[0] if matches else None


def _confirmed_roster(participants, tid):
    return [
        dict(row)
        for row in participants
        if _text(row["tournament_id"]) == tid and _text(row["status"]) == "confirmed"
    ]


def _validate_draw_roster(tournament, roster):
    if _text(tournament["status"]) != "signup_closed":
        raise RegistrationError("Q1 can only be generated after registration is closed")
    minimum = int(_text(tournament["min_participants"]))
    if len(roster) < minimum:
        raise RegistrationError(
            f"Q1 requires at least {minimum} confirmed participants; currently {len(roster)}"
        )
    if len(roster) % 2:
        raise RegistrationError(
            "Q1 requires an even confirmed roster. No bye or automatic removal will be created."
        )


def _safe_int(value) -> int:
    try:
        return int(_text(value))
    except ValueError:
        return 1_000_000


def _slot_rank(slots):
    return {
        _text(row["slot_id"]): (
            _safe_int(row.get("sort_order")),
            _text(row["slot_id"]),
        )
        for row in slots
        if _enabled(row["enabled"])
    }


def _enabled_availability(availability, slots, tid):
    enabled = set(_slot_rank(slots))
    selected: dict[str, set[str]] = {}
    for row in availability:
        if _text(row["tournament_id"]) != tid:
            continue
        slot_id = _text(row["slot_id"])
        if slot_id in enabled:
            selected.setdefault(_text(row["discord_user_id"]), set()).add(slot_id)
    return selected


def _shared_slots(player_a, player_b, selected, rank):
    shared = selected.get(str(player_a), set()) & selected.get(str(player_b), set())
    return tuple(sorted(shared, key=lambda slot: rank[slot]))


def _match_row(tid, round_id, number, player_a, player_b, shared):
    row = _blank(MATCH_HEADERS)
    row.update(
        tournament_id=tid,
        round_id=round_id,
        match_id=f"{round_id}-M{number:02d}",
        match_number=str(number),
        player_a_discord_user_id=_text(player_a["discord_user_id"]),
        player_a_display_name=_text(player_a["display_name_at_signup"]),
        player_b_discord_user_id=_text(player_b["discord_user_id"]),
        player_b_display_name=_text(player_b["display_name_at_signup"]),
        status="proposed",
        shared_slot_ids_csv=",".join(shared),
        has_scheduling_conflict="FALSE" if shared else "TRUE",
        scheduling_conflict_notes=""
        if shared
        else "No shared enabled availability slot",
        extension_count="0",
    )
    return row


def _match_sort_key(row):
    return (_safe_int(row.get("match_number")), _text(row.get("match_id")))


def _find_player_location(matches, q_indices, user_id):
    found = []
    for index in q_indices:
        row = matches[index]
        if _text(row["player_a_discord_user_id"]) == user_id:
            found.append((index, "a"))
        if _text(row["player_b_discord_user_id"]) == user_id:
            found.append((index, "b"))
    if len(found) > 1:
        raise LiveArenaConfigError(
            f"MATCHES: player occurs more than once in Q1: {user_id}"
        )
    return found[0] if found else None


def _swap_match_player(first, first_side, second, second_side):
    first_id = f"player_{first_side}_discord_user_id"
    first_name = f"player_{first_side}_display_name"
    second_id = f"player_{second_side}_discord_user_id"
    second_name = f"player_{second_side}_display_name"
    first[first_id], second[second_id] = second[second_id], first[first_id]
    first[first_name], second[second_name] = second[second_name], first[first_name]


def _refresh_match_availability(match, selected, rank):
    shared = _shared_slots(
        _text(match["player_a_discord_user_id"]),
        _text(match["player_b_discord_user_id"]),
        selected,
        rank,
    )
    match["shared_slot_ids_csv"] = ",".join(shared)
    match["has_scheduling_conflict"] = "FALSE" if shared else "TRUE"
    match["scheduling_conflict_notes"] = (
        "" if shared else "No shared enabled availability slot"
    )
