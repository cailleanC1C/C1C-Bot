import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from modules.community.live_arena.registration import LocalizedSlot, RegistrationError, SignupPreparation
from modules.community.live_arena import views


def _preparation():
    monday = datetime(2026, 8, 10, 18, tzinfo=UTC)
    slots = []
    localized = []
    for index, (day_offset, hour_offset) in enumerate(((0, 0), (0, 2), (1, 0), (1, 2))):
        start = monday + timedelta(days=day_offset, hours=hour_offset)
        slot_id = f"slot-{index}"
        slots.append(
            {
                "slot_id": slot_id,
                "weekday_utc": start.strftime("%A"),
                "start_time_utc": start.strftime("%H:%M"),
                "end_time_utc": (start + timedelta(hours=2)).strftime("%H:%M"),
                "enabled": "TRUE",
            }
        )
        localized.append(LocalizedSlot(slot_id, start, start + timedelta(hours=2)))
    return SignupPreparation(
        {"ACTIVE_TOURNAMENT_ID": "LA-1"},
        {
            "tournament_name": "Trial Cup",
            "signup_closes_at_utc": "2026-08-12T22:51:00Z",
        },
        tuple(slots),
        tuple(localized),
    )


def _snapshot(timezone="Europe/Vienna"):
    preparation = _preparation()
    return SimpleNamespace(
        timezone=timezone,
        slots=preparation.slots,
        selected_slot_ids=("slot-0", "slot-2"),
        config=preparation.config,
        tournament=preparation.tournament,
    )


def test_timezone_options_are_human_readable_and_map_to_iana_values():
    mapping = dict(views.TIMEZONE_OPTIONS)
    assert mapping["US/Canada Eastern — New York / Toronto"] == "America/New_York"
    assert mapping["UK & Ireland — London / Dublin"] == "Europe/London"
    assert mapping["Central Europe — Vienna / Berlin / Paris"] == "Europe/Vienna"
    assert mapping["India — Delhi / Mumbai"] == "Asia/Kolkata"
    assert mapping["Australia Eastern — Sydney / Melbourne"] == "Australia/Sydney"
    assert mapping["New Zealand — Auckland"] == "Pacific/Auckland"
    assert all("/" in timezone for _, timezone in views.TIMEZONE_OPTIONS)


def test_timezone_prompt_explains_local_time_without_iana_jargon():
    embed = views.timezone_prompt_embed()
    assert "Choose the region" in embed.description
    assert "local time" in embed.description
    assert "daylight-saving" in embed.description
    assert "IANA" not in embed.description


def test_timezone_select_has_other_fallback_and_existing_timezone_default():
    view = views.TimezoneSelectView(
        SimpleNamespace(), service=SimpleNamespace(), snapshot=_snapshot()
    )
    options = view.children[0].options
    selected = [option for option in options if option.default]
    assert len(selected) == 1
    assert selected[0].value == "Europe/Vienna"
    assert options[-1].value == views.TIMEZONE_OTHER
    assert "Other" in options[-1].label


def test_unlisted_saved_timezone_defaults_to_other():
    view = views.TimezoneSelectView(
        SimpleNamespace(), service=SimpleNamespace(), snapshot=_snapshot("America/Phoenix")
    )
    assert view.children[0].options[-1].default is True


def test_manual_timezone_error_is_player_friendly():
    embed = views._timezone_player_error(
        RegistrationError("timezone must be a valid IANA timezone")
    )
    assert "wasn't recognized" in embed.description
    assert "Europe/London" in embed.description
    assert "IANA" not in embed.description


def test_availability_picker_explicitly_supports_multiple_windows():
    flow = views.AvailabilityView(
        SimpleNamespace(),
        SimpleNamespace(),
        _preparation(),
        "Europe/Vienna",
        SimpleNamespace(id=7),
    )
    select = flow.children[0]
    assert select.max_values == 2
    assert "multiple choices allowed" in select.placeholder
    assert "Select ALL time windows" in flow.embed().description
    assert "You can choose more than one" in flow.embed().description
    assert "3 windows across 2 different days" in flow.embed().description


def test_availability_count_shows_progress_against_minimums():
    flow = views.AvailabilityView(
        SimpleNamespace(),
        SimpleNamespace(),
        _preparation(),
        "Europe/Vienna",
        SimpleNamespace(id=7),
        selected=("slot-0", "slot-2"),
    )
    description = flow.embed().description
    assert "2 of minimum 3" in description
    assert "2 of minimum 2" in description
    assert "Central Europe" in description


def test_known_timezone_selection_reuses_existing_preflight(monkeypatch):
    preparation = _preparation()
    service = SimpleNamespace(
        initialize=AsyncMock(),
        prepare_signup=AsyncMock(return_value=preparation),
    )
    manager = SimpleNamespace(
        sheet_id="sheet",
        service_factory=lambda _sheet: service,
    )
    monkeypatch.setattr(
        views,
        "load_pr3_config",
        AsyncMock(return_value=({"MESSAGES_TAB": "MESSAGES"}, [])),
    )
    monkeypatch.setattr(views, "load_messages", AsyncMock(return_value={}))
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=7, roles=[]),
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )
    timezone_view = views.TimezoneSelectView(manager)
    timezone_view.select._values = ["Europe/Vienna"]
    asyncio.run(timezone_view._selected(interaction))
    service.initialize.assert_awaited_once()
    service.prepare_signup.assert_awaited_once_with("7", [], "Europe/Vienna")
    sent = interaction.edit_original_response.await_args.kwargs
    assert isinstance(sent["view"], views.AvailabilityView)
    assert isinstance(sent["embed"], discord.Embed)


def test_update_timezone_selector_preserves_saved_slot_ids(monkeypatch):
    snapshot = _snapshot()
    service = SimpleNamespace()
    manager = SimpleNamespace()
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=7),
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )
    timezone_view = views.TimezoneSelectView(
        manager, service=service, snapshot=snapshot
    )
    timezone_view.select._values = ["Europe/Vienna"]
    asyncio.run(timezone_view._selected(interaction))
    flow = interaction.edit_original_response.await_args.kwargs["view"]
    assert flow.updating is True
    assert flow.selected == {"slot-0", "slot-2"}
