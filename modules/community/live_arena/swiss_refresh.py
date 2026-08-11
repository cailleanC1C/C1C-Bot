"""Staleness recovery for Swiss previews that were already approved but not opened."""

from __future__ import annotations

from modules.community.live_arena.registration import RegistrationError, _locks
from modules.community.live_arena.service import _text, load_config
from modules.community.live_arena.swiss import (
    SwissQualificationService,
    _source_fingerprint_from_notes,
    source_fingerprint,
)


async def regenerate_current_preview(
    service: SwissQualificationService,
    actor_id: str,
    round_number: int,
):
    """Regenerate a preview, demoting an approved draw only when its source is stale.

    A current approved draw cannot be reshuffled by preference. If finalized source
    results changed before publication, however, approval is no longer valid and the
    draw is explicitly returned to preview state before deterministic regeneration.
    """
    config = await load_config(service.sheet_id)
    tid = config["ACTIVE_TOURNAMENT_ID"]
    round_id = f"{tid}-Q{round_number}"

    rounds = await service.repository.rounds()
    matches = await service.repository.matches()
    found = [
        row
        for row in rounds
        if _text(row.get("tournament_id")) == tid
        and _text(row.get("round_id")) == round_id
    ]
    if len(found) != 1:
        raise RegistrationError(f"Q{round_number} does not have exactly one persisted draw")
    status = _text(found[0].get("status"))
    if status == "preview":
        return await service.generate_preview(actor_id, round_number, regenerate=True)
    if status != "approved":
        raise RegistrationError(
            f"Q{round_number} can only be regenerated from preview or stale approved state"
        )

    expected = _source_fingerprint_from_notes(_text(found[0].get("notes")))
    current = source_fingerprint(matches, tid, before_round=round_number)
    if expected and expected == current:
        raise RegistrationError(
            "This approved Swiss draw is still current and cannot be reshuffled by preference"
        )

    async with _locks[(service.sheet_id, tid)]:
        latest_rounds = await service.repository.rounds()
        latest_matches = await service.repository.matches()
        target = [
            row
            for row in latest_rounds
            if _text(row.get("tournament_id")) == tid
            and _text(row.get("round_id")) == round_id
        ]
        if len(target) != 1 or _text(target[0].get("status")) != "approved":
            raise RegistrationError("Swiss draw state changed while invalidation was being processed")
        expected = _source_fingerprint_from_notes(_text(target[0].get("notes")))
        current = source_fingerprint(latest_matches, tid, before_round=round_number)
        if expected and expected == current:
            raise RegistrationError(
                "This approved Swiss draw is still current and cannot be reshuffled by preference"
            )
        replacement = [dict(row) for row in latest_rounds]
        row = next(
            row
            for row in replacement
            if _text(row.get("tournament_id")) == tid
            and _text(row.get("round_id")) == round_id
        )
        row["status"] = "preview"
        row["approved_at_utc"] = ""
        row["approved_by_discord_user_id"] = ""
        await service.repository.persist_rounds(
            replacement,
            previous_rounds=latest_rounds,
        )

    return await service.generate_preview(actor_id, round_number, regenerate=True)
