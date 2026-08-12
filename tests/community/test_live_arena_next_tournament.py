from datetime import UTC, datetime

import pytest

from modules.community.live_arena.next_tournament import (
    NextTournamentService,
    _parse_local_datetime,
    _validate_basics,
)
from modules.community.live_arena.registration import RegistrationError


def test_validate_basics_accepts_normal_tournament_values():
    values = _validate_basics(
        "C1C Live Arena Cup",
        "Arena Cup",
        "8",
        "16",
        "Europe/Vienna",
    )
    assert values == ("C1C Live Arena Cup", "Arena Cup", 8, 16, "Europe/Vienna")


def test_validate_basics_rejects_bad_limits_and_timezone():
    with pytest.raises(RegistrationError):
        _validate_basics("Cup", "Cup", "16", "8", "UTC")
    with pytest.raises(RegistrationError):
        _validate_basics("Cup", "Cup", "8", "16", "Not/A_Timezone")


def test_parse_local_datetime_converts_from_tournament_timezone_to_utc():
    parsed = _parse_local_datetime(
        "2026-09-01 18:00", "Europe/Vienna", "signup opening time"
    )
    assert parsed.tzinfo is UTC
    assert parsed == datetime(2026, 9, 1, 16, 0, tzinfo=UTC)


def test_parse_local_datetime_rejects_non_calendar_input():
    with pytest.raises(RegistrationError, match="YYYY-MM-DD HH:MM"):
        _parse_local_datetime("tomorrow evening", "UTC", "signup opening time")


def test_new_tournament_id_is_unique_when_timestamp_collides():
    now = datetime(2026, 8, 12, 7, 30, 0, tzinfo=UTC)
    rows = [
        {"tournament_id": "LA-20260812-073000"},
        {"tournament_id": "LA-20260812-073000-2"},
    ]
    assert NextTournamentService._new_id(rows, now) == "LA-20260812-073000-3"
