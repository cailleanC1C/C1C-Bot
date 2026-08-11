"""Atomic Q1 approval path when the random draw contains a bye."""

from __future__ import annotations

from datetime import UTC, timedelta

from modules.community.live_arena import qualification
from modules.community.live_arena.bye_support import _is_bye
from modules.community.live_arena.qualification import QualificationSnapshot
from modules.community.live_arena.registration import RegistrationError, _locks, utc_iso
from modules.community.live_arena.service import _text

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    fallback = qualification.QualificationService.approve_draw

    async def approve_q1_with_bye(self, actor_id: str):
        base = await qualification.load_config(self.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        existing_matches = await self.repository.matches()
        bye_rows = [
            row
            for row in existing_matches
            if _text(row.get("tournament_id")) == tid
            and _text(row.get("round_id")) == f"{tid}-Q1"
            and _is_bye(row)
        ]
        if not bye_rows:
            return await fallback(self, actor_id)
        if len(bye_rows) != 1:
            raise RegistrationError("Q1 must contain exactly one random bye for an odd roster")

        async with _locks[(self.sheet_id, tid)]:
            _, (_, tournament), _, _ = await self.context()
            participants = await self.registration_repository.participants()
            roster = qualification._confirmed_roster(participants, tid)
            if _text(tournament.get("status")) != "signup_closed":
                raise RegistrationError("Q1 can only be approved after registration is closed")
            minimum = int(_text(tournament.get("min_participants")) or 0)
            if len(roster) < minimum:
                raise RegistrationError(
                    f"Q1 requires at least {minimum} confirmed participants; currently {len(roster)}"
                )
            if len(roster) % 2 != 1:
                raise RegistrationError("A Q1 bye is only valid for an odd confirmed roster")

            old_rounds = await self.repository.rounds()
            old_matches = await self.repository.matches()
            round_id = f"{tid}-Q1"
            round_row = qualification._single_q1_round(old_rounds, tid)
            if round_row is None or _text(round_row.get("status")) != "proposed":
                raise RegistrationError("Only a proposed Q1 draw can be approved")

            qmatches = [
                dict(row)
                for row in old_matches
                if _text(row.get("tournament_id")) == tid
                and _text(row.get("round_id")) == round_id
            ]
            byes = [row for row in qmatches if _is_bye(row)]
            if len(byes) != 1:
                raise RegistrationError("Q1 proposed draw must contain exactly one bye")

            proposed_ids: list[str] = []
            for row in qmatches:
                a = _text(row.get("player_a_discord_user_id"))
                b = _text(row.get("player_b_discord_user_id"))
                if a:
                    proposed_ids.append(a)
                if b:
                    proposed_ids.append(b)
            roster_ids = {_text(row.get("discord_user_id")) for row in roster}
            if (
                len(proposed_ids) != len(roster_ids)
                or len(set(proposed_ids)) != len(proposed_ids)
                or set(proposed_ids) != roster_ids
            ):
                raise RegistrationError(
                    "The confirmed roster changed after this draw was generated. Regenerate Q1 before approving it."
                )

            now_dt = self.clock().astimezone(UTC)
            now = utc_iso(now_dt)
            deadline = utc_iso(now_dt + timedelta(days=6))
            rounds = [dict(row) for row in old_rounds]
            target = qualification._single_q1_round(rounds, tid)
            target.update(
                status="active",
                opens_at_utc=now,
                deadline_at_utc=deadline,
                published_at_utc=now,
                approved_at_utc=now,
                approved_by_discord_user_id=str(actor_id),
            )

            matches = [dict(row) for row in old_matches]
            for row in matches:
                if (
                    _text(row.get("tournament_id")) != tid
                    or _text(row.get("round_id")) != round_id
                ):
                    continue
                row["published_at_utc"] = now
                row["deadline_at_utc"] = deadline
                if _is_bye(row):
                    bye_id = _text(row.get("player_a_discord_user_id"))
                    row.update(
                        status="bye",
                        final_result_type="bye",
                        final_score_a="",
                        final_score_b="",
                        final_winner_discord_user_id=bye_id,
                        finalized_by_discord_user_id=str(actor_id),
                        finalized_at_utc=now,
                        confirmed_at_utc=now,
                    )
                else:
                    row["status"] = "published"

            await self.repository.persist_state(
                rounds,
                matches,
                previous_rounds=old_rounds,
                previous_matches=old_matches,
            )
            await self._audit(
                tid,
                actor_id,
                "q1_draw_approved",
                {
                    "match_count": len(qmatches),
                    "bye_count": 1,
                    "deadline_at_utc": deadline,
                },
                now,
            )
            published = tuple(
                sorted(
                    (
                        row
                        for row in matches
                        if _text(row.get("tournament_id")) == tid
                        and _text(row.get("round_id")) == round_id
                    ),
                    key=qualification._match_sort_key,
                )
            )
            return QualificationSnapshot(target, published)

    qualification.QualificationService.approve_draw = approve_q1_with_bye
