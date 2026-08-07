"""Discord-independent Live Arena registration domain service."""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from typing import Callable
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from shared.sheets.async_core import afetch_values

from modules.community.live_arena.repository import LiveArenaRepository
from modules.community.live_arena.service import (
    AVAILABILITY_SLOT_HEADERS,
    ELIGIBLE_CLAN_HEADERS,
    TOURNAMENT_HEADERS,
    LiveArenaConfigError,
    _enabled,
    _required_int,
    _rows,
    _text,
    load_config,
)

log = logging.getLogger("c1c.community.live_arena")
_locks: defaultdict[tuple[str, str], asyncio.Lock] = defaultdict(asyncio.Lock)


class RegistrationError(ValueError):
    """A clear, user-presentable registration rejection."""


def utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_close(value: object) -> datetime:
    text = _text(value)
    if not text:
        raise RegistrationError("signup_closes_at_utc is required while signup is open")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RegistrationError("signup_closes_at_utc is invalid") from exc
    if parsed.tzinfo is None:
        raise RegistrationError("signup_closes_at_utc must include a timezone")
    return parsed.astimezone(UTC)


def validate_availability(
    timezone: str, slot_ids: list[str], slots: list[dict[str, object]], close: object
) -> list[str]:
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RegistrationError("timezone must be a valid IANA timezone") from exc
    selected = list(dict.fromkeys(str(value) for value in slot_ids))
    by_id = {_text(row["slot_id"]): row for row in slots}
    if any(slot_id not in by_id for slot_id in selected):
        raise RegistrationError("unknown availability slot ID")
    if any(not _enabled(by_id[slot_id]["enabled"]) for slot_id in selected):
        raise RegistrationError("disabled availability slot selected")
    if len(selected) < 3:
        raise RegistrationError("select at least 3 distinct availability slots")
    anchor = _parse_close(close)
    monday = anchor.date() - timedelta(days=anchor.weekday())
    weekdays = {
        name: index
        for index, name in enumerate(
            (
                "Monday",
                "Tuesday",
                "Wednesday",
                "Thursday",
                "Friday",
                "Saturday",
                "Sunday",
            )
        )
    }
    local_days = set()
    for slot_id in selected:
        row = by_id[slot_id]
        try:
            day = monday + timedelta(days=weekdays[_text(row["weekday_utc"])])
            start = time.fromisoformat(_text(row["start_time_utc"]))
            end = time.fromisoformat(_text(row["end_time_utc"]))
        except (KeyError, ValueError) as exc:
            raise LiveArenaConfigError(f"invalid availability slot: {slot_id}") from exc
        start_at = datetime.combine(day, start, UTC)
        end_at = datetime.combine(day, end, UTC)
        if end_at <= start_at:
            end_at += timedelta(days=1)
        if end_at - start_at != timedelta(hours=2):
            raise RegistrationError(
                "selected availability slots must be two-hour windows"
            )
        local_days.add(start_at.astimezone(zone).date())
    if len(local_days) < 2:
        raise RegistrationError("availability must span at least 2 local start-days")
    return selected


