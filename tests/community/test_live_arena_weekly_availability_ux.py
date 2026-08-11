import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from modules.community.live_arena import views
from modules.community.live_arena.registration import (
    LocalizedSlot,
    RegistrationService,
    RegistrationSnapshot,
    SignupPreparation,
)


def run(awaitable):
    return asyncio.run(awaitable)


def _slots():
    base = datetime(2026, 8, 10, 18, tzinfo=UTC)
    localized = tuple(
        LocalizedSlot(
            f"slot-{day}",
            base + timedelta(days=day),
            base + timedelta(days=day, hours=2),
        )
        for day in range(7)
    )
    rows = tuple(
        {
            "slot_id": slot.slot_id,
            "weekday_utc": slot.local_start.strftime("%A"),
            "start_time_utc": slot.local_start.strftime("%H:%M"),
            "end_time_utc": slot.local_end.strftime("%H:%M"),
            "enabled": "TRUE",
        }
        for slot in localized
    )
    return rows, localized


def _snapshot(*, tournament_status="active", can_update=True):
    rows, localized = _slots()
    return RegistrationSnapshot(
        config={"ACTIVE_TOURNAMENT_ID": "cup"},
        tournament={
            "tournament_name": "Weekly Cup",
            "status": tournament_status,
            "signup_closes_at_utc": "2026-08-12T22:51:00+00:00",
        },
        participant={"status": "confirmed", "timezone": "UTC"},
        status="confirmed",
        timezone="UTC",
        slots=rows,
        selected_slot_ids=("slot-0", "slot-1", "slot-2"),
        localized_slots=localized[:3],
        tournament_status=tournament_status,
        can_update=can_update,
        can_withdraw=False,
    )


def test_weekly_editor_uses_weekday_buttons_and_no_calendar_date():
    rows, localized = _slots()
    preparation = SignupPreparation(
        {"ACTIVE_TOURNAMENT_ID": "cup"},
        {
            "tournament_name": "Weekly Cup",
            "signup_closes_at_utc": "2026-08-12T22:51:00+00:00",
        },
        rows,
        localized,
    )
    view = views.AvailabilityView(
        SimpleNamespace(),
        SimpleNamespace(),
        preparation,
        "UTC",
        SimpleNamespace(id=1),
        selected=("slot-0", "slot-1", "slot-2"),
        updating=True,
    )

    labels = [getattr(item, "label", None) for item in view.children]
    for weekday in ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"):
        assert weekday in labels
    assert "Previous Day" not in labels
    assert "Next Day" not in labels
    assert "Change Timezone" in labels

    embed = view.embed()
    assert embed.title == "Weekly Availability — Monday"
    assert "repeat every week" in embed.description
    assert "specific calendar date" in embed.description
    assert "10 August" not in embed.description


def test_registration_summary_is_weekly_and_date_free():
    embed = views.registration_embed(_snapshot())

    assert "**Monday**" in embed.description
    assert "**Tuesday**" in embed.description
    assert "**Wednesday**" in embed.description
    assert "repeat every week" in embed.description
    assert "specific calendar dates" in embed.description
    assert "10 August" not in embed.description
    assert "11 August" not in embed.description


def test_timezone_change_screen_can_keep_current_timezone():
    view = views.TimezoneSelectView(
        SimpleNamespace(), service=SimpleNamespace(), snapshot=_snapshot()
    )
    labels = [getattr(item, "label", None) for item in view.children]
    assert "Keep Current Timezone" in labels


def test_update_availability_opens_weekly_editor_without_timezone_gate(monkeypatch):
    current = _snapshot(tournament_status="active", can_update=True)
    weekly_view = SimpleNamespace(embed=lambda: discord.Embed(title="Weekly Availability"))
    prepare = AsyncMock(return_value=weekly_view)
    monkeypatch.setattr(views, "_prepare_availability", prepare)

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=7),
        response=SimpleNamespace(defer=AsyncMock()),
        edit_original_response=AsyncMock(),
    )
    action_view = views.RegistrationActionsView(
        SimpleNamespace(), SimpleNamespace(), current
    )
    update_button = next(item for item in action_view.children if item.label == "Update Availability")

    run(update_button.callback(interaction))

    interaction.response.defer.assert_awaited_once()
    prepare.assert_awaited_once()
    assert prepare.await_args.args[2] == "UTC"
    assert prepare.await_args.kwargs["snapshot"] is current
    edited = interaction.edit_original_response.await_args.kwargs
    assert edited["view"] is weekly_view
    assert edited["embed"].title == "Weekly Availability"


def test_active_tournament_allows_confirmed_player_to_update_availability(monkeypatch):
    rows, _ = _slots()
    participants = [
        {
            "tournament_id": "cup",
            "discord_user_id": "7",
            "status": "confirmed",
            "timezone": "UTC",
            "updated_at_utc": "",
        }
    ]
    availability = []
    repo = SimpleNamespace(
        participants=AsyncMock(return_value=participants),
        availability=AsyncMock(return_value=availability),
        persist_core_state=AsyncMock(),
    )
    service = RegistrationService(
        "sheet",
        repository=repo,
        clock=lambda: datetime(2026, 8, 12, tzinfo=UTC),
    )
    service._context = AsyncMock(
        return_value=(
            {"ACTIVE_TOURNAMENT_ID": "cup"},
            {
                "tournament_id": "cup",
                "status": "active",
                "signup_closes_at_utc": "2026-08-12T22:51:00+00:00",
            },
            [],
            rows,
        )
    )
    service._audit = AsyncMock()

    run(service.update_availability("7", "UTC", ["slot-0", "slot-1", "slot-2"]))

    repo.persist_core_state.assert_awaited_once()
    service._audit.assert_awaited_once()
    assert participants[0]["timezone"] == "UTC"
