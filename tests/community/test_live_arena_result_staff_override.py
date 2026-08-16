from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.community.live_arena import result_lifecycle_ux as ux
from modules.community.live_arena import result_staff_override as override
from modules.community.live_arena.registration import RegistrationError


def run(awaitable):
    return asyncio.run(awaitable)


def match(*, reporter="1", a="1", b="2"):
    return {
        "reported_by_discord_user_id": reporter,
        "player_a_discord_user_id": a,
        "player_b_discord_user_id": b,
    }


def interaction(user_id):
    return SimpleNamespace(user=SimpleNamespace(id=int(user_id), roles=[]))


def test_staff_reporter_can_proxy_non_reporting_participant(monkeypatch):
    monkeypatch.setattr(ux, "_is_organizer", AsyncMock(return_value=True))

    represented, proxied = run(
        override._represented_opponent_with_staff_override(
            ux, interaction("1"), "sheet", match(reporter="1", a="1", b="2")
        )
    )

    assert represented == "2"
    assert proxied is True


def test_non_staff_reporter_is_still_blocked(monkeypatch):
    monkeypatch.setattr(ux, "_is_organizer", AsyncMock(return_value=False))

    with pytest.raises(RegistrationError, match="cannot confirm or dispute"):
        run(
            override._represented_opponent_with_staff_override(
                ux, interaction("1"), "sheet", match(reporter="1", a="1", b="2")
            )
        )


def test_non_reporting_participant_acts_as_player_even_if_staff(monkeypatch):
    monkeypatch.setattr(ux, "_is_organizer", AsyncMock(return_value=True))

    represented, proxied = run(
        override._represented_opponent_with_staff_override(
            ux, interaction("2"), "sheet", match(reporter="1", a="1", b="2")
        )
    )

    assert represented == "2"
    assert proxied is False
    ux._is_organizer.assert_not_awaited()


def test_staff_outsider_can_proxy_non_reporting_participant(monkeypatch):
    monkeypatch.setattr(ux, "_is_organizer", AsyncMock(return_value=True))

    represented, proxied = run(
        override._represented_opponent_with_staff_override(
            ux, interaction("900"), "sheet", match(reporter="1", a="1", b="2")
        )
    )

    assert represented == "2"
    assert proxied is True


def test_non_staff_outsider_is_rejected(monkeypatch):
    monkeypatch.setattr(ux, "_is_organizer", AsyncMock(return_value=False))

    with pytest.raises(RegistrationError, match="configured tournament organizer"):
        run(
            override._represented_opponent_with_staff_override(
                ux, interaction("900"), "sheet", match(reporter="1", a="1", b="2")
            )
        )
