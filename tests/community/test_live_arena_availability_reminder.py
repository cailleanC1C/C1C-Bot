import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from modules.community.live_arena import availability_reminder, qualification_panel, result_views


def run(awaitable):
    return asyncio.run(awaitable)


def test_match_result_controls_include_weekly_availability_shortcut():
    view = result_views.MatchResultView("sheet")
    labels = [getattr(item, "label", "") for item in view.children]
    assert "Report Result" in labels
    assert "Dispute Result" in labels
    assert "Review / Update Weekly Availability" in labels


def test_match_embed_explains_recurring_weekly_availability():
    tournament = {"tournament_name": "Trial Cup"}
    round_row = {
        "round_name": "Qualification Round 2",
        "deadline_at_utc": "2026-08-18T20:00:00Z",
    }
    match = {
        "player_a_discord_user_id": "1",
        "player_b_discord_user_id": "2",
        "shared_slot_ids_csv": "",
        "match_number": "1",
    }
    embed = qualification_panel.match_embed(tournament, round_row, match, [])
    assert "**Weekly availability reminder**" in embed.description
    assert "recurring **day-of-the-week** schedule" in embed.description
    assert "not a set of calendar dates" in embed.description
    assert "never changes your opponent" in embed.description
    assert "Review / Update Weekly Availability" in embed.description


def test_availability_shortcut_opens_saved_editor_directly(monkeypatch):
    snapshot = SimpleNamespace(
        participant={"status": "confirmed"},
        can_update=True,
        timezone="Europe/Vienna",
    )
    service = SimpleNamespace(
        initialize=AsyncMock(),
        get_registration=AsyncMock(return_value=snapshot),
    )

    class FakeRegistrationService:
        def __new__(cls, sheet_id):
            assert sheet_id == "sheet"
            return service

    editor = SimpleNamespace(embed=lambda: discord.Embed(title="Weekly Availability"))
    prepare = AsyncMock(return_value=editor)
    monkeypatch.setattr(availability_reminder, "RegistrationService", FakeRegistrationService)
    monkeypatch.setattr(availability_reminder.views, "_prepare_availability", prepare)

    interaction = SimpleNamespace(
        user=SimpleNamespace(id=7),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    button = availability_reminder.WeeklyAvailabilityShortcutButton("sheet")
    run(button.callback(interaction))

    service.initialize.assert_awaited_once()
    service.get_registration.assert_awaited_once_with("7")
    prepare.assert_awaited_once()
    assert prepare.await_args.args[2] == "Europe/Vienna"
    assert prepare.await_args.kwargs["snapshot"] is snapshot
    sent = interaction.followup.send.await_args.kwargs
    assert sent["view"] is editor
    assert sent["embed"].title == "Weekly Availability"
    assert sent["ephemeral"] is True


def test_availability_shortcut_is_persistent():
    view = availability_reminder.WeeklyAvailabilityShortcutView("sheet")
    assert view.timeout is None
    assert len(view.children) == 1
    assert view.children[0].custom_id == "live_arena:availability:review_update"
