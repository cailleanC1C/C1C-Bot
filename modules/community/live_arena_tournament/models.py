"""Workbook-neutral models and validation for Live Arena registration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def norm(value: object) -> str:
    return "_".join(str(value or "").strip().lower().replace("-", " ").split())


def truthy(value: object) -> bool:
    return norm(value) in {"1", "true", "yes", "y", "active", "enabled"}


class LiveArenaError(RuntimeError):
    pass


class SchemaError(LiveArenaError):
    pass


class RegistrationError(LiveArenaError):
    pass


class AvailabilityError(RegistrationError):
    pass


@dataclass(slots=True)
class Tournament:
    tournament_id: str
    name: str
    status: str
    maximum_participants: int
    minimum_availability: int = 3
    signup_closes_at: str = ""
    eligibility_scope: str = "selected_clans"


@dataclass(slots=True)
class AvailabilitySlot:
    slot_id: str
    weekday_utc: int
    start_time_utc: str
    end_time_utc: str
    enabled: bool = True
    sort_order: int = 0
    end_day_offset: int = 0


WEEKDAYS = {
    name.lower(): index
    for index, name in enumerate(
        ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
    )
}


def parse_weekday(value: object) -> int:
    """Accept the workbook's weekday names (and legacy zero-based numbers)."""
    text = str(value).strip()
    if text.lower() in WEEKDAYS:
        return WEEKDAYS[text.lower()]
    try:
        number = int(text)
    except ValueError as exc:
        raise SchemaError(f"Invalid weekday_utc value: {value!r}") from exc
    if not 0 <= number <= 6:
        raise SchemaError(f"weekday_utc must be Monday-Sunday or 0-6: {value!r}")
    return number


@dataclass(slots=True)
class Participant:
    participant_slot: int
    tournament_id: str
    discord_user_id: str = ""
    status: str = "open"
    display_name_at_signup: str = ""
    clan_tag_at_signup: str = ""
    timezone: str = "UTC"
    values: dict[str, object] = field(default_factory=dict)
    row_number: int = 0


def validate_timezone(name: str) -> str:
    candidate = name.strip()
    try:
        ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, ValueError):
        raise RegistrationError(
            "Enter a valid IANA timezone, for example Europe/Vienna, America/New_York, Asia/Kolkata, or Australia/Sydney."
        )
    return candidate


def slot_local_datetime(
    slot: AvailabilitySlot, timezone_name: str, *, anchor_monday: datetime | None = None
) -> datetime:
    now = datetime.now(timezone.utc)
    anchor = anchor_monday or (now - timedelta(days=now.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    hour, minute = map(int, slot.start_time_utc.split(":")[:2])
    value = anchor.replace(hour=hour, minute=minute) + timedelta(days=slot.weekday_utc)
    return value.astimezone(ZoneInfo(timezone_name))


def slot_local_window(
    slot: AvailabilitySlot, timezone_name: str, *, anchor_monday: datetime
) -> tuple[datetime, datetime]:
    """Return the configured UTC window converted using one explicit DST anchor."""
    start = slot_local_datetime(slot, timezone_name, anchor_monday=anchor_monday)
    end_text = slot.end_time_utc or slot.start_time_utc
    hour, minute = map(int, end_text.split(":")[:2])
    end_utc = anchor_monday.replace(hour=hour, minute=minute) + timedelta(
        days=slot.weekday_utc + slot.end_day_offset
    )
    if not slot.end_day_offset and end_utc <= start.astimezone(timezone.utc):
        end_utc += timedelta(days=1)
    return start, end_utc.astimezone(ZoneInfo(timezone_name))


def validate_availability(
    selected: list[str],
    slots: list[AvailabilitySlot],
    timezone_name: str,
    minimum: int = 3,
    *,
    anchor_monday: datetime | None = None,
) -> list[str]:
    unique = list(dict.fromkeys(selected))
    enabled = {s.slot_id: s for s in slots if s.enabled}
    invalid = [item for item in unique if item not in enabled]
    if invalid:
        raise AvailabilityError(
            "One or more selected availability windows are no longer enabled."
        )
    if len(unique) < minimum:
        raise AvailabilityError(f"Select at least {minimum} availability windows.")
    if anchor_monday is None:
        now = datetime.now(timezone.utc)
        anchor_monday = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    days = {
        slot_local_datetime(
            enabled[item], timezone_name, anchor_monday=anchor_monday
        ).weekday()
        for item in unique
    }
    if len(days) < 2:
        raise AvailabilityError("Availability must cover at least two local weekdays.")
    return unique
