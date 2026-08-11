"""Top-8 freeze and fixed knockout bracket lifecycle for Live Arena PR 6B-3."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from modules.community.live_arena.competition import (
    MATCH_TERMINAL_STATUSES,
    calculate_qualification_standings,
)
from modules.community.live_arena.organizer import OrganizerService
from modules.community.live_arena.qualification import (
    MATCH_HEADERS,
    ROUND_HEADERS,
    QualificationRepository,
    QualificationService,
    QualificationSnapshot,
)
from modules.community.live_arena.registration import RegistrationError, _locks, utc_iso
from modules.community.live_arena.repository import LiveArenaRepository
from modules.community.live_arena.service import _text, load_config

log = logging.getLogger("c1c.community.live_arena.knockout")

SEED_ROUND_SUFFIX = "TOP8"
_SOURCE_PREFIX = "knockout_source="
KNOCKOUT = {
    "quarterfinal": {"suffix": "QF", "number": 4, "name": "Quarterfinals"},
    "semifinal": {"suffix": "SF", "number": 5, "name": "Semifinals"},
    "final": {"suffix": "F", "number": 6, "name": "Final"},
}


class KnockoutService:
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
        """Compatibility surface used by the generic round Discord reconciler."""
        service = OrganizerService(
            self.sheet_id,
            repository=self.registration_repository,
            clock=self.clock,
        )
        await service.initialize()
        return await service.context()

    async def record_overview_message_id(self, round_id: str, message_id: str) -> None:
        helper = QualificationService(
            self.sheet_id,
            registration_repository=self.registration_repository,
            qualification_repository=self.repository,
            clock=self.clock,
        )
        await helper.record_overview_message_id(round_id, message_id)

    async def seed_snapshot(self) -> list[dict[str, object]]:
        config = await load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        rounds = await self.repository.rounds()
        row = _seed_row(rounds, tid)
        return _read_seeds(row) if row else []

    async def snapshot(self, stage: str) -> QualificationSnapshot:
        meta = _meta(stage)
        config = await load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        round_id = f"{tid}-{meta['suffix']}"
        rounds = await self.repository.rounds()
        matches = await self.repository.matches()
        row = _round_by_id(rounds, tid, round_id)
        current = tuple(
            sorted(
                (
                    dict(item)
                    for item in matches
                    if _text(item.get("tournament_id")) == tid
                    and _text(item.get("round_id")) == round_id
                ),
                key=lambda item: int(_text(item.get("match_number")) or 0),
            )
        )
        return QualificationSnapshot(dict(row) if row else None, current)

    async def freeze_top8(self, actor_id: str) -> list[dict[str, object]]:
        """Approve/freeze the canonical Top 8. Organizers cannot reorder it."""
        config = await load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            existing = _seed_row(old_rounds, tid)
            if existing is not None:
                if _text(existing.get("status")) == "frozen":
                    return _read_seeds(existing)
                raise RegistrationError("Top 8 seed state already exists and requires organizer review")

            q3 = _round_by_id(old_rounds, tid, f"{tid}-Q3")
            if q3 is None or _text(q3.get("status")) != "closed":
                raise RegistrationError("Qualification Round 3 must be closed before Top 8 can be frozen")

            standings = calculate_qualification_standings(old_matches, tid)
            if len(standings) < 8:
                raise RegistrationError("At least eight completed qualification participants are required")
            top8 = standings[:8]
            affected = _competitive_ties(top8, standings)
            if affected:
                names = ", ".join(entry.display_name for entry in affected)
                raise RegistrationError(
                    "A qualification tie still affects Top 8 qualification or seeding. "
                    f"Resolve the required BO3 tiebreak before freezing seeds: {names}"
                )

            seeds = [
                {
                    "seed": index,
                    "discord_user_id": entry.discord_user_id,
                    "display_name": entry.display_name,
                    "qualification_rank": entry.rank,
                    "record": entry.match_record,
                }
                for index, entry in enumerate(top8, 1)
            ]
            now = utc_iso(self.clock().astimezone(UTC))
            row = _blank(ROUND_HEADERS)
            row.update(
                tournament_id=tid,
                round_id=f"{tid}-{SEED_ROUND_SUFFIX}",
                round_name="Top 8 Seeding",
                round_stage="top8_seeding",
                round_number="3",
                status="frozen",
                approved_at_utc=now,
                approved_by_discord_user_id=str(actor_id),
                completed_at_utc=now,
                notes=json.dumps({"seeds": seeds}, sort_keys=True, separators=(",", ":")),
            )
            rounds = [dict(item) for item in old_rounds] + [row]
            await self.repository.persist_rounds(rounds, previous_rounds=old_rounds)
            await self._audit(tid, actor_id, "top8_seeds_frozen", {"seeds": seeds}, now)
            return seeds

    async def generate_quarterfinal_preview(self, actor_id: str) -> QualificationSnapshot:
        seeds = await self.seed_snapshot()
        if len(seeds) != 8:
            raise RegistrationError("Freeze the final Top 8 before generating quarterfinals")
        pairs = [
            (seeds[0], seeds[7]),
            (seeds[3], seeds[4]),
            (seeds[1], seeds[6]),
            (seeds[2], seeds[5]),
        ]
        return await self._create_preview(
            actor_id,
            "quarterfinal",
            pairs,
            source=_seed_fingerprint(seeds),
            regenerate=False,
        )

    async def generate_next_preview(
        self,
        actor_id: str,
        stage: str,
        *,
        regenerate: bool = False,
    ) -> QualificationSnapshot:
        if stage not in {"semifinal", "final"}:
            raise RegistrationError("Next-round knockout preview is only for semifinal or final")
        config = await load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        previous_stage = "quarterfinal" if stage == "semifinal" else "semifinal"
        previous = KNOCKOUT[previous_stage]
        rounds = await self.repository.rounds()
        matches = await self.repository.matches()
        previous_round = _round_by_id(rounds, tid, f"{tid}-{previous['suffix']}")
        if previous_round is None or _text(previous_round.get("status")) != "closed":
            raise RegistrationError(f"{previous['name']} must be closed first")
        previous_matches = sorted(
            [
                row
                for row in matches
                if _text(row.get("tournament_id")) == tid
                and _text(row.get("round_id")) == f"{tid}-{previous['suffix']}"
            ],
            key=lambda row: int(_text(row.get("match_number")) or 0),
        )
        expected = 4 if stage == "semifinal" else 2
        if len(previous_matches) != expected or any(
            _text(row.get("status")) not in MATCH_TERMINAL_STATUSES
            for row in previous_matches
        ):
            raise RegistrationError(f"Every {previous['name']} matchup must be finalized")
        winners = [_winner(row) for row in previous_matches]
        if any(not uid for uid, _ in winners):
            raise RegistrationError(
                "A double-forfeit knockout result requires organizer resolution before advancement"
            )
        if stage == "semifinal":
            # Fixed slots: QF1 winner vs QF2 winner; QF3 winner vs QF4 winner.
            pairs = [
                (_player(winners[0]), _player(winners[1])),
                (_player(winners[2]), _player(winners[3])),
            ]
        else:
            pairs = [(_player(winners[0]), _player(winners[1]))]
        return await self._create_preview(
            actor_id,
            stage,
            pairs,
            source=_winner_fingerprint(previous_matches),
            regenerate=regenerate,
        )

    async def refresh_preview_if_stale(self, actor_id: str, stage: str) -> QualificationSnapshot | None:
        """Regenerate an unpublished downstream preview if upstream winners changed."""
        snapshot = await self.snapshot(stage)
        if snapshot.round_row is None or snapshot.status != "preview":
            return None
        if stage == "quarterfinal":
            current = _seed_fingerprint(await self.seed_snapshot())
        else:
            config = await load_config(self.sheet_id)
            tid = config["ACTIVE_TOURNAMENT_ID"]
            previous_stage = "quarterfinal" if stage == "semifinal" else "semifinal"
            previous_id = f"{tid}-{KNOCKOUT[previous_stage]['suffix']}"
            previous = sorted(
                [
                    row
                    for row in await self.repository.matches()
                    if _text(row.get("tournament_id")) == tid
                    and _text(row.get("round_id")) == previous_id
                ],
                key=lambda row: int(_text(row.get("match_number")) or 0),
            )
            if any(_text(row.get("status")) not in MATCH_TERMINAL_STATUSES for row in previous):
                return snapshot
            current = _winner_fingerprint(previous)
        expected = _source_from_notes(_text(snapshot.round_row.get("notes")))
        if expected == current:
            return snapshot
        if stage == "quarterfinal":
            return await self._regenerate_quarterfinal(actor_id)
        return await self.generate_next_preview(actor_id, stage, regenerate=True)

    async def approve_and_open(self, actor_id: str, stage: str) -> QualificationSnapshot:
        meta = _meta(stage)
        config = await load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            round_id = f"{tid}-{meta['suffix']}"
            target = _round_by_id(old_rounds, tid, round_id)
            if target is None or _text(target.get("status")) != "preview":
                raise RegistrationError(f"{meta['name']} must have a preview before approval")

            if stage == "quarterfinal":
                seed_row = _seed_row(old_rounds, tid)
                if seed_row is None or _text(seed_row.get("status")) != "frozen":
                    raise RegistrationError("Top 8 seeds must remain frozen before Quarterfinal publication")
                current_source = _seed_fingerprint(_read_seeds(seed_row))
            else:
                previous_stage = "quarterfinal" if stage == "semifinal" else "semifinal"
                previous_meta = KNOCKOUT[previous_stage]
                previous_round = _round_by_id(old_rounds, tid, f"{tid}-{previous_meta['suffix']}")
                if previous_round is None or _text(previous_round.get("status")) != "closed":
                    raise RegistrationError(f"{previous_meta['name']} must be closed before {meta['name']} can open")
                previous_matches = sorted(
                    [
                        row
                        for row in old_matches
                        if _text(row.get("tournament_id")) == tid
                        and _text(row.get("round_id")) == f"{tid}-{previous_meta['suffix']}"
                    ],
                    key=lambda row: int(_text(row.get("match_number")) or 0),
                )
                current_source = _winner_fingerprint(previous_matches)

            expected_source = _source_from_notes(_text(target.get("notes")))
            if not expected_source or expected_source != current_source:
                raise RegistrationError(
                    f"{meta['name']} preview is stale because the upstream competitive result changed. Refresh it before approval."
                )

            round_matches = [
                row
                for row in old_matches
                if _text(row.get("tournament_id")) == tid
                and _text(row.get("round_id")) == round_id
            ]
            _validate_knockout_shape(stage, round_matches)
            now_dt = self.clock().astimezone(UTC)
            now = utc_iso(now_dt)
            deadline = utc_iso(now_dt + timedelta(days=6))
            rounds = [dict(row) for row in old_rounds]
            opened = _round_by_id(rounds, tid, round_id)
            opened.update(
                status="open",
                approved_at_utc=now,
                approved_by_discord_user_id=str(actor_id),
                opens_at_utc=now,
                published_at_utc=now,
                deadline_at_utc=deadline,
            )
            matches = [dict(row) for row in old_matches]
            for row in matches:
                if (
                    _text(row.get("tournament_id")) == tid
                    and _text(row.get("round_id")) == round_id
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
                actor_id,
                "knockout_round_opened",
                {"stage": stage, "round_id": round_id},
                now,
            )
            return QualificationSnapshot(
                opened,
                tuple(
                    row
                    for row in matches
                    if _text(row.get("round_id")) == round_id
                    and _text(row.get("tournament_id")) == tid
                ),
            )

    async def complete_tournament(self, actor_id: str) -> dict[str, object]:
        """Validate the competitive completion gate before lifecycle transition."""
        config = await load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            rounds = await self.repository.rounds()
            matches = await self.repository.matches()
            final_round = _round_by_id(rounds, tid, f"{tid}-F")
            if final_round is None or _text(final_round.get("status")) != "closed":
                raise RegistrationError("Close the finalized Final before completing the tournament")
            final_matches = [
                row
                for row in matches
                if _text(row.get("tournament_id")) == tid
                and _text(row.get("round_id")) == f"{tid}-F"
            ]
            if len(final_matches) != 1 or _text(final_matches[0].get("status")) not in MATCH_TERMINAL_STATUSES:
                raise RegistrationError("The Final must be finalized before tournament completion")
            champion_id, champion_name = _winner(final_matches[0])
            if not champion_id:
                raise RegistrationError("Tournament completion requires a champion")
            runner_id = _other_player(final_matches[0], champion_id)
            runner_name = _display_name(final_matches[0], runner_id)
            now = utc_iso(self.clock().astimezone(UTC))
            summary = {
                "tournament_id": tid,
                "champion_discord_user_id": champion_id,
                "champion_display_name": champion_name,
                "runner_up_discord_user_id": runner_id,
                "runner_up_display_name": runner_name,
                "completed_at_utc": now,
            }
            await self._audit(
                tid,
                actor_id,
                "knockout_competition_completed",
                summary,
                now,
            )
            return summary

    async def _regenerate_quarterfinal(self, actor_id: str) -> QualificationSnapshot:
        seeds = await self.seed_snapshot()
        pairs = [
            (seeds[0], seeds[7]),
            (seeds[3], seeds[4]),
            (seeds[1], seeds[6]),
            (seeds[2], seeds[5]),
        ]
        return await self._create_preview(
            actor_id,
            "quarterfinal",
            pairs,
            source=_seed_fingerprint(seeds),
            regenerate=True,
        )

    async def _create_preview(
        self,
        actor_id: str,
        stage: str,
        pairs,
        *,
        source: str,
        regenerate: bool,
    ) -> QualificationSnapshot:
        meta = _meta(stage)
        config = await load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            round_id = f"{tid}-{meta['suffix']}"
            existing = _round_by_id(old_rounds, tid, round_id)
            if existing is not None and not regenerate:
                raise RegistrationError(f"{meta['name']} already exists")
            if existing is not None and _text(existing.get("status")) != "preview":
                raise RegistrationError(f"{meta['name']} can only be regenerated while it is a preview")

            now = utc_iso(self.clock().astimezone(UTC))
            round_row = _blank(ROUND_HEADERS)
            if existing is not None:
                round_row.update(dict(existing))
            round_row.update(
                tournament_id=tid,
                round_id=round_id,
                round_name=meta["name"],
                round_stage=stage,
                round_number=str(meta["number"]),
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
                notes=(
                    f"{_SOURCE_PREFIX}{source}\n"
                    "Fixed knockout bracket; organizer approval required before publication"
                ),
            )
            generated = []
            for number, (a, b) in enumerate(pairs, 1):
                row = _blank(MATCH_HEADERS)
                row.update(
                    tournament_id=tid,
                    round_id=round_id,
                    match_id=f"{round_id}-M{number:02d}",
                    match_number=str(number),
                    player_a_discord_user_id=str(a["discord_user_id"]),
                    player_a_display_name=str(a["display_name"]),
                    player_b_discord_user_id=str(b["discord_user_id"]),
                    player_b_display_name=str(b["display_name"]),
                    status="preview",
                    notes="Fixed knockout bracket slot",
                )
                generated.append(row)

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
            ] + generated
            await self.repository.persist_state(
                rounds,
                matches,
                previous_rounds=old_rounds,
                previous_matches=old_matches,
            )
            await self._audit(
                tid,
                actor_id,
                "knockout_preview_regenerated" if regenerate else "knockout_preview_generated",
                {"stage": stage, "source": source},
                now,
            )
            return QualificationSnapshot(round_row, tuple(generated))

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
            log.exception("Live Arena knockout audit append failed • event=%s", event)


def _competitive_ties(top8, standings):
    """Any unresolved tie touching seeds 1-8 is competitively meaningful."""
    affected = []
    top_ids = {entry.discord_user_id for entry in top8}
    for entry in standings:
        if not entry.tied:
            continue
        # A tied player inside Top 8 affects seeding. A tied player outside may affect
        # the cutoff when sharing the same competition rank with a Top-8 entrant.
        if entry.discord_user_id in top_ids or any(
            top.rank == entry.rank and top.tied for top in top8
        ):
            affected.append(entry)
    unique = {}
    for entry in affected:
        unique[entry.discord_user_id] = entry
    return list(unique.values())


def _seed_row(rounds, tid):
    return _round_by_id(rounds, tid, f"{tid}-{SEED_ROUND_SUFFIX}")


def _read_seeds(row):
    try:
        payload = json.loads(_text(row.get("notes")) or "{}")
        seeds = payload.get("seeds", [])
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RegistrationError("Frozen Top 8 seed data is invalid") from exc
    if len(seeds) != 8:
        raise RegistrationError("Frozen Top 8 seed data must contain exactly eight players")
    expected = list(range(1, 9))
    actual = [int(seed.get("seed", 0)) for seed in seeds]
    if actual != expected:
        raise RegistrationError("Frozen Top 8 seed order is invalid")
    ids = [_text(seed.get("discord_user_id")) for seed in seeds]
    if any(not uid for uid in ids) or len(set(ids)) != 8:
        raise RegistrationError("Frozen Top 8 contains duplicate or missing players")
    return seeds


def _round_by_id(rounds, tid, round_id):
    found = [
        row
        for row in rounds
        if _text(row.get("tournament_id")) == tid
        and _text(row.get("round_id")) == round_id
    ]
    if len(found) > 1:
        raise RegistrationError(f"ROUNDS contains duplicate {round_id}")
    return found[0] if found else None


def _winner(row):
    uid = _text(row.get("final_winner_discord_user_id"))
    if not uid:
        return "", ""
    if uid == _text(row.get("player_a_discord_user_id")):
        return uid, _text(row.get("player_a_display_name"))
    if uid == _text(row.get("player_b_discord_user_id")):
        return uid, _text(row.get("player_b_display_name"))
    raise RegistrationError("Final winner is not one of the matchup players")


def _other_player(row, winner_id):
    a = _text(row.get("player_a_discord_user_id"))
    b = _text(row.get("player_b_discord_user_id"))
    if winner_id == a:
        return b
    if winner_id == b:
        return a
    raise RegistrationError("Winner is not part of the matchup")


def _display_name(row, user_id: str) -> str:
    if user_id == _text(row.get("player_a_discord_user_id")):
        return _text(row.get("player_a_display_name"))
    if user_id == _text(row.get("player_b_discord_user_id")):
        return _text(row.get("player_b_display_name"))
    return user_id


def _player(winner):
    return {"discord_user_id": winner[0], "display_name": winner[1]}


def _validate_knockout_shape(stage, matches):
    expected = {"quarterfinal": 4, "semifinal": 2, "final": 1}[stage]
    if len(matches) != expected:
        raise RegistrationError(
            f"{_meta(stage)['name']} must contain exactly {expected} matchup(s)"
        )
    seen = set()
    for row in matches:
        a = _text(row.get("player_a_discord_user_id"))
        b = _text(row.get("player_b_discord_user_id"))
        if not a or not b or a == b or a in seen or b in seen:
            raise RegistrationError(
                "Knockout preview contains an invalid or duplicate player placement"
            )
        seen.update((a, b))


def _seed_fingerprint(seeds) -> str:
    payload = [(int(seed["seed"]), _text(seed["discord_user_id"])) for seed in seeds]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()[:24]


def _winner_fingerprint(matches) -> str:
    payload = [
        (
            _text(row.get("match_id")),
            _text(row.get("status")),
            _text(row.get("final_result_type")),
            _text(row.get("final_winner_discord_user_id")),
            _text(row.get("final_score_a")),
            _text(row.get("final_score_b")),
        )
        for row in matches
    ]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":")).encode()).hexdigest()[:24]


def _source_from_notes(notes: str) -> str:
    for line in str(notes or "").splitlines():
        if line.startswith(_SOURCE_PREFIX):
            return line[len(_SOURCE_PREFIX) :].strip()
    return ""


def _meta(stage):
    if stage not in KNOCKOUT:
        raise RegistrationError("Unknown knockout stage")
    return KNOCKOUT[stage]


def _blank(headers):
    return {header: "" for header in headers}
