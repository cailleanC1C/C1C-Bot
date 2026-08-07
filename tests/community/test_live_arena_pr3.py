import asyncio
from datetime import date

import pytest

from modules.community.live_arena import messages
from modules.community.live_arena.messages import load_messages, load_pr3_config
from modules.community.live_arena.registration import localize_availability
from modules.community.live_arena.service import LiveArenaConfigError
from modules.community.live_arena.views import JoinTournamentView


CONFIG = [
    ["Key", "Value", "Notes / clear name"],
    ["MESSAGES_TAB", "MESSAGES", ""],
    ["SIGNUP_CHANNEL_ID", "1535020329624146123", ""],
    ["PARTICIPANT_ROLE_ID", "1535031871191257189", ""],
    ["PUBLIC_PANEL_MESSAGE_ID", "", ""],
]
MESSAGE_ROWS = [
    ["message_key", "title", "description", "color_hex", "active", "notes"],
    [
        "signup_open",
        "Live Arena signups are open",
        "{tournament_name} by {signup_deadline}. Confirmed: {confirmed_count}/{max_participants}.",
        "#1A73E8",
        "TRUE",
        "",
    ],
    [
        "signup_confirmed",
        "You're aboard",
        "{participant}, {tournament_name} by {signup_deadline}.",
        "#34A853",
        "TRUE",
        "",
    ],
]


def test_literal_pr3_config_and_messages_render(monkeypatch):
    async def fetch(_sheet, tab):
        return CONFIG if tab == "CONFIG" else MESSAGE_ROWS

    monkeypatch.setattr(messages, "afetch_values", fetch)
    config, _ = asyncio.run(load_pr3_config("sheet"))
    loaded = asyncio.run(load_messages("sheet", config["MESSAGES_TAB"]))
    embed = loaded["signup_open"].embed(
        tournament_name="Trial Cup",
        signup_deadline="<t:1:F>",
        confirmed_count=4,
        max_participants=16,
    )
    assert set(loaded) == {"signup_open", "signup_confirmed"}
    assert "Trial Cup" in embed.description and "4/16" in embed.description


def test_unexpected_message_header_fails(monkeypatch):
    matrix = [MESSAGE_ROWS[0] + ["message"], *(row + [""] for row in MESSAGE_ROWS[1:])]

    async def fetch(*_args):
        return matrix

    monkeypatch.setattr(messages, "afetch_values", fetch)
    with pytest.raises(LiveArenaConfigError, match="unexpected header"):
        asyncio.run(load_messages("sheet", "MESSAGES"))


def test_localization_handles_utc_cross_midnight():
    slots = [
        dict(
            slot_id="real-slot",
            weekday_utc="Monday",
            start_time_utc="23:00",
            end_time_utc="01:00",
            enabled="TRUE",
        )
    ]
    localized = localize_availability("Asia/Kolkata", slots, "2026-08-09T20:00:00Z")
    assert localized[0].slot_id == "real-slot"
    assert localized[0].local_start.date() == date(2026, 8, 4)
    assert localized[0].local_start.hour == 4


def test_persistent_join_contract():
    view = JoinTournamentView(object())
    assert view.timeout is None
    button = view.children[0]
    assert button.custom_id == "live_arena:join"
    assert button.label == "Join Tournament"
