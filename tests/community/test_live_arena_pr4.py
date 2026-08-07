import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from modules.community.live_arena import messages, views
from modules.community.live_arena.registration import (
    LocalizedSlot,
    RegistrationError,
    RegistrationSnapshot,
    SignupPreparation,
)


def run(value):
    return asyncio.run(value)


def snapshot(*, status="confirmed", tournament_status="signup_open"):
    base = datetime(2026, 8, 3, 18, tzinfo=UTC)
    localized = tuple(
        LocalizedSlot(f"slot-{i}", base + timedelta(days=i // 2, hours=2 * (i % 2)), base + timedelta(days=i // 2, hours=2 * (i % 2) + 2))
        for i in range(4)
    )
    rows = tuple(
        dict(slot_id=s.slot_id, weekday_utc=s.local_start.strftime("%A"), start_time_utc=s.local_start.strftime("%H:%M"), end_time_utc=s.local_end.strftime("%H:%M"), enabled="TRUE")
        for s in localized
    )
    return RegistrationSnapshot(
        {"ACTIVE_TOURNAMENT_ID": "LA-1"},
        {"tournament_name": "Trial Cup", "signup_closes_at_utc": "2026-08-09T20:00:00Z"},
        {"status": status, "timezone": "UTC"}, status, "UTC", rows,
        tuple(s.slot_id for s in localized[:3]), localized[:3], tournament_status,
        status == "confirmed" and tournament_status == "signup_open",
        status == "confirmed" and tournament_status in {"signup_open", "signup_closed"},
    )


def test_public_panel_has_exact_persistent_player_actions():
    panel = views.JoinTournamentView(object())
    assert panel.timeout is None
    assert [(item.label, item.custom_id, item.style) for item in panel.children] == [
        ("Join Tournament", "live_arena:join", discord.ButtonStyle.primary),
        ("My Registration", "live_arena:my_registration", discord.ButtonStyle.secondary),
    ]


@pytest.mark.parametrize(
    ("status", "tournament_status", "labels"),
    [
        ("confirmed", "signup_open", ["Update Availability", "Withdraw"]),
        ("confirmed", "signup_closed", ["Withdraw"]),
        ("confirmed", "completed", []),
        ("withdrawn", "signup_open", []),
        ("removed", "signup_open", []),
        ("disqualified", "signup_open", []),
        ("unexpected", "signup_open", []),
    ],
)
def test_registration_actions_follow_exact_status_rules(status, tournament_status, labels):
    view = views.RegistrationActionsView(object(), object(), snapshot(status=status, tournament_status=tournament_status))
    assert [item.label for item in view.children] == labels


def test_registration_embed_has_saved_counts_timezone_and_grouped_days():
    embed = views.registration_embed(snapshot())
    for expected in ("Trial Cup", "confirmed", "UTC", "**Windows:** 3", "**Local days:** 2", "Monday", "Tuesday"):
        assert expected in embed.description


def test_withdrawn_embed_points_to_join_without_restore():
    embed = views.registration_embed(snapshot(status="withdrawn"))
    assert "Join Tournament" in embed.description
    assert "Restore" not in embed.description


def test_update_timezone_prefills_and_preserves_real_slot_ids():
    current = snapshot()
    manager = SimpleNamespace()
    modal = views.UpdateTimezoneModal(manager, object(), current)
    assert modal.timezone_input.default == "UTC"
    modal.timezone_input._value = "Asia/Kolkata"
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=7), response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    run(modal.on_submit(interaction))
    view = interaction.followup.send.await_args.kwargs["view"]
    assert view.timezone == "Asia/Kolkata"
    assert view.selected == set(current.selected_slot_ids)


@pytest.mark.parametrize(
    ("error", "logged", "visible"),
    [(RegistrationError("bad timezone"), False, "bad timezone"), (RuntimeError("boom"), True, "Something went wrong")],
)
def test_update_timezone_logs_only_unexpected_failures(monkeypatch, caplog, error, logged, visible):
    current = snapshot()
    modal = views.UpdateTimezoneModal(SimpleNamespace(), object(), current)
    modal.timezone_input._value = "UTC"
    monkeypatch.setattr(views, "localize_availability", lambda *_args: (_ for _ in ()).throw(error))
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=7), response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    caplog.set_level("ERROR", logger="c1c.community.live_arena.views")
    run(modal.on_submit(interaction))
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert visible in embed.description
    assert ("availability preparation failed" in caplog.text) is logged


