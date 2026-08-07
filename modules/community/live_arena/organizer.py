"""Organizer registration lifecycle domain for Live Arena PR5."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from uuid import uuid4

from shared.sheets.async_core import afetch_values

from modules.community.live_arena.registration import (
    RegistrationError,
    RegistrationService,
    _locks,
    utc_iso,
    validate_availability,
)
from modules.community.live_arena.repository import LiveArenaRepository
from modules.community.live_arena.service import (
    AVAILABILITY_SLOT_HEADERS,
    ELIGIBLE_CLAN_HEADERS,
    TOURNAMENT_HEADERS,
    LiveArenaConfigError,
    _rows,
    _text,
    load_config,
)

log = logging.getLogger("c1c.community.live_arena.organizer")
KNOWN_STATUSES = ("confirmed", "withdrawn", "removed", "disqualified")


class OrganizerService:
    def __init__(self, sheet_id, repository=None, clock=None):
        self.sheet_id = sheet_id
        self.repository = repository or LiveArenaRepository(sheet_id)
        self.clock = clock or (lambda: datetime.now(UTC))

    async def initialize(self):
        await self.repository.initialize()

    async def context(self):
        config = await load_config(self.sheet_id)
        matrices = await __import__("asyncio").gather(
            afetch_values(self.sheet_id, config["TOURNAMENTS_TAB"]),
            afetch_values(self.sheet_id, config["ELIGIBLE_CLANS_TAB"]),
            afetch_values(self.sheet_id, config["AVAILABILITY_SLOTS_TAB"]),
        )
        tournaments = _rows(
            matrices[0] or [], TOURNAMENT_HEADERS, config["TOURNAMENTS_TAB"]
        )
        tournament_id_column = [_text(value) for value in matrices[0][0]].index(
            "tournament_id"
        )
        sheet_rows = [
            index
            for index, values in enumerate(matrices[0][1:], 2)
            if tournament_id_column < len(values)
            and _text(values[tournament_id_column]) == config["ACTIVE_TOURNAMENT_ID"]
        ]
        matches = [
            row
            for row in tournaments
            if _text(row["tournament_id"]) == config["ACTIVE_TOURNAMENT_ID"]
        ]
        if len(matches) != 1 or len(sheet_rows) != 1:
            raise LiveArenaConfigError(
                "TOURNAMENTS: active tournament must occur exactly once"
            )
        return (
            config,
            (sheet_rows[0], matches[0]),
            _rows(
                matrices[1] or [], ELIGIBLE_CLAN_HEADERS, config["ELIGIBLE_CLANS_TAB"]
            ),
            _rows(
                matrices[2] or [],
                AVAILABILITY_SLOT_HEADERS,
                config["AVAILABILITY_SLOTS_TAB"],
            ),
        )

    async def transition(self, action, actor_id):
        expected, new = {
            "open": ("draft", "signup_open"),
            "close": ("signup_open", "signup_closed"),
            "reopen": ("signup_closed", "signup_open"),
        }[action]
        config = await load_config(self.sheet_id)
        tournament_id = config["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tournament_id)]:
            _, (row_number, tournament), _, _ = await self.context()
            if _text(tournament["status"]) != expected:
                raise RegistrationError(
                    f"registration can only {action} from {expected}"
                )
            if action in {"open", "reopen"}:
                self._future_deadline(tournament["signup_closes_at_utc"])
            participants = await self.repository.participants()
            confirmed = sum(
                _text(r["tournament_id"]) == tournament_id
                and _text(r["status"]) == "confirmed"
                for r in participants
            )
            values = {"status": new}
            now = utc_iso(self.clock())
            if action == "open":
                values["signup_opens_at_utc"] = now
            await self.repository.update_tournament_cells(row_number, values)
            event = {
                "open": "registration_opened",
                "close": "registration_closed",
                "reopen": "registration_reopened",
            }[action]
            await self._audit(
                tournament_id,
                actor_id,
                "",
                event,
                {
                    "previous_status": expected,
                    "new_status": new,
                    "signup_closes_at_utc": _text(tournament["signup_closes_at_utc"]),
                    "confirmed_count": confirmed,
                    "parity": "even" if confirmed % 2 == 0 else "odd",
                },
                now,
            )
            return confirmed

    def _future_deadline(self, value):
        try:
            parsed = datetime.fromisoformat(_text(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise RegistrationError(
                "signup_closes_at_utc must be prepared as a valid future timezone-aware timestamp before opening or reopening"
            ) from exc
        if parsed.tzinfo is None or parsed <= self.clock():
            raise RegistrationError(
                "signup_closes_at_utc must be prepared as a valid future timezone-aware timestamp before opening or reopening"
            )

    async def remove(self, actor_id, target_id):
        return await self._participant_change(actor_id, target_id, restore=False)

    async def restore(self, actor_id, target_id, member):
        return await self._participant_change(
            actor_id, target_id, restore=True, member=member
        )

    async def _participant_change(self, actor_id, target_id, *, restore, member=None):
        config = await load_config(self.sheet_id)
        tid = config["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tid)]:
            _, (_, tournament), clans, slots = await self.context()
            if _text(tournament["status"]) not in {"signup_open", "signup_closed"}:
                raise RegistrationError(
                    "participant changes require open or closed registration"
                )
            participants = await self.repository.participants()
            row = next(
                (
                    r
                    for r in participants
                    if _text(r["tournament_id"]) == tid
                    and _text(r["discord_user_id"]) == str(target_id)
                ),
                None,
            )
            required = "removed" if restore else "confirmed"
            if row is None or _text(row["status"]) != required:
                raise RegistrationError(f"participant must currently be {required}")
            if restore:
                if member is None:
                    raise RegistrationError(
                        "participant Discord member cannot be resolved"
                    )
                clan = RegistrationService._eligible(
                    tournament, clans, [str(r.id) for r in member.roles]
                )
                confirmed = sum(
                    _text(r["tournament_id"]) == tid
                    and _text(r["status"]) == "confirmed"
                    for r in participants
                )
                if confirmed >= int(_text(tournament["max_participants"])):
                    raise RegistrationError("tournament capacity is full")
                availability = await self.repository.availability()
                saved = [
                    _text(r["slot_id"])
                    for r in availability
                    if _text(r["tournament_id"]) == tid
                    and _text(r["discord_user_id"]) == str(target_id)
                ]
                validate_availability(
                    _text(row["timezone"]),
                    saved,
                    slots,
                    tournament["signup_closes_at_utc"],
                )
            old = [dict(r) for r in participants]
            now = utc_iso(self.clock())
            if restore:
                row.update(
                    status="confirmed",
                    confirmed_at_utc=now,
                    withdrawn_at_utc="",
                    withdrawal_reason="",
                    updated_at_utc=now,
                    display_name_at_signup=member.display_name,
                    clan_tag_at_signup=_text(clan["clan_tag"]),
                )
            else:
                row.update(status="removed", updated_at_utc=now)
            await self.repository.persist_participants(
                participants, previous_participants=old
            )
            await self._audit(
                tid,
                actor_id,
                str(target_id),
                "participant_restored" if restore else "participant_removed",
                {},
                now,
            )

    async def _audit(self, tid, actor, target, event, details, now):
        try:
            await self.repository.append_audit(
                dict(
                    event_id=str(uuid4()),
                    tournament_id=tid,
                    event_type=event,
                    actor_discord_user_id=str(actor),
                    target_discord_user_id=target,
                    details=json.dumps(details, sort_keys=True, separators=(",", ":")),
                    created_at_utc=now,
                )
            )
        except Exception:
            log.exception(
                "❌ Live Arena audit — append failed • tournament=%s • event=%s",
                tid,
                event,
            )


def status_counts(participants, tournament_id):
    return {
        status: sum(
            _text(r["tournament_id"]) == tournament_id and _text(r["status"]) == status
            for r in participants
        )
        for status in KNOWN_STATUSES
    }
