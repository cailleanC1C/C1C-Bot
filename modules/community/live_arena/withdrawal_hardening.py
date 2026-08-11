"""Withdrawal effects across unpublished qualification and knockout rounds."""

from __future__ import annotations

from datetime import UTC

from modules.community.live_arena import knockout, knockout_runtime
from modules.community.live_arena.competition import (
    MATCH_TERMINAL_STATUSES,
    _append_note,
    _mark_round_ready_if_complete,
)
from modules.community.live_arena.competition_operations import CompetitionOperationsService
from modules.community.live_arena.qualification import QualificationSnapshot
from modules.community.live_arena.registration import utc_iso
from modules.community.live_arena.service import _text

_installed = False
_ADVANCE = "WITHDRAWAL_ADVANCE="
_DOUBLE = "WITHDRAWAL_DOUBLE"


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    _install_withdrawal_cleanup()
    _install_knockout_progression()
    _install_knockout_publisher()


def _install_withdrawal_cleanup() -> None:
    original = CompetitionOperationsService.withdraw_active_participant

    async def withdraw_with_unpublished_cleanup(self, actor_id: str, target_user_id: str, *, reason: str):
        result = await original(
            self,
            actor_id,
            target_user_id,
            reason=reason,
        )
        config = await __import__(
            "modules.community.live_arena.service",
            fromlist=["load_config"],
        ).load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        old_rounds = await self.repository.rounds()
        old_matches = await self.repository.matches()
        rounds = [dict(row) for row in old_rounds]
        matches = [dict(row) for row in old_matches]
        remove_round_ids: set[str] = set()
        changed = False

        for round_row in rounds:
            if _text(round_row.get("tournament_id")) != tid:
                continue
            status = _text(round_row.get("status"))
            if status not in {"preview", "approved", "proposed"}:
                continue
            round_id = _text(round_row.get("round_id"))
            affected = [
                match
                for match in matches
                if _text(match.get("tournament_id")) == tid
                and _text(match.get("round_id")) == round_id
                and str(target_user_id)
                in {
                    _text(match.get("player_a_discord_user_id")),
                    _text(match.get("player_b_discord_user_id")),
                }
            ]
            if not affected:
                continue
            stage = _text(round_row.get("round_stage")).lower()
            if stage == "qualification":
                # Unpublished Q2/Q3 must be rebuilt from the current confirmed roster.
                remove_round_ids.add(round_id)
                changed = True
                continue
            if stage in knockout.KNOCKOUT:
                for match in affected:
                    _mark_withdrawal_advance(match, str(target_user_id), reason)
                    changed = True

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

        if changed:
            await self.repository.persist_state(
                rounds,
                matches,
                previous_rounds=old_rounds,
                previous_matches=old_matches,
            )
        return result

    CompetitionOperationsService.withdraw_active_participant = withdraw_with_unpublished_cleanup


def _install_knockout_progression() -> None:
    original_create = knockout.KnockoutService._create_preview
    original_open = knockout.KnockoutService.approve_and_open

    async def create_preview_with_withdrawals(
        self,
        actor_id: str,
        stage: str,
        pairs,
        *,
        source: str,
        regenerate: bool,
    ):
        snapshot = await original_create(
            self,
            actor_id,
            stage,
            pairs,
            source=source,
            regenerate=regenerate,
        )
        participants = await self.registration_repository.participants()
        active = {
            _text(row.get("discord_user_id"))
            for row in participants
            if _text(row.get("tournament_id"))
            == _text(snapshot.round_row.get("tournament_id"))
            and _text(row.get("status")) == "confirmed"
        }
        old_matches = await self.repository.matches()
        matches = [dict(row) for row in old_matches]
        target_ids = {_text(row.get("match_id")) for row in snapshot.matches}
        changed = False
        for match in matches:
            if _text(match.get("match_id")) not in target_ids:
                continue
            a = _text(match.get("player_a_discord_user_id"))
            b = _text(match.get("player_b_discord_user_id"))
            missing = [uid for uid in (a, b) if uid not in active]
            if len(missing) == 1:
                _mark_withdrawal_advance(
                    match,
                    missing[0],
                    "participant no longer active before knockout publication",
                )
                changed = True
            elif len(missing) == 2:
                match["notes"] = _append_note(
                    _text(match.get("notes")),
                    _DOUBLE,
                )
                changed = True
        if changed:
            await self.repository.persist_matches(matches, previous_matches=old_matches)
            return await self.snapshot(stage)
        return snapshot

    async def open_with_withdrawal_advances(self, actor_id: str, stage: str):
        snapshot = await original_open(self, actor_id, stage)
        if not any(_withdrawal_marker(row) for row in snapshot.matches):
            return snapshot
        old_rounds = await self.repository.rounds()
        old_matches = await self.repository.matches()
        rounds = [dict(row) for row in old_rounds]
        matches = [dict(row) for row in old_matches]
        now = utc_iso(self.clock().astimezone(UTC))
        round_id = _text(snapshot.round_row.get("round_id"))
        for match in matches:
            if _text(match.get("round_id")) != round_id:
                continue
            marker = _withdrawal_marker(match)
            if not marker:
                continue
            if marker == _DOUBLE:
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
            else:
                match.update(
                    status="forfeit",
                    final_result_type="forfeit",
                    final_score_a="",
                    final_score_b="",
                    final_winner_discord_user_id=marker,
                    finalized_by_discord_user_id=str(actor_id),
                    finalized_at_utc=now,
                    confirmed_at_utc=now,
                )
        _mark_round_ready_if_complete(rounds, matches, _text(snapshot.round_row.get("tournament_id")), round_id)
        await self.repository.persist_state(
            rounds,
            matches,
            previous_rounds=old_rounds,
            previous_matches=old_matches,
        )
        return await self.snapshot(stage)

    knockout.KnockoutService._create_preview = create_preview_with_withdrawals
    knockout.KnockoutService.approve_and_open = open_with_withdrawal_advances


def _install_knockout_publisher() -> None:
    original = knockout_runtime.KnockoutPublisher.reconcile

    async def reconcile_without_advance_threads(self, snapshot: QualificationSnapshot):
        filtered = QualificationSnapshot(
            snapshot.round_row,
            tuple(
                row
                for row in snapshot.matches
                if _text(row.get("status")) not in MATCH_TERMINAL_STATUSES
            ),
        )
        return await original(self, filtered)

    knockout_runtime.KnockoutPublisher.reconcile = reconcile_without_advance_threads


def _mark_withdrawal_advance(match, withdrawn_id: str, reason: str) -> None:
    a = _text(match.get("player_a_discord_user_id"))
    b = _text(match.get("player_b_discord_user_id"))
    opponent = b if a == str(withdrawn_id) else a
    marker = f"{_ADVANCE}{opponent}" if opponent else _DOUBLE
    match["notes"] = _append_note(
        _text(match.get("notes")),
        f"{marker} | withdrawn={withdrawn_id} | reason={reason}",
    )


def _withdrawal_marker(match) -> str:
    for line in _text(match.get("notes")).splitlines():
        if _DOUBLE in line:
            return _DOUBLE
        if _ADVANCE in line:
            return line.split(_ADVANCE, 1)[1].split("|", 1)[0].strip()
    return ""