def test_update_save_calls_pr2_only_and_uses_exact_template(monkeypatch):
    current = snapshot()
    preparation = SignupPreparation(current.config, current.tournament, current.slots, current.localized_slots)
    service = SimpleNamespace(update_availability=AsyncMock())
    manager = SimpleNamespace(sheet_id="sheet", sync=AsyncMock())
    member = SimpleNamespace(id=7, mention="<@7>")
    flow = views.AvailabilityView(manager, service, preparation, "UTC", member, selected=current.selected_slot_ids, updating=True)
    config = {"MESSAGES_TAB": "MESSAGES", "PARTICIPANT_ROLE_ID": "99"}
    template = messages.MessageTemplate("availability_updated", "Availability updated", "{participant}, your timezone and availability for {tournament_name} were updated.", 1)
    monkeypatch.setattr(views, "load_pr3_config", AsyncMock(return_value=(config, [])))
    monkeypatch.setattr(views, "load_messages", AsyncMock(return_value={"availability_updated": template}))
    interaction = SimpleNamespace(user=member, response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    review = views.ReviewView(flow)
    run(review.children[1].callback(interaction))
    args = service.update_availability.await_args.args
    assert args[:2] == ("7", "UTC") and set(args[2]) == set(current.selected_slot_ids)
    manager.sync.assert_not_awaited()
    assert interaction.followup.send.await_args.kwargs["embed"].description == "<@7>, your timezone and availability for Trial Cup were updated."


def test_update_save_unexpected_failure_is_logged_and_generic(monkeypatch, caplog):
    current = snapshot()
    preparation = SignupPreparation(current.config, current.tournament, current.slots, current.localized_slots)
    service = SimpleNamespace(update_availability=AsyncMock(side_effect=RuntimeError("database detail")))
    manager = SimpleNamespace(sheet_id="sheet", sync=AsyncMock())
    member = SimpleNamespace(id=7, mention="<@7>")
    flow = views.AvailabilityView(manager, service, preparation, "UTC", member, selected=current.selected_slot_ids, updating=True)
    monkeypatch.setattr(views, "load_pr3_config", AsyncMock(return_value=({"MESSAGES_TAB": "MESSAGES"}, [])))
    monkeypatch.setattr(views, "load_messages", AsyncMock(return_value={}))
    interaction = SimpleNamespace(user=member, response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
    caplog.set_level("ERROR", logger="c1c.community.live_arena.views")
    run(views.ReviewView(flow).children[1].callback(interaction))
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert "Something went wrong" in embed.description and "database detail" not in embed.description
    assert "action=update_availability" in caplog.text


def test_withdraw_cancel_is_embed_only_and_does_not_mutate():
    current = snapshot()
    service = SimpleNamespace(withdraw=AsyncMock())
    interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
    view = views.WithdrawalConfirmationView(object(), service, current)
    run(view.children[1].callback(interaction))
    service.withdraw.assert_not_awaited()
    assert isinstance(interaction.response.edit_message.await_args.kwargs["embed"], discord.Embed)


def withdrawal_interaction(role):
    member = SimpleNamespace(id=7, mention="<@7>", roles=[role] if role else [], remove_roles=AsyncMock())
    return SimpleNamespace(
        user=member, guild=SimpleNamespace(get_role=lambda _id: role),
        response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()),
    )


def test_withdraw_calls_pr2_once_then_removes_role_and_refreshes(monkeypatch):
    current = snapshot()
    service = SimpleNamespace(withdraw=AsyncMock())
    manager = SimpleNamespace(sheet_id="sheet", sync=AsyncMock())
    role = SimpleNamespace(id=99)
    interaction = withdrawal_interaction(role)
    config = {"MESSAGES_TAB": "MESSAGES", "PARTICIPANT_ROLE_ID": "99"}
    template = messages.MessageTemplate("withdrawal_confirmed", "Withdrawal recorded", "{participant} has withdrawn from {tournament_name}.", 1)
    monkeypatch.setattr(views, "load_pr3_config", AsyncMock(return_value=(config, [])))
    monkeypatch.setattr(views, "load_messages", AsyncMock(return_value={"withdrawal_confirmed": template}))
    modal = views.WithdrawalReasonModal(manager, service, current)
    modal.reason._value = "busy"
    run(modal.on_submit(interaction))
    service.withdraw.assert_awaited_once_with("7", "busy")
    interaction.user.remove_roles.assert_awaited_once_with(role, reason="Live Arena registration withdrawn")
    manager.sync.assert_awaited_once()
    assert interaction.followup.send.await_args.kwargs["embed"].description == "<@7> has withdrawn from Trial Cup."


def test_signup_closed_withdraws_and_removes_role_without_panel_sync(monkeypatch):
    current = snapshot(tournament_status="signup_closed")
    service = SimpleNamespace(withdraw=AsyncMock())
    manager = SimpleNamespace(sheet_id="sheet", sync=AsyncMock())
    role = SimpleNamespace(id=99)
    interaction = withdrawal_interaction(role)
    template = messages.MessageTemplate("withdrawal_confirmed", "Withdrawal recorded", "{participant} has withdrawn from {tournament_name}.", 1)
    monkeypatch.setattr(views, "load_pr3_config", AsyncMock(return_value=({"MESSAGES_TAB": "MESSAGES", "PARTICIPANT_ROLE_ID": "99"}, [])))
    monkeypatch.setattr(views, "load_messages", AsyncMock(return_value={"withdrawal_confirmed": template}))
    run(views.WithdrawalReasonModal(manager, service, current).on_submit(interaction))
    service.withdraw.assert_awaited_once()
    interaction.user.remove_roles.assert_awaited_once_with(role, reason="Live Arena registration withdrawn")
    manager.sync.assert_not_awaited()
    assert isinstance(interaction.followup.send.await_args.kwargs["embed"], discord.Embed)


@pytest.mark.parametrize("role_error,sync_error", [(RuntimeError("denied"), None), (None, RuntimeError("offline"))])
def test_secondary_withdrawal_failure_keeps_success(monkeypatch, role_error, sync_error):
    current = snapshot()
    service = SimpleNamespace(withdraw=AsyncMock())
    manager = SimpleNamespace(sheet_id="sheet", sync=AsyncMock(side_effect=sync_error))
    role = SimpleNamespace(id=99)
    interaction = withdrawal_interaction(role)
    interaction.user.remove_roles.side_effect = role_error
    config = {"MESSAGES_TAB": "MESSAGES", "PARTICIPANT_ROLE_ID": "99"}
    template = messages.MessageTemplate("withdrawal_confirmed", "Withdrawal recorded", "{participant} has withdrawn from {tournament_name}.", 1)
    monkeypatch.setattr(views, "load_pr3_config", AsyncMock(return_value=(config, [])))
    monkeypatch.setattr(views, "load_messages", AsyncMock(return_value={"withdrawal_confirmed": template}))
    modal = views.WithdrawalReasonModal(manager, service, current)
    run(modal.on_submit(interaction))
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert service.withdraw.await_count == 1 and isinstance(embed, discord.Embed)
    assert bool(embed.fields) is bool(role_error)


def test_withdrawal_rejection_has_no_role_or_refresh(monkeypatch, caplog):
    current = snapshot()
    service = SimpleNamespace(withdraw=AsyncMock(side_effect=Exception("write failed")))
    manager = SimpleNamespace(sheet_id="sheet", sync=AsyncMock())
    role = SimpleNamespace(id=99)
    interaction = withdrawal_interaction(role)
    monkeypatch.setattr(views, "load_pr3_config", AsyncMock(return_value=({"MESSAGES_TAB": "MESSAGES", "PARTICIPANT_ROLE_ID": "99"}, [])))
    monkeypatch.setattr(views, "load_messages", AsyncMock(return_value={}))
    caplog.set_level("ERROR", logger="c1c.community.live_arena.views")
    run(views.WithdrawalReasonModal(manager, service, current).on_submit(interaction))
    interaction.user.remove_roles.assert_not_awaited()
    manager.sync.assert_not_awaited()
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert isinstance(embed, discord.Embed) and "write failed" not in embed.description
    assert "action=withdraw" in caplog.text
