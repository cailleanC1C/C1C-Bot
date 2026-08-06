"""Registration business rules, independent of Discord UI."""

from __future__ import annotations
import asyncio
from collections import defaultdict
from datetime import datetime, timezone
from .models import (
    AvailabilitySlot,
    RegistrationError,
    validate_availability,
    validate_timezone,
    parse_weekday,
    norm,
    truthy,
)


class LiveArenaService:
    def __init__(self, repository):
        self.repository = repository
        self._locks = defaultdict(asyncio.Lock)

    @staticmethod
    def eligible_clan(member_role_ids, rows, tournament_id):
        roles = {str(x) for x in member_role_ids}
        for row in rows:
            if (
                str(row.get("tournament_id", "")) == tournament_id
                and truthy(row.get("active", row.get("enabled", True)))
                and str(row.get("discord_role_id", row.get("role_id", ""))) in roles
            ):
                return str(row.get("clan_tag", row.get("clan", "")))
        raise RegistrationError(
            "You do not currently hold an eligible clan role for this tournament."
        )

    @staticmethod
    def participant_for(rows, tournament_id, user_id):
        return next(
            (
                r
                for r in rows
                if str(r.get("tournament_id")) == tournament_id
                and str(r.get("discord_user_id")) == str(user_id)
            ),
            None,
        )

    @staticmethod
    def confirmed_count(rows, tournament_id):
        return sum(
            str(r.get("tournament_id")) == tournament_id
            and norm(r.get("status")) == "confirmed"
            for r in rows
        )

    @staticmethod
    def can_transition(old, new):
        return (norm(old), norm(new)) in {
            ("draft", "signup_open"),
            ("signup_open", "signup_closed"),
            ("signup_closed", "signup_open"),
        }

    async def register(
        self,
        *,
        tournament,
        user_id,
        display_name,
        member_role_ids,
        timezone_name,
        slot_ids,
    ):
        timezone_name = validate_timezone(timezone_name)
        tid = tournament.tournament_id
        async with self._locks[tid]:
            participants = await self.repository.rows(
                "participants",
                ("tournament_id", "participant_slot", "status", "discord_user_id"),
            )
            existing = self.participant_for(participants, tid, user_id)
            if existing and norm(existing.get("status")) not in {
                "open",
                "withdrawn",
                "confirmed",
            }:
                raise RegistrationError(
                    "This registration cannot be changed through self-service; contact a tournament organizer."
                )
            if norm(tournament.status) != "signup_open":
                raise RegistrationError("Registration is not open.")
            if not existing and (
                self.confirmed_count(participants, tid)
                >= tournament.maximum_participants
            ):
                raise RegistrationError("The tournament is at capacity.")
            clans = await self.repository.rows(
                "eligible_clans", ("tournament_id", "clan_tag")
            )
            clan = self.eligible_clan(member_role_ids, clans, tid)
            raw_slots = await self.repository.rows(
                "availability_slots", ("slot_id", "weekday_utc", "start_time_utc")
            )
            slots = [
                AvailabilitySlot(
                    str(r["slot_id"]),
                    parse_weekday(r["weekday_utc"]),
                    str(r["start_time_utc"]),
                    str(r.get("end_time_utc", "")),
                    truthy(r.get("enabled", r.get("active", True))),
                    int(r.get("sort_order") or 0),
                )
                for r in raw_slots
                if str(r.get("tournament_id", tid)) in ("", tid)
            ]
            selected = validate_availability(
                slot_ids, slots, timezone_name, tournament.minimum_availability
            )
            target = existing or min(
                (
                    r
                    for r in participants
                    if str(r.get("tournament_id")) == tid
                    and norm(r.get("status")) == "open"
                    and not str(r.get("discord_user_id", "")).strip()
                ),
                key=lambda r: int(r["participant_slot"]),
                default=None,
            )
            if not target:
                raise RegistrationError("No open participant slot is available.")
            now = datetime.now(timezone.utc).isoformat()
            row_number = int(
                target.get("_row_number") or participants.index(target) + 2
            )
            changes = {
                "discord_user_id": str(user_id),
                "display_name_at_signup": display_name,
                "clan_tag_at_signup": clan,
                "timezone": timezone_name,
                "status": "confirmed",
                "clan_verification_status": "verified",
                "signed_up_at": target.get("signed_up_at") or now,
                "confirmed_at": (
                    target.get("confirmed_at")
                    if existing and norm(target.get("status")) == "confirmed"
                    else now
                ),
                "withdrawn_at": "",
                "withdrawal_reason": "",
            }
            old = dict(target)
            try:
                await self.repository.replace_row("participants", row_number, changes)
                await self.repository.replace_availability(
                    tid, str(user_id), selected, now
                ) if hasattr(
                    self.repository, "replace_availability"
                ) else self._unsupported()
            except Exception:
                try:
                    await self.repository.replace_row(
                        "participants", row_number, {k: old.get(k, "") for k in changes}
                    )
                except Exception:
                    pass
                raise
            await self.repository.audit(
                "availability_updated"
                if existing and norm(old.get("status")) == "confirmed"
                else "registration_confirmed",
                tid,
                user_id,
                "participant",
                user_id,
                old,
                changes,
            )
            return {
                "participant": {**target, **changes},
                "created": not existing or norm(old.get("status")) != "confirmed",
                "slots": selected,
            }

    async def change_participant_status(
        self, tournament_id, user_id, status, actor, reason=""
    ):
        """Withdraw/remove/restore without allowing invalid participant-state overwrites."""
        async with self._locks[tournament_id]:
            rows = await self.repository.rows("participants")
            participant = self.participant_for(rows, tournament_id, user_id)
            if not participant:
                raise RegistrationError("No registration was found.")
            old_status = norm(participant.get("status"))
            allowed = {
                "withdrawn": {"confirmed"},
                "removed": {"confirmed", "withdrawn"},
                "confirmed": {"removed", "withdrawn"},
            }
            if old_status not in allowed.get(status, set()):
                raise RegistrationError(
                    f"Cannot change participant from {old_status} to {status}."
                )
            now = datetime.now(timezone.utc).isoformat()
            changes = {"status": status}
            if status == "withdrawn":
                changes.update(
                    withdrawn_at=now,
                    withdrawal_reason=reason or "self_service_withdrawal",
                )
            elif status == "confirmed":
                changes.update(withdrawn_at="", withdrawal_reason="", confirmed_at=now)
            row_number = int(
                participant.get("_row_number") or rows.index(participant) + 2
            )
            await self.repository.replace_row("participants", row_number, changes)
            await self.repository.audit(
                f"participant_{status}",
                tournament_id,
                actor,
                "participant",
                user_id,
                participant,
                changes,
            )
            return {**participant, **changes}

    @staticmethod
    def _unsupported():
        raise RegistrationError("Availability repository writes are unavailable.")
