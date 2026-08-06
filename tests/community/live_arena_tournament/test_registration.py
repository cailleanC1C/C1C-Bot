from datetime import datetime, timezone
import pytest
from modules.community.live_arena_tournament.config import parse_system_config
from modules.community.live_arena_tournament.models import (
    AvailabilityError,
    AvailabilitySlot,
    SchemaError,
    slot_local_datetime,
    validate_availability,
)
from modules.community.live_arena_tournament.service import LiveArenaService

TABLES = (
    "tournament_config",
    "eligible_clans",
    "tournament_roles",
    "destinations",
    "participants",
    "availability_slots",
    "participant_availability",
    "messages",
    "message_components",
    "bot_state",
    "audit_log",
)


def config_rows():
    return [
        ["Key", "Value"],
        ["active_tournament_id", "T1"],
        *[[f"tab.{x}", f"route-{x}"] for x in TABLES],
    ]


def test_system_config_routes_without_fallback_names():
    cfg = parse_system_config(config_rows(), "sheet")
    assert cfg.tabs["participants"] == "route-participants"


def test_missing_route_is_actionable():
    with pytest.raises(SchemaError, match=r"tab.audit_log"):
        parse_system_config(config_rows()[:-1], "sheet")


def slots():
    return [
        AvailabilitySlot("a", 0, "23:00", "01:00"),
        AvailabilitySlot("b", 1, "12:00", "14:00"),
        AvailabilitySlot("c", 2, "12:00", "14:00"),
        AvailabilitySlot("off", 3, "12:00", "14:00", False),
    ]


def test_availability_deduplicates_and_spans_local_days():
    assert validate_availability(["a", "a", "b", "c"], slots(), "Europe/Vienna") == [
        "a",
        "b",
        "c",
    ]


def test_availability_minimum_and_disabled():
    with pytest.raises(AvailabilityError, match="at least"):
        validate_availability(["a", "b"], slots(), "UTC")
    with pytest.raises(AvailabilityError, match="enabled"):
        validate_availability(["a", "b", "off"], slots(), "UTC")


def test_availability_one_day_fails():
    same = [AvailabilitySlot(str(i), 0, f"{i * 2:02}:00", "") for i in range(3)]
    with pytest.raises(AvailabilityError, match="two local weekdays"):
        validate_availability(["0", "1", "2"], same, "UTC")


def test_timezone_conversion_is_dst_aware():
    winter = slot_local_datetime(slots()[0], "Europe/Vienna")
    summer = slot_local_datetime(
        slots()[0],
        "Europe/Vienna",
        anchor_monday=datetime(2026, 7, 6, tzinfo=timezone.utc),
    )
    assert winter.utcoffset() != summer.utcoffset()


def test_eligibility_uses_role_ids_and_removed_is_detectable():
    rows = [
        {
            "tournament_id": "T1",
            "discord_role_id": "7",
            "clan_tag": "C1CM",
            "active": "true",
        }
    ]
    assert LiveArenaService.eligible_clan({7}, rows, "T1") == "C1CM"
    with pytest.raises(Exception):
        LiveArenaService.eligible_clan({8}, rows, "T1")


def test_transitions_are_registration_only():
    assert LiveArenaService.can_transition("draft", "signup_open")
    assert LiveArenaService.can_transition("signup_open", "signup_closed")
    assert not LiveArenaService.can_transition("draft", "signup_closed")