class RegistrationService:
    def __init__(
        self,
        sheet_id: str,
        repository: LiveArenaRepository | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sheet_id = sheet_id
        self.repository = repository or LiveArenaRepository(sheet_id)
        self.clock = clock or (lambda: datetime.now(UTC))

    async def initialize(self) -> None:
        await self.repository.initialize()

    async def _context(self):
        config = await load_config(self.sheet_id)
        matrices = await asyncio.gather(
            afetch_values(self.sheet_id, config["TOURNAMENTS_TAB"]),
            afetch_values(self.sheet_id, config["ELIGIBLE_CLANS_TAB"]),
            afetch_values(self.sheet_id, config["AVAILABILITY_SLOTS_TAB"]),
        )
        tournaments = _rows(
            matrices[0] or [], TOURNAMENT_HEADERS, config["TOURNAMENTS_TAB"]
        )
        tournament = next(
            (
                r
                for r in tournaments
                if _text(r["tournament_id"]) == config["ACTIVE_TOURNAMENT_ID"]
            ),
            None,
        )
        if tournament is None:
            raise LiveArenaConfigError("active tournament not found")
        return (
            config,
            tournament,
            _rows(
                matrices[1] or [], ELIGIBLE_CLAN_HEADERS, config["ELIGIBLE_CLANS_TAB"]
            ),
            _rows(
                matrices[2] or [],
                AVAILABILITY_SLOT_HEADERS,
                config["AVAILABILITY_SLOTS_TAB"],
            ),
        )

    async def register(
        self,
        user_id: str,
        display_name: str,
        role_ids: list[str],
        timezone: str,
        slot_ids: list[str],
    ) -> None:
        config, tournament, clans, slots = await self._context()
        tournament_id = config["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tournament_id)]:
            await self._register_locked(
                tournament,
                clans,
                slots,
                user_id,
                display_name,
                role_ids,
                timezone,
                slot_ids,
            )

    async def _register_locked(
        self,
        tournament,
        clans,
        slots,
        user_id,
        display_name,
        role_ids,
        timezone,
        slot_ids,
    ):
        self._require_status(tournament, {"signup_open"})
        tournament_id = _text(tournament["tournament_id"])
        participants = await self.repository.participants()
        availability = await self.repository.availability()
        existing = next(
            (
                r
                for r in participants
                if _text(r["tournament_id"]) == tournament_id
                and _text(r["discord_user_id"]) == str(user_id)
            ),
            None,
        )
        if existing and _text(existing["status"]) == "confirmed":
            raise RegistrationError("already registered")
        if existing and _text(existing["status"]) in {"removed", "disqualified"}:
            raise RegistrationError(
                f"{_text(existing['status'])} participant cannot self-register"
            )
        if existing and _text(existing["status"]) != "withdrawn":
            raise RegistrationError(
                f"unsupported participant status: {_text(existing['status'])}"
            )
        maximum = _required_int(
            tournament["max_participants"], "TOURNAMENTS.max_participants"
        )
        if (
            sum(
                _text(r["tournament_id"]) == tournament_id
                and _text(r["status"]) == "confirmed"
                for r in participants
            )
            >= maximum
        ):
            raise RegistrationError("tournament capacity is full")
        clan = self._eligible(tournament, clans, role_ids)
        selected = validate_availability(
            timezone, slot_ids, slots, tournament["signup_closes_at_utc"]
        )
        now = utc_iso(self.clock())
        previous_p, previous_a = (
            [dict(r) for r in participants],
            [dict(r) for r in availability],
        )
        if existing:
            existing.update(
                display_name_at_signup=display_name,
                clan_tag_at_signup=_text(clan["clan_tag"]),
                timezone=timezone,
                status="confirmed",
                confirmed_at_utc=now,
                withdrawn_at_utc="",
                withdrawal_reason="",
                updated_at_utc=now,
            )
            event = "registration_reconfirmed"
        else:
            participants.append(
                dict(
                    tournament_id=tournament_id,
                    discord_user_id=str(user_id),
                    display_name_at_signup=display_name,
                    clan_tag_at_signup=_text(clan["clan_tag"]),
                    timezone=timezone,
                    status="confirmed",
                    signed_up_at_utc=now,
                    confirmed_at_utc=now,
                    withdrawn_at_utc="",
                    withdrawal_reason="",
                    updated_at_utc=now,
                    notes="",
                )
            )
            event = "registration_confirmed"
        updated_a = self._replacement(
            availability, tournament_id, str(user_id), selected, now
        )
        await self._core_write(participants, updated_a, previous_p, previous_a)
        await self._audit(
            tournament_id,
            str(user_id),
            event,
            {
                "clan_tag": _text(clan["clan_tag"]),
                "timezone": timezone,
                "availability_count": len(selected),
            },
            now,
        )

    async def update_availability(
        self, user_id: str, timezone: str, slot_ids: list[str]
    ) -> None:
        config, tournament, _, slots = await self._context()
        tournament_id = config["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tournament_id)]:
            self._require_status(tournament, {"signup_open"})
            participants, availability = (
                await self.repository.participants(),
                await self.repository.availability(),
            )
            row = self._confirmed(participants, tournament_id, str(user_id))
            selected = validate_availability(
                timezone, slot_ids, slots, tournament["signup_closes_at_utc"]
            )
            now = utc_iso(self.clock())
            old_p, old_a = (
                [dict(r) for r in participants],
                [dict(r) for r in availability],
            )
            row.update(timezone=timezone, updated_at_utc=now)
            await self._core_write(
                participants,
                self._replacement(
                    availability, tournament_id, str(user_id), selected, now
                ),
                old_p,
                old_a,
            )
            await self._audit(
                tournament_id,
                str(user_id),
                "availability_updated",
                {"timezone": timezone, "availability_count": len(selected)},
                now,
            )

    async def withdraw(self, user_id: str, reason: str = "") -> None:
        config, tournament, _, _ = await self._context()
        tournament_id = config["ACTIVE_TOURNAMENT_ID"]
        async with _locks[(self.sheet_id, tournament_id)]:
            self._require_status(tournament, {"signup_open", "signup_closed"})
            participants = await self.repository.participants()
            row = self._confirmed(participants, tournament_id, str(user_id))
            old = [dict(r) for r in participants]
            now = utc_iso(self.clock())
            row.update(
                status="withdrawn",
                withdrawn_at_utc=now,
                withdrawal_reason=reason,
                updated_at_utc=now,
            )
            try:
                await self.repository.replace_participants(participants)
            except Exception:
                await self.repository.replace_participants(old)
                raise
            await self._audit(
                tournament_id,
                str(user_id),
                "registration_withdrawn",
                {"withdrawal_reason": reason},
                now,
            )

    @staticmethod
    def _require_status(tournament, allowed):
        if _text(tournament["status"]) not in allowed:
            raise RegistrationError("tournament status does not allow this mutation")

    @staticmethod
    def _confirmed(rows, tournament_id, user_id):
        row = next(
            (
                r
                for r in rows
                if _text(r["tournament_id"]) == tournament_id
                and _text(r["discord_user_id"]) == user_id
            ),
            None,
        )
        if row is None or _text(row["status"]) != "confirmed":
            raise RegistrationError("participant is not confirmed")
        return row

    @staticmethod
    def _eligible(tournament, clans, role_ids):
        if _text(tournament["eligibility_scope"]) != "selected_clans":
            raise RegistrationError("unsupported eligibility scope")
        roles = {str(value) for value in role_ids}
        match = next(
            (
                r
                for r in clans
                if _text(r["tournament_id"]) == _text(tournament["tournament_id"])
                and _enabled(r["active"])
                and _text(r["discord_role_id"]) in roles
            ),
            None,
        )
        if match is None:
            raise RegistrationError("no matching active eligible clan role")
        return match

    @staticmethod
    def _replacement(rows, tournament_id, user_id, selected, now):
        retained = {
            _text(r["slot_id"]): r
            for r in rows
            if _text(r["tournament_id"]) == tournament_id
            and _text(r["discord_user_id"]) == user_id
        }
        other = [
            r
            for r in rows
            if not (
                _text(r["tournament_id"]) == tournament_id
                and _text(r["discord_user_id"]) == user_id
            )
        ]
        for slot_id in selected:
            created = _text(retained.get(slot_id, {}).get("created_at_utc")) or now
            other.append(
                dict(
                    tournament_id=tournament_id,
                    discord_user_id=user_id,
                    slot_id=slot_id,
                    created_at_utc=created,
                    updated_at_utc=now,
                    notes="",
                )
            )
        return other

    async def _core_write(self, participants, availability, old_p, old_a):
        try:
            await self.repository.replace_participants(participants)
            await self.repository.replace_availability(availability)
        except Exception:
            await self.repository.replace_participants(old_p)
            await self.repository.replace_availability(old_a)
            raise

    async def _audit(self, tournament_id, user_id, event, details, now):
        try:
            await self.repository.append_audit(
                dict(
                    event_id=str(uuid4()),
                    tournament_id=tournament_id,
                    event_type=event,
                    actor_discord_user_id=user_id,
                    target_discord_user_id=user_id,
                    details=json.dumps(details, sort_keys=True, separators=(",", ":")),
                    created_at_utc=now,
                )
            )
        except Exception:
            log.exception(
                "❌ Live Arena audit — append failed • tournament=%s • user=%s • event=%s",
                tournament_id,
                user_id,
                event,
            )
