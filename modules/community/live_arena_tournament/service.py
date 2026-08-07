"""Registration business rules, independent of Discord UI."""

from __future__ import annotations
import asyncio
import logging
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

log = logging.getLogger("c1c.community.live_arena_registration")


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

    async def _audit_warning(self, *args):
        try:
            await self.repository.audit(*args)
            return []
        except Exception as exc:
            log.exception(
                "live_arena_audit_failed",
                extra={"event": args[0], "tournament_id": args[1]},
            )
            return [f"Audit log could not be written: {exc}"]

    async def register(
        self,
        *,
        tournament,
        user_id,
        display_name,
        member_role_ids,
        timezone_name,
        slot_ids,
        anchor_monday=None,
    ):
        """Commit participant+availability under one tournament lock; audit is non-core."""
        timezone_name = validate_timezone(timezone_name)
        tid = tournament.tournament_id
        log.info(
            "live_arena_registration_start",
            extra={"tournament_id": tid, "actor_id": user_id},
        )
        async with self._locks[tid]:
            participants = await self.repository.rows(
                "participants",
                ("tournament_id", "participant_slot", "status", "discord_user_id"),
            )
            existing = self.participant_for(participants, tid, user_id)
            old_status = norm(existing.get("status")) if existing else ""
            if existing and old_status not in {"open", "withdrawn", "confirmed"}:
                log.info(
                    "live_arena_registration_rejected",
                    extra={"tournament_id": tid, "reason": "participant_state"},
                )
                raise RegistrationError(
                    "This registration cannot be changed through self-service; contact a tournament organizer."
                )
            if norm(tournament.status) != "signup_open":
                raise RegistrationError("Registration is not open.")
            increases_count = old_status != "confirmed"
            if (
                increases_count
                and self.confirmed_count(participants, tid)
                >= tournament.maximum_participants
            ):
                log.info(
                    "live_arena_registration_rejected",
                    extra={"tournament_id": tid, "reason": "capacity"},
                )
                raise RegistrationError("The tournament is at capacity.")
            clans = await self.repository.rows(
                "eligible_clans", ("tournament_id", "clan_tag")
            )
            clan = self.eligible_clan(member_role_ids, clans, tid)
            raw_slots = await self.repository.rows(
                "availability_slots",
                (
                    "slot_id",
                    "weekday_utc",
                    "start_time_utc",
                    "end_time_utc",
                    "end_day_offset",
                    "enabled",
                    "sort_order",
                ),
            )
            slots = [
                AvailabilitySlot(
                    str(r["slot_id"]),
                    parse_weekday(r["weekday_utc"]),
                    str(r["start_time_utc"]),
                    str(r.get("end_time_utc", "")),
                    truthy(r.get("enabled")),
                    int(float(r.get("sort_order") or 0)),
                    int(float(r.get("end_day_offset") or 0)),
                )
                for r in raw_slots
                if str(r.get("tournament_id", tid)) in ("", tid)
            ]
            selected = validate_availability(
                slot_ids,
                slots,
                timezone_name,
                tournament.minimum_availability,
                anchor_monday=anchor_monday,
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
            append_new = target is None
            if append_new:
                next_slot = (
                    max(
                        (
                            int(r.get("participant_slot") or 0)
                            for r in participants
                            if str(r.get("tournament_id")) == tid
                        ),
                        default=0,
                    )
                    + 1
                )
                target = {
                    "tournament_id": tid,
                    "participant_slot": next_slot,
                    "status": "open",
                    "discord_user_id": "",
                }
            now = datetime.now(timezone.utc).isoformat()
            changes = {
                "tournament_id": tid,
                "participant_slot": target["participant_slot"],
                "discord_user_id": str(user_id),
                "display_name_at_signup": display_name,
                "clan_tag_at_signup": clan,
                "timezone": timezone_name,
                "status": "confirmed",
                "clan_verification_status": "verified",
                "signed_up_at": target.get("signed_up_at") or now,
                "confirmed_at": target.get("confirmed_at")
                if old_status == "confirmed"
                else now,
                "withdrawn_at": "",
                "withdrawal_reason": "",
            }
            old = dict(target)
            old_slots = (
                await self.repository.availability_slot_ids(tid, str(user_id))
                if hasattr(self.repository, "availability_slot_ids")
                else []
            )
            row_number = int(
                target.get("_row_number")
                or (participants.index(target) + 2 if not append_new else 0)
            )
            try:
                if append_new:
                    await self.repository.append("participants", changes)
                else:
                    await self.repository.replace_row(
                        "participants", row_number, changes
                    )
                await self.repository.replace_availability(
                    tid, str(user_id), selected, now
                )
            except Exception:
                log.exception(
                    "live_arena_registration_core_failed", extra={"tournament_id": tid}
                )
                try:
                    if append_new:
                        await self.repository.delete_appended_participant(
                            tid, str(user_id)
                        )
                    else:
                        await self.repository.replace_row(
                            "participants",
                            row_number,
                            {k: old.get(k, "") for k in changes},
                        )
                except Exception:
                    log.exception(
                        "live_arena_registration_rollback_failed",
                        extra={"tournament_id": tid},
                    )
                raise
            event = (
                "registration_updated"
                if old_status == "confirmed"
                else "registration_confirmed"
            )
            audit_old = {**old, "selected_slot_ids": old_slots}
            audit_new = {**changes, "selected_slot_ids": selected}
            warnings = await self._audit_warning(
                event, tid, user_id, "participant", user_id, audit_old, audit_new
            )
            log.info(
                "live_arena_registration_updated"
                if old_status == "confirmed"
                else "live_arena_registration_confirmed",
                extra={"tournament_id": tid, "actor_id": user_id},
            )
            return {
                "participant": {**target, **changes},
                "created": old_status != "confirmed",
                "slots": selected,
                "warnings": warnings,
            }

    async def change_participant_status(
        self,
        tournament_id,
        user_id,
        status,
        actor,
        reason="",
        *,
        tournament=None,
        member_role_ids=None,
        eligible_rows=None,
        member_present=True,
    ):
        """Validate and mutate status within the same lock (including restore capacity)."""
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
            if status == "confirmed":
                if not tournament or norm(tournament.status) not in {
                    "signup_open",
                    "signup_closed",
                }:
                    raise RegistrationError(
                        "Participants can only be restored while registration is open or closed."
                    )
                if (
                    self.confirmed_count(rows, tournament_id)
                    >= tournament.maximum_participants
                ):
                    raise RegistrationError("The tournament is at capacity.")
                if not member_present:
                    raise RegistrationError(
                        "The participant is no longer a member of this server."
                    )
                self.eligible_clan(
                    member_role_ids or (), eligible_rows or (), tournament_id
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
            event = {
                "withdrawn": "registration_withdrawn",
                "removed": "participant_removed",
                "confirmed": "participant_restored",
            }[status]
            warnings = await self._audit_warning(
                event,
                tournament_id,
                actor,
                "participant",
                user_id,
                participant,
                changes,
            )
            log.info(
                f"live_arena_{event}",
                extra={"tournament_id": tournament_id, "actor_id": actor},
            )
            return {**participant, **changes, "_warnings": warnings}
