"""Remaining PR5 lifecycle regressions from Issue #1071's frozen contract."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from modules.community.live_arena import organizer as organizer_module
from modules.community.live_arena import organizer_panel as organizer_panel_module
from modules.community.live_arena import panel as panel_module
from modules.community.live_arena import views
from modules.community.live_arena.messages import MessageTemplate
from modules.community.live_arena.organizer import OrganizerService
from modules.community.live_arena.organizer_panel import (
    ConfirmParticipant,
    ConfirmReconcile,
    ConfirmTransition,
    OrganizerPanelManager,
    OrganizerView,
)
from modules.community.live_arena.panel import LiveArenaPanelManager, PanelSyncResult
from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import TournamentSnapshot


def run(awaitable):
    return asyncio.run(awaitable)


class _Template:
    def __init__(self, title="Panel"):
        self.title = title

    def embed(self, **values):
        return discord.Embed(title=self.title, description=str(values))


class _Message:
    def __init__(self, message_id=55):
        self.id = message_id
        self.edit = AsyncMock()
        self.delete = AsyncMock()


class _Channel:
    def __init__(self, fetched=None, fetch_error=None, guild=None):
        self.fetched = fetched
        self.fetch_error = fetch_error
        self.guild = guild
        self.sent = []

    async def fetch_message(self, _message_id):
        if self.fetch_error:
            raise self.fetch_error
        return self.fetched

    async def send(self, **_kwargs):
        message = _Message(100 + len(self.sent))
        self.sent.append(message)
        return message


def _not_found():
    return discord.NotFound(
        SimpleNamespace(status=404, reason="missing"), "missing"
    )


def _organizer_sync_manager(
    monkeypatch,
    *,
    panel_id="",
    status="draft",
    channel=None,
    sheet_id="sheet-organizer-sync",
):
    channel = channel or _Channel(guild=SimpleNamespace())
    config = {
        "ORGANIZER_CHANNEL_ID": "10",
        "ORGANIZER_PANEL_MESSAGE_ID": panel_id,
        "PARTICIPANT_ROLE_ID": "99",
        "MESSAGES_TAB": "MESSAGES",
    }
    tournament = SimpleNamespace(
        tournament_id="cup",
        tournament_name="Trial Cup",
        status=status,
        max_participants=16,
    )
    counts = {"confirmed": 0, "withdrawn": 0, "removed": 0, "disqualified": 0}
    parity = {"missing": [], "extra": [], "unresolved": []}
    monkeypatch.setattr(
        organizer_panel_module,
        "load_pr5_config",
        AsyncMock(return_value=(dict(config), [])),
    )
    monkeypatch.setattr(
        organizer_panel_module,
        "load_messages",
        AsyncMock(return_value={"organizer_panel": _Template("Organizer")}),
    )
    bot = SimpleNamespace(
        get_channel=lambda _channel_id: channel,
        fetch_channel=AsyncMock(return_value=channel),
    )
    manager = OrganizerPanelManager(bot, sheet_id, SimpleNamespace(sync=AsyncMock()))
    manager.data = AsyncMock(
        return_value=(dict(config), tournament, [], counts, parity)
    )
    manager._persist = AsyncMock()
    return manager, channel


def test_organizer_panel_exists_in_draft_and_persists_created_id(monkeypatch):
    manager, channel = _organizer_sync_manager(
        monkeypatch, status="draft", sheet_id="sheet-organizer-draft"
    )

    result = run(manager.sync())

    assert result == PanelSyncResult(True)
    assert len(channel.sent) == 1
    manager._persist.assert_awaited_once_with(
        manager.data.await_args_list[0].args and manager.data.return_value[0]
        or manager.data.return_value[0],
        str(channel.sent[0].id),
    )


def test_organizer_existing_panel_edits_without_duplicate(monkeypatch):
    existing = _Message(77)
    manager, channel = _organizer_sync_manager(
        monkeypatch,
        panel_id="77",
        status="signup_open",
        channel=_Channel(fetched=existing, guild=SimpleNamespace()),
        sheet_id="sheet-organizer-edit",
    )

    result = run(manager.sync())

    assert result == PanelSyncResult(True)
    existing.edit.assert_awaited_once()
    assert channel.sent == []
    manager._persist.assert_not_awaited()


def test_organizer_notfound_recreates_once_and_transient_fetch_does_not(monkeypatch):
    recreate, recreate_channel = _organizer_sync_manager(
        monkeypatch,
        panel_id="77",
        channel=_Channel(fetch_error=_not_found(), guild=SimpleNamespace()),
        sheet_id="sheet-organizer-notfound",
    )
    result = run(recreate.sync())
    assert result == PanelSyncResult(True)
    assert len(recreate_channel.sent) == 1
    recreate._persist.assert_awaited_once()

    transient, transient_channel = _organizer_sync_manager(
        monkeypatch,
        panel_id="77",
        channel=_Channel(
            fetch_error=RuntimeError("temporary Discord failure"), guild=SimpleNamespace()
        ),
        sheet_id="sheet-organizer-transient",
    )
    result = run(transient.sync())
    assert result == PanelSyncResult(False, "fetch")
    assert transient_channel.sent == []
    transient._persist.assert_not_awaited()


def test_organizer_edit_failure_reports_failure_without_duplicate(monkeypatch):
    existing = _Message(77)
    existing.edit.side_effect = RuntimeError("edit denied")
    manager, channel = _organizer_sync_manager(
        monkeypatch,
        panel_id="77",
        channel=_Channel(fetched=existing, guild=SimpleNamespace()),
        sheet_id="sheet-organizer-edit-failure",
    )

    result = run(manager.sync())

    assert result == PanelSyncResult(False, "edit")
    assert channel.sent == []


def test_organizer_failed_id_persistence_deletes_untracked_message(monkeypatch):
    manager, channel = _organizer_sync_manager(
        monkeypatch, sheet_id="sheet-organizer-persist-failure"
    )
    manager._persist.side_effect = RuntimeError("write failed")

    with pytest.raises(RuntimeError, match="write failed"):
        run(manager.sync())

    channel.sent[0].delete.assert_awaited_once()


def test_organizer_persist_updates_only_existing_value_cell(monkeypatch):
    matrix = [
        ["Key", "Value", "Notes / clear name"],
        ["ORGANIZER_CHANNEL_ID", "10", "channel"],
        ["ORGANIZER_PANEL_MESSAGE_ID", "", "bot managed"],
        ["PARTICIPANT_ROLE_ID", "99", "role"],
    ]
    worksheet = SimpleNamespace(update_cell=AsyncMock())
    manager = OrganizerPanelManager(
        SimpleNamespace(), "sheet-persist-cell", SimpleNamespace()
    )

    with (
        patch(
            "shared.sheets.async_core.afetch_values",
            AsyncMock(return_value=matrix),
        ),
        patch(
            "modules.community.live_arena.organizer_panel.aget_worksheet",
            AsyncMock(return_value=worksheet),
        ),
        patch(
            "modules.community.live_arena.organizer_panel.acall_with_backoff",
            AsyncMock(),
        ) as call,
    ):
        run(manager._persist({}, "777"))

    assert call.await_count == 1
    assert call.await_args.args == (worksheet.update_cell, 3, 2, "777")


def test_concurrent_organizer_sync_creates_exactly_one_panel(monkeypatch):
    state = {
        "ORGANIZER_CHANNEL_ID": "10",
        "ORGANIZER_PANEL_MESSAGE_ID": "",
        "PARTICIPANT_ROLE_ID": "99",
        "MESSAGES_TAB": "MESSAGES",
    }
    channel = _Channel(guild=SimpleNamespace())

    async def fetch_message(message_id):
        return next(message for message in channel.sent if message.id == int(message_id))

    channel.fetch_message = fetch_message

    async def load_config(_sheet_id):
        return dict(state), []

    async def data(_guild=None):
        return (
            dict(state),
            SimpleNamespace(
                tournament_id="cup",
                tournament_name="Trial Cup",
                status="draft",
                max_participants=16,
            ),
            [],
            {"confirmed": 0, "withdrawn": 0, "removed": 0, "disqualified": 0},
            {"missing": [], "extra": [], "unresolved": []},
        )

    async def persist(_config, message_id):
        state["ORGANIZER_PANEL_MESSAGE_ID"] = message_id

    monkeypatch.setattr(organizer_panel_module, "load_pr5_config", load_config)
    monkeypatch.setattr(
        organizer_panel_module,
        "load_messages",
        AsyncMock(return_value={"organizer_panel": _Template("Organizer")}),
    )
    bot = SimpleNamespace(get_channel=lambda _id: channel)
    first = OrganizerPanelManager(
        bot, "sheet-organizer-concurrent", SimpleNamespace(sync=AsyncMock())
    )
    second = OrganizerPanelManager(
        bot, "sheet-organizer-concurrent", SimpleNamespace(sync=AsyncMock())
    )
    first.data = data
    second.data = data
    first._persist = persist
    second._persist = persist

    async def concurrent():
        await asyncio.gather(first.sync(), second.sync())

    run(concurrent())
    assert len(channel.sent) == 1
    channel.sent[0].edit.assert_awaited_once()


def _public_snapshot(status):
    return TournamentSnapshot(
        "cup",
        "Trial Cup",
        status,
        "selected_clans",
        8,
        16,
        "2026-08-01T00:00:00Z",
        "2026-08-09T20:00:00Z",
        1,
        84,
        50,
    )


def test_public_panel_open_closed_reopen_reuses_same_message(monkeypatch):
    existing = _Message(55)
    channel = _Channel(fetched=existing)
    config = {
        "MESSAGES_TAB": "MESSAGES",
        "SIGNUP_CHANNEL_ID": "10",
        "PUBLIC_PANEL_MESSAGE_ID": "55",
    }
    matrix = [
        ["Key", "Value", "Notes / clear name"],
        ["PUBLIC_PANEL_MESSAGE_ID", "55", "bot managed"],
    ]

    class Repo:
        async def initialize(self):
            return None

        async def participants(self):
            return []

    async def load_messages(_sheet_id, _tab, keys):
        key = next(iter(keys))
        return {key: _Template(key)}

    monkeypatch.setattr(
        panel_module, "load_pr3_config", AsyncMock(return_value=(config, matrix))
    )
    monkeypatch.setattr(
        panel_module,
        "load_tournament_snapshot",
        AsyncMock(
            side_effect=[
                _public_snapshot("signup_open"),
                _public_snapshot("signup_closed"),
                _public_snapshot("signup_open"),
            ]
        ),
    )
    monkeypatch.setattr(panel_module, "load_messages", load_messages)
    monkeypatch.setattr(panel_module, "LiveArenaRepository", lambda _sheet: Repo())
    manager = LiveArenaPanelManager(
        SimpleNamespace(get_channel=lambda _id: channel),
        "sheet-public-lifecycle",
    )

    run(manager.sync())
    run(manager.sync())
    run(manager.sync())

    assert existing.edit.await_count == 3
    assert [
        len(call.kwargs["view"].children) for call in existing.edit.await_args_list
    ] == [2, 1, 2]
    assert channel.sent == []


def test_close_odd_roster_confirmation_warns_without_mutating(monkeypatch):
    manager = SimpleNamespace(
        data=AsyncMock(
            return_value=(
                {},
                SimpleNamespace(tournament_name="Trial Cup"),
                [],
                {"confirmed": 5},
                {},
            )
        )
    )
    interaction = SimpleNamespace(
        guild=SimpleNamespace(),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    view = OrganizerView(manager, "signup_open")

    monkeypatch.setattr(
        OrganizerView, "authorized", AsyncMock(return_value=True)
    )
    run(view.transition(interaction, "close"))

    kwargs = interaction.response.send_message.await_args.kwargs
    assert isinstance(kwargs["embed"], discord.Embed)
    assert "odd" in kwargs["embed"].description.lower()
    assert "no player will be auto-demoted" in kwargs["embed"].description
    assert isinstance(kwargs["view"], ConfirmTransition)
    assert kwargs["ephemeral"] is True


def _participant_row(status="confirmed", *, timezone="UTC"):
    return {
        "tournament_id": "cup",
        "discord_user_id": "7",
        "display_name_at_signup": "Old Name",
        "clan_tag_at_signup": "OLD",
        "timezone": timezone,
        "status": status,
        "signed_up_at_utc": "2026-08-01T00:00:00Z",
        "confirmed_at_utc": "2026-08-01T00:00:00Z",
        "withdrawn_at_utc": "",
        "withdrawal_reason": "",
        "updated_at_utc": "2026-08-01T00:00:00Z",
        "notes": "keep",
    }


def _tournament(status="signup_open", *, maximum=16):
    return {
        "tournament_id": "cup",
        "tournament_name": "Trial Cup",
        "status": status,
        "eligibility_scope": "selected_clans",
        "min_participants": "8",
        "max_participants": str(maximum),
        "signup_opens_at_utc": "2026-08-01T00:00:00Z",
        "signup_closes_at_utc": "2026-08-09T20:00:00Z",
        "notes": "",
    }


def _slots(*, disable_first=False):
    return [
        {
            "slot_id": "MON_0000_0200",
            "weekday_utc": "Monday",
            "start_time_utc": "00:00",
            "end_time_utc": "02:00",
            "enabled": "FALSE" if disable_first else "TRUE",
            "sort_order": "1",
            "display_label": "Monday 00:00–02:00 UTC",
        },
        {
            "slot_id": "MON_0200_0400",
            "weekday_utc": "Monday",
            "start_time_utc": "02:00",
            "end_time_utc": "04:00",
            "enabled": "TRUE",
            "sort_order": "2",
            "display_label": "Monday 02:00–04:00 UTC",
        },
        {
            "slot_id": "TUE_0000_0200",
            "weekday_utc": "Tuesday",
            "start_time_utc": "00:00",
            "end_time_utc": "02:00",
            "enabled": "TRUE",
            "sort_order": "13",
            "display_label": "Tuesday 00:00–02:00 UTC",
        },
    ]


def _availability():
    return [
        {
            "tournament_id": "cup",
            "discord_user_id": "7",
            "slot_id": slot_id,
            "created_at_utc": "2026-08-01T00:00:00Z",
            "updated_at_utc": "2026-08-01T00:00:00Z",
            "notes": "",
        }
        for slot_id in ("MON_0000_0200", "MON_0200_0400", "TUE_0000_0200")
    ]


def _service_with_context(participants, tournament, clans=None, slots=None, availability=None):
    repo = SimpleNamespace(
        participants=AsyncMock(return_value=participants),
        availability=AsyncMock(return_value=availability or []),
        persist_participants=AsyncMock(),
        append_audit=AsyncMock(),
    )
    service = OrganizerService(
        "sheet",
        repository=repo,
        clock=lambda: datetime(2026, 8, 7, 12, tzinfo=UTC),
    )
    service.context = AsyncMock(
        return_value=(
            {"ACTIVE_TOURNAMENT_ID": "cup"},
            (2, tournament),
            clans or [],
            slots or [],
        )
    )
    return service, repo


@pytest.mark.parametrize("tournament_status", ["signup_open", "signup_closed"])
def test_remove_preserves_historical_fields_and_availability(tournament_status):
    row = _participant_row()
    original = dict(row)
    service, repo = _service_with_context([row], _tournament(tournament_status))

    with patch(
        "modules.community.live_arena.organizer.load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": "cup"}),
    ):
        run(service.remove("100", "7"))

    assert row["status"] == "removed"
    assert row["signed_up_at_utc"] == original["signed_up_at_utc"]
    assert row["confirmed_at_utc"] == original["confirmed_at_utc"]
    assert row["timezone"] == original["timezone"]
    assert row["notes"] == "keep"
    repo.availability.assert_not_awaited()
    previous = repo.persist_participants.await_args.kwargs["previous_participants"]
    assert previous[0]["status"] == "confirmed"
    audit = repo.append_audit.await_args.args[0]
    assert audit["event_type"] == "participant_removed"
    assert audit["actor_discord_user_id"] == "100"
    assert audit["target_discord_user_id"] == "7"


@pytest.mark.parametrize(
    ("participant_status", "tournament_status"),
    [("withdrawn", "signup_open"), ("confirmed", "draft")],
)
def test_remove_rejects_wrong_participant_or_tournament_status(
    participant_status, tournament_status
):
    row = _participant_row(participant_status)
    service, repo = _service_with_context([row], _tournament(tournament_status))

    with patch(
        "modules.community.live_arena.organizer.load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": "cup"}),
    ):
        with pytest.raises(RegistrationError):
            run(service.remove("100", "7"))

    repo.persist_participants.assert_not_awaited()
    repo.append_audit.assert_not_awaited()


def _restore_fixture(
    *,
    member_role_id=123,
    maximum=16,
    timezone="UTC",
    disable_first=False,
    extra_confirmed=False,
):
    row = _participant_row("removed", timezone=timezone)
    row["withdrawn_at_utc"] = "2026-08-05T00:00:00Z"
    row["withdrawal_reason"] = "old reason"
    participants = [row]
    if extra_confirmed:
        participants.append(
            {
                **_participant_row("confirmed"),
                "discord_user_id": "8",
                "display_name_at_signup": "Other",
            }
        )
    clans = [
        {
            "tournament_id": "cup",
            "clan_tag": "C1CM",
            "clan_name": "Martyrs",
            "discord_role_id": "123",
            "active": "TRUE",
            "notes": "",
        }
    ]
    service, repo = _service_with_context(
        participants,
        _tournament("signup_open", maximum=maximum),
        clans,
        _slots(disable_first=disable_first),
        _availability(),
    )
    member = SimpleNamespace(
        id=7,
        display_name="Current Name",
        roles=[SimpleNamespace(id=member_role_id)],
    )
    return service, repo, row, member


def test_restore_revalidates_and_restores_same_row_without_replacing_availability():
    service, repo, row, member = _restore_fixture()
    signed_up = row["signed_up_at_utc"]

    with patch(
        "modules.community.live_arena.organizer.load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": "cup"}),
    ):
        run(service.restore("100", "7", member))

    assert row["status"] == "confirmed"
    assert row["signed_up_at_utc"] == signed_up
    assert row["display_name_at_signup"] == "Current Name"
    assert row["clan_tag_at_signup"] == "C1CM"
    assert row["withdrawn_at_utc"] == ""
    assert row["withdrawal_reason"] == ""
    repo.availability.assert_awaited_once()
    assert not hasattr(repo, "persist_availability")
    audit = repo.append_audit.await_args.args[0]
    assert audit["event_type"] == "participant_restored"
    assert audit["actor_discord_user_id"] == "100"
    assert audit["target_discord_user_id"] == "7"


def test_restore_rejects_unresolved_or_ineligible_member_without_mutation():
    service, repo, _row, member = _restore_fixture()
    with patch(
        "modules.community.live_arena.organizer.load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": "cup"}),
    ):
        with pytest.raises(RegistrationError, match="cannot be resolved"):
            run(service.restore("100", "7", None))
    repo.persist_participants.assert_not_awaited()

    service, repo, _row, member = _restore_fixture(member_role_id=999)
    with patch(
        "modules.community.live_arena.organizer.load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": "cup"}),
    ):
        with pytest.raises(RegistrationError, match="eligible clan role"):
            run(service.restore("100", "7", member))
    repo.persist_participants.assert_not_awaited()


def test_restore_rejects_capacity_timezone_and_disabled_saved_slots():
    service, repo, _row, member = _restore_fixture(maximum=1, extra_confirmed=True)
    with patch(
        "modules.community.live_arena.organizer.load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": "cup"}),
    ):
        with pytest.raises(RegistrationError, match="capacity"):
            run(service.restore("100", "7", member))
    repo.persist_participants.assert_not_awaited()

    service, repo, _row, member = _restore_fixture(timezone="Mars/Nope")
    with patch(
        "modules.community.live_arena.organizer.load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": "cup"}),
    ):
        with pytest.raises(RegistrationError, match="IANA"):
            run(service.restore("100", "7", member))
    repo.persist_participants.assert_not_awaited()

    service, repo, _row, member = _restore_fixture(disable_first=True)
    with patch(
        "modules.community.live_arena.organizer.load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": "cup"}),
    ):
        with pytest.raises(RegistrationError, match="disabled"):
            run(service.restore("100", "7", member))
    repo.persist_participants.assert_not_awaited()


@pytest.mark.parametrize("restore", [False, True])
def test_participant_role_failure_and_panel_failure_do_not_rollback_core(
    monkeypatch, restore
):
    role = SimpleNamespace(id=99)
    target = SimpleNamespace(
        id=7,
        roles=[] if restore else [role],
        add_roles=AsyncMock(side_effect=RuntimeError("role denied")),
        remove_roles=AsyncMock(side_effect=RuntimeError("role denied")),
    )
    service = SimpleNamespace(
        initialize=AsyncMock(), remove=AsyncMock(), restore=AsyncMock()
    )
    manager = SimpleNamespace(
        sheet_id="sheet",
        secondary_sync=AsyncMock(return_value=["public panel", "organizer panel"]),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=100),
        guild=SimpleNamespace(get_role=lambda _id: role),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    monkeypatch.setattr(
        organizer_panel_module, "OrganizerService", lambda _sheet_id: service
    )
    monkeypatch.setattr(
        organizer_panel_module,
        "load_pr5_config",
        AsyncMock(return_value=({"PARTICIPANT_ROLE_ID": "99"}, [])),
    )
    monkeypatch.setattr(
        OrganizerView, "authorized", AsyncMock(return_value=True)
    )

    run(ConfirmParticipant(manager, target, restore).children[0].callback(interaction))

    if restore:
        service.restore.assert_awaited_once_with("100", "7", target)
        target.add_roles.assert_awaited_once()
    else:
        service.remove.assert_awaited_once_with("100", "7")
        target.remove_roles.assert_awaited_once()
    manager.secondary_sync.assert_awaited_once()
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert isinstance(embed, discord.Embed)
    assert embed.fields
    warning = embed.fields[0].value
    assert "Participant role" in warning
    assert "public panel" in warning
    assert "organizer panel" in warning


def test_participant_core_failure_skips_role_and_panel_sync(monkeypatch):
    role = SimpleNamespace(id=99)
    target = SimpleNamespace(
        id=7,
        roles=[role],
        remove_roles=AsyncMock(),
    )
    service = SimpleNamespace(
        initialize=AsyncMock(),
        remove=AsyncMock(side_effect=RegistrationError("participant must currently be confirmed")),
    )
    manager = SimpleNamespace(
        sheet_id="sheet", secondary_sync=AsyncMock()
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=100),
        guild=SimpleNamespace(get_role=lambda _id: role),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    monkeypatch.setattr(
        organizer_panel_module, "OrganizerService", lambda _sheet_id: service
    )
    monkeypatch.setattr(
        OrganizerView, "authorized", AsyncMock(return_value=True)
    )

    run(ConfirmParticipant(manager, target, False).children[0].callback(interaction))

    target.remove_roles.assert_not_awaited()
    manager.secondary_sync.assert_not_awaited()
    assert isinstance(
        interaction.followup.send.await_args.kwargs["embed"], discord.Embed
    )


def test_reconcile_adds_removes_continues_after_failures_and_refreshes(monkeypatch):
    role = SimpleNamespace(id=99)
    missing_ok = SimpleNamespace(id=1, add_roles=AsyncMock())
    missing_fail = SimpleNamespace(
        id=2, add_roles=AsyncMock(side_effect=RuntimeError("add denied"))
    )
    extra_ok = SimpleNamespace(id=8, remove_roles=AsyncMock())
    extra_fail = SimpleNamespace(
        id=9, remove_roles=AsyncMock(side_effect=RuntimeError("remove denied"))
    )
    participants = [
        {"tournament_id": "cup", "discord_user_id": uid, "status": "confirmed"}
        for uid in ("1", "2", "3", "4")
    ]
    manager = SimpleNamespace(
        sheet_id="sheet",
        data=AsyncMock(
            return_value=(
                {"PARTICIPANT_ROLE_ID": "99"},
                SimpleNamespace(tournament_id="cup"),
                participants,
                {},
                {
                    "missing": [missing_ok, missing_fail],
                    "extra": [extra_ok, extra_fail],
                    "unresolved": ["4"],
                },
            )
        ),
        sync=AsyncMock(return_value=PanelSyncResult(True)),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=100),
        guild=SimpleNamespace(get_role=lambda _id: role),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    monkeypatch.setattr(
        OrganizerView, "authorized", AsyncMock(return_value=True)
    )

    run(ConfirmReconcile(manager).children[0].callback(interaction))

    missing_ok.add_roles.assert_awaited_once()
    missing_fail.add_roles.assert_awaited_once()
    extra_ok.remove_roles.assert_awaited_once()
    extra_fail.remove_roles.assert_awaited_once()
    manager.sync.assert_awaited_once()
    description = interaction.followup.send.await_args.kwargs["embed"].description
    assert "Added: **1**" in description
    assert "Removed: **1**" in description
    assert "Already correct: **1**" in description
    assert "Unresolved: **1**" in description
    assert "Failures: **2**" in description


def _review_flow(*, updating, manager, service):
    return SimpleNamespace(
        updating=updating,
        manager=manager,
        service=service,
        timezone="UTC",
        selected={"a", "b", "c"},
        preparation=SimpleNamespace(
            config={"ACTIVE_TOURNAMENT_ID": "cup"},
            tournament={
                "tournament_name": "Trial Cup",
                "signup_closes_at_utc": "2026-08-09T20:00:00Z",
            },
        ),
    )


def test_player_signup_refreshes_organizer_but_availability_update_does_not(monkeypatch):
    organizer = SimpleNamespace(sync=AsyncMock())
    manager = SimpleNamespace(
        sheet_id="sheet", sync=AsyncMock(), organizer_manager=organizer
    )
    member = SimpleNamespace(
        id=7,
        display_name="Player",
        mention="<@7>",
        roles=[],
    )
    interaction = SimpleNamespace(
        user=member,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    config = {"MESSAGES_TAB": "MESSAGES", "PARTICIPANT_ROLE_ID": "99"}
    signup_template = MessageTemplate(
        "signup_confirmed",
        "You're aboard",
        "{participant} {tournament_name} {signup_deadline}",
        1,
    )
    update_template = MessageTemplate(
        "availability_updated",
        "Availability updated",
        "{participant} {tournament_name}",
        1,
    )
    monkeypatch.setattr(
        views, "load_pr3_config", AsyncMock(return_value=(config, []))
    )
    monkeypatch.setattr(
        views,
        "load_messages",
        AsyncMock(
            return_value={
                "signup_confirmed": signup_template,
                "availability_updated": update_template,
            }
        ),
    )
    monkeypatch.setattr(views, "_assign_role", AsyncMock())

    signup_service = SimpleNamespace(register=AsyncMock())
    run(
        views.ReviewView(
            _review_flow(updating=False, manager=manager, service=signup_service)
        ).children[1].callback(interaction)
    )
    signup_service.register.assert_awaited_once()
    manager.sync.assert_awaited_once()
    organizer.sync.assert_awaited_once()

    manager.sync.reset_mock()
    organizer.sync.reset_mock()
    interaction.followup.send.reset_mock()
    update_service = SimpleNamespace(update_availability=AsyncMock())
    run(
        views.ReviewView(
            _review_flow(updating=True, manager=manager, service=update_service)
        ).children[1].callback(interaction)
    )
    update_service.update_availability.assert_awaited_once()
    manager.sync.assert_not_awaited()
    organizer.sync.assert_not_awaited()


def test_closed_player_withdrawal_refreshes_organizer_without_reopening_public(monkeypatch):
    organizer = SimpleNamespace(sync=AsyncMock())
    manager = SimpleNamespace(
        sheet_id="sheet", sync=AsyncMock(), organizer_manager=organizer
    )
    service = SimpleNamespace(withdraw=AsyncMock())
    role = SimpleNamespace(id=99)
    member = SimpleNamespace(
        id=7,
        mention="<@7>",
        roles=[],
        remove_roles=AsyncMock(),
    )
    interaction = SimpleNamespace(
        user=member,
        guild=SimpleNamespace(get_role=lambda _id: role),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    snapshot = SimpleNamespace(
        config={"ACTIVE_TOURNAMENT_ID": "cup"},
        tournament={"tournament_name": "Trial Cup"},
        tournament_status="signup_closed",
    )
    template = MessageTemplate(
        "withdrawal_confirmed",
        "Withdrawal recorded",
        "{participant} has withdrawn from {tournament_name}.",
        1,
    )
    monkeypatch.setattr(
        views,
        "load_pr3_config",
        AsyncMock(
            return_value=(
                {"MESSAGES_TAB": "MESSAGES", "PARTICIPANT_ROLE_ID": "99"},
                [],
            )
        ),
    )
    monkeypatch.setattr(
        views,
        "load_messages",
        AsyncMock(return_value={"withdrawal_confirmed": template}),
    )

    modal = views.WithdrawalReasonModal(manager, service, snapshot)
    modal.reason._value = "busy"
    run(modal.on_submit(interaction))

    service.withdraw.assert_awaited_once_with("7", "busy")
    manager.sync.assert_not_awaited()
    organizer.sync.assert_awaited_once()
    assert isinstance(
        interaction.followup.send.await_args.kwargs["embed"], discord.Embed
    )
