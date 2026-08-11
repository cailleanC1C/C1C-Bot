"""Constrained organizer repair for invalid Q2/Q3 Swiss previews.

Manual edits are intentionally narrow: a valid Swiss preview cannot be reshuffled by
preference. Only players already implicated in a hard-rule-invalid preview may be
repaired, and the complete candidate draw is revalidated before persistence.
"""

from __future__ import annotations

import json
from datetime import UTC
from uuid import uuid4

from modules.community.live_arena.competition import calculate_qualification_standings
from modules.community.live_arena.qualification import (
    _enabled_availability,
    _match_row,
    _match_sort_key,
    _shared_slots,
    _slot_rank,
)
from modules.community.live_arena.registration import RegistrationError, _locks, utc_iso
from modules.community.live_arena.service import _text, load_config
from modules.community.live_arena.swiss import (
    SwissQualificationService,
    _opponent_history,
    _players_from_roster,
    source_fingerprint,
)

_SOURCE_NOTE_PREFIX = "swiss_source_fingerprint="


async def repair_preview_pairings(
    service: SwissQualificationService,
    actor_id: str,
    round_number: int,
    replacement_pairs: list[tuple[str, str]],
):
    """Repair only the hard-rule-conflicted subset of an existing Swiss preview."""
    if round_number not in {2, 3}:
        raise RegistrationError("Manual Swiss repair is only available for Q2/Q3")

    config = await load_config(service.sheet_id)
    tid = config["ACTIVE_TOURNAMENT_ID"]
    round_id = f"{tid}-Q{round_number}"
    async with _locks[(service.sheet_id, tid)]:
        old_rounds = await service.repository.rounds()
        old_matches = await service.repository.matches()
        round_rows = [
            row
            for row in old_rounds
            if _text(row.get("tournament_id")) == tid
            and _text(row.get("round_id")) == round_id
        ]
        if len(round_rows) != 1 or _text(round_rows[0].get("status")) != "preview":
            raise RegistrationError("Manual Swiss repair requires an organizer preview")

        participants = await service.registration_repository.participants()
        roster = [
            dict(row)
            for row in participants
            if _text(row.get("tournament_id")) == tid
            and _text(row.get("status")) == "confirmed"
        ]
        standings = calculate_qualification_standings(old_matches, tid)
        players = _players_from_roster(roster, standings)
        player_by_id = {player.user_id: player for player in players}
        roster_by_id = {_text(row["discord_user_id"]): row for row in roster}
        history = _opponent_history(old_matches, tid, before_round=round_number)
        current = [
            dict(row)
            for row in old_matches
            if _text(row.get("tournament_id")) == tid
            and _text(row.get("round_id")) == round_id
        ]

        conflicted = conflicted_preview_players(current, set(player_by_id), player_by_id, history)
        if not conflicted:
            raise RegistrationError(
                "This Swiss preview is valid. A valid draw cannot be manually reshuffled by preference."
            )

        normalized = _normalize_replacements(replacement_pairs)
        supplied = {uid for pair in normalized for uid in pair}
        if supplied != conflicted:
            missing = ", ".join(sorted(conflicted - supplied)) or "none"
            extra = ", ".join(sorted(supplied - conflicted)) or "none"
            raise RegistrationError(
                "Manual repair must cover exactly the conflicted subset. "
                f"Missing: {missing}. Outside subset: {extra}."
            )

        unaffected = [
            row
            for row in current
            if _text(row.get("player_a_discord_user_id")) not in conflicted
            and _text(row.get("player_b_discord_user_id")) not in conflicted
        ]
        availability = await service.registration_repository.availability()
        slots = await service.registration_repository.availability_slots()
        selected = _enabled_availability(availability, slots, tid)
        slot_rank = _slot_rank(slots)

        repaired = [dict(row) for row in unaffected]
        next_number = 1
        used_numbers = {
            int(_text(row.get("match_number")))
            for row in unaffected
            if _text(row.get("match_number")).isdigit()
        }
        for a_id, b_id in normalized:
            while next_number in used_numbers:
                next_number += 1
            shared = _shared_slots(a_id, b_id, selected, slot_rank)
            row = _match_row(
                tid,
                round_id,
                next_number,
                roster_by_id[a_id],
                roster_by_id[b_id],
                shared,
            )
            row["status"] = "preview"
            row["notes"] = "Organizer manual repair of hard-rule-conflicted Swiss subset"
            repaired.append(row)
            used_numbers.add(next_number)
            next_number += 1

        repaired.sort(key=_match_sort_key)
        _validate_complete_candidate(repaired, set(player_by_id), player_by_id, history)

        new_matches = [
            dict(row)
            for row in old_matches
            if not (
                _text(row.get("tournament_id")) == tid
                and _text(row.get("round_id")) == round_id
            )
        ] + repaired

        current_fingerprint = source_fingerprint(old_matches, tid, before_round=round_number)
        new_rounds = [dict(row) for row in old_rounds]
        target = next(
            row
            for row in new_rounds
            if _text(row.get("tournament_id")) == tid
            and _text(row.get("round_id")) == round_id
        )
        target["notes"] = _replace_fingerprint(_text(target.get("notes")), current_fingerprint)
        target["generated_at_utc"] = utc_iso(service.clock().astimezone(UTC))
        target["generated_by_discord_user_id"] = str(actor_id)
        target["approved_at_utc"] = ""
        target["approved_by_discord_user_id"] = ""

        await service.repository.persist_state(
            new_rounds,
            new_matches,
            previous_rounds=old_rounds,
            previous_matches=old_matches,
        )
        now = utc_iso(service.clock().astimezone(UTC))
        try:
            await service.registration_repository.append_audit(
                dict(
                    event_id=str(uuid4()),
                    tournament_id=tid,
                    event_type="swiss_preview_manual_repair",
                    actor_discord_user_id=str(actor_id),
                    target_discord_user_id="",
                    details=json.dumps(
                        {
                            "round_number": round_number,
                            "conflicted_player_ids": sorted(conflicted),
                            "replacement_pairs": [list(pair) for pair in normalized],
                        },
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    created_at_utc=now,
                )
            )
        except Exception:
            pass
        return await service.snapshot(round_number)


def conflicted_preview_players(current, roster_ids, player_by_id, history) -> set[str]:
    """Return the smallest closed set of players implicated by hard-rule violations."""
    conflicted: set[str] = set()
    occurrences: dict[str, int] = {}
    paired_ids: set[str] = set()
    pair_rows: list[tuple[str, str]] = []
    for row in current:
        a = _text(row.get("player_a_discord_user_id"))
        b = _text(row.get("player_b_discord_user_id"))
        pair_rows.append((a, b))
        if not a or not b or a == b:
            conflicted.update(uid for uid in (a, b) if uid)
            continue
        for uid in (a, b):
            occurrences[uid] = occurrences.get(uid, 0) + 1
            paired_ids.add(uid)
            if uid not in roster_ids:
                conflicted.add(uid)
        if frozenset((a, b)) in history:
            conflicted.update((a, b))
        pa = player_by_id.get(a)
        pb = player_by_id.get(b)
        if pa is None or pb is None or abs(pa.wins - pb.wins) > 1:
            conflicted.update((a, b))
    conflicted.update(uid for uid, count in occurrences.items() if count != 1)
    conflicted.update(roster_ids - paired_ids)

    # If one endpoint must move, its current opponent also belongs to the repair set.
    # Close transitively so unaffected rows can truly remain byte-for-byte untouched.
    changed = True
    while changed:
        changed = False
        for a, b in pair_rows:
            if not a or not b:
                continue
            if (a in conflicted) ^ (b in conflicted):
                conflicted.update((a, b))
                changed = True
    return conflicted


def _normalize_replacements(pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
    normalized = []
    seen = set()
    for raw_a, raw_b in pairs:
        a, b = str(raw_a).strip(), str(raw_b).strip()
        if not a or not b or a == b:
            raise RegistrationError("Every manual Swiss pairing must contain two different player IDs")
        if a in seen or b in seen:
            raise RegistrationError("A player can appear only once in the manual repair subset")
        seen.update((a, b))
        normalized.append((a, b))
    if not normalized:
        raise RegistrationError("Provide at least one replacement pairing")
    return normalized


def _validate_complete_candidate(current, roster_ids, player_by_id, history) -> None:
    seen = set()
    for row in current:
        a = _text(row.get("player_a_discord_user_id"))
        b = _text(row.get("player_b_discord_user_id"))
        if not a or not b or a == b or a in seen or b in seen:
            raise RegistrationError("Manual Swiss candidate has duplicate or invalid player placement")
        if a not in roster_ids or b not in roster_ids:
            raise RegistrationError("Manual Swiss candidate contains a player outside the confirmed roster")
        if frozenset((a, b)) in history:
            raise RegistrationError("Manual Swiss candidate contains a rematch")
        if abs(player_by_id[a].wins - player_by_id[b].wins) > 1:
            raise RegistrationError("Manual Swiss candidate crosses non-adjacent record groups")
        seen.update((a, b))
    if seen != roster_ids:
        raise RegistrationError("Manual Swiss candidate must cover the full confirmed roster exactly once")


def parse_manual_pairs(raw: str) -> list[tuple[str, str]]:
    """Parse `id-id, id-id` or one pairing per line."""
    chunks = [part.strip() for part in str(raw or "").replace("\n", ",").split(",") if part.strip()]
    result = []
    for chunk in chunks:
        parts = [part.strip() for part in chunk.split("-")]
        if len(parts) != 2 or not all(parts):
            raise RegistrationError("Enter manual pairings as `playerID-playerID`, separated by commas or lines")
        result.append((parts[0], parts[1]))
    return _normalize_replacements(result)


def _replace_fingerprint(notes: str, fingerprint: str) -> str:
    lines = [
        line
        for line in str(notes or "").splitlines()
        if not line.startswith(_SOURCE_NOTE_PREFIX)
    ]
    lines.insert(0, f"{_SOURCE_NOTE_PREFIX}{fingerprint}")
    return "\n".join(line for line in lines if line).strip()
