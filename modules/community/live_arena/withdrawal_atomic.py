"""Compensating-write active withdrawal for Live Arena competition state."""

from __future__ import annotations

from datetime import UTC

from modules.community.live_arena.competition import (
    MATCH_TERMINAL_STATUSES,
    ROUND_OPEN_STATUSES,
    _append_note,
    _mark_round_ready_if_complete,
    _single_round,
)
from modules.community.live_arena.competition_operations import CompetitionOperationsService
from modules.community.live_arena.registration import RegistrationError, _locks, utc_iso
from modules.community.live_arena.service import _text, load_config
from modules.community.live_arena.withdrawal_hardening import _mark_withdrawal_advance

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    async def withdraw_atomic(
        self,
        actor_id: str,
        target_user_id: str,
        *,
        reason: str,
    ) -> dict[str, object]:
        reason = str(reason or "").strip()
        if not reason:
            raise RegistrationError("A withdrawal reason is required")
        base = await load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        target_id = str(target_user_id)

        async with _locks[(self.sheet_id, tid)]:
            participants = await self.registration_repository.participants()
            target = next(
                (
                    row
                    for row in participants
                    if _text(row.get("tournament_id")) == tid
                    and _text(row.get("discord_user_id")) == target_id
                ),
                None,
            )
            if target is None or _text(target.get("status")) != "confirmed":
                raise RegistrationError("The participant is not currently confirmed")
            tournaments = await self.registration_repository.tournaments()
            tournament = next(
                (row for row in tournaments if _text(row.get("tournament_id")) == tid),
                None,
            )
            if tournament is None or _text(tournament.get("status")) != "active":
                raise RegistrationError("Post-start withdrawals are only available while the tournament is active")

            old_participants = [dict(row) for row in participants]
            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            rounds = [dict(row) for row in old_rounds]
            matches = [dict(row) for row in old_matches]
            now = utc_iso(self.clock().astimezone(UTC))

            target.update(
                status="withdrawn",
                withdrawn_at_utc=now,
                withdrawal_reason=reason,
                updated_at_utc=now,
            )

            remove_round_ids: set[str] = set()
            affected: list[dict[str, object]] = []
            for match in matches:
                if _text(match.get("tournament_id")) != tid:
                    continue
                if _text(match.get("status")) in MATCH_TERMINAL_STATUSES:
                    continue
                a = _text(match.get("player_a_discord_user_id"))
                b = _text(match.get("player_b_discord_user_id"))
                if target_id not in {a, b}:
                    continue
                round_row = _single_round(rounds, tid, _text(match.get("round_id")))
                round_status = _text(round_row.get("status"))
                stage = _text(round_row.get("round_stage")).lower()

                if round_status in {"preview", "proposed", "approved"}:
                    if stage == "qualification":
                        round_id = _text(round_row.get("round_id"))
                        remove_round_ids.add(round_id)
                        affected.append(
                            {
                                "match_id": _text(match.get("match_id")),
                                "action": "qualification_preview_removed",
                            }
                        )
                    else:
                        _mark_withdrawal_advance(match, target_id, reason)
                        affected.append(
                            {
                                "match_id": _text(match.get("match_id")),
                                "action": "knockout_preview_advance_marked",
                            }
                        )
                    continue

                if round_status not in ROUND_OPEN_STATUSES:
                    continue
                opponent = b if a == target_id else a
                if not opponent:
                    continue
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
                    f"Withdrawal forfeit: <@{target_id}> withdrew. Reason: {reason}",
                )
                affected.append(
                    {
                        "match_id": _text(match.get("match_id")),
                        "action": "forfeit",
                        "winner": opponent,
                    }
                )
                _mark_round_ready_if_complete(
                    rounds,
                    matches,
                    tid,
                    _text(match.get("round_id")),
                )

            if remove_round_ids:
                rounds = [
                    row
                    for row in rounds
                    if not (
                        _text(row.get("tournament_id")) == tid
                        and _text(row.get("round_id")) in remove_round_ids
                    )
                ]
                matches = [
                    row
                    for row in matches
                    if not (
                        _text(row.get("tournament_id")) == tid
                        and _text(row.get("round_id")) in remove_round_ids
                    )
                ]

            await self.registration_repository.persist_participants(
                participants,
                previous_participants=old_participants,
            )
            try:
                await self.repository.persist_state(
                    rounds,
                    matches,
                    previous_rounds=old_rounds,
                    previous_matches=old_matches,
                )
            except Exception as competition_error:
                try:
                    await self.registration_repository.persist_participants(
                        old_participants,
                        previous_participants=participants,
                    )
                except Exception as rollback_error:
                    raise RuntimeError(
                        "Active withdrawal competition write failed and participant rollback also failed: "
                        f"write={competition_error!r}; rollback={rollback_error!r}"
                    ) from rollback_error
                raise

            await self._audit(
                tid,
                str(actor_id),
                "active_participant_withdrawn",
                {
                    "target_user_id": target_id,
                    "reason": reason,
                    "affected_matches": affected,
                    "removed_unpublished_round_ids": sorted(remove_round_ids),
                },
                now,
                target_user_id=target_id,
            )
            return dict(target)

    CompetitionOperationsService.withdraw_active_participant = withdraw_atomic
