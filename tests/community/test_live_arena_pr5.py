"""Focused PR5 regression tests for organizer production repair behavior."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord
import pytest

from modules.community.live_arena.messages import (
    MESSAGE_HEADERS,
    PR5_CONFIG_KEYS,
    PR5_MESSAGES,
    REQUIRED_MESSAGES,
    MessageTemplate,
    load_messages,
)
from modules.community.live_arena.organizer import OrganizerService
from modules.community.live_arena.organizer_panel import (
    ConfirmReconcile,
    OrganizerPanelManager,
    OrganizerView,
    RosterActions,
    role_parity,
    roster_embed,
)
from modules.community.live_arena.panel import PanelSyncResult
from modules.community.live_arena.repository import (
    AUDIT_LOG_HEADERS,
    PARTICIPANT_AVAILABILITY_HEADERS,
    PARTICIPANT_HEADERS,
    LiveArenaRepository,
)
from modules.community.live_arena.service import TOURNAMENT_HEADERS


def run(awaitable):
    return asyncio.run(awaitable)


def test_secondary_sync_reports_handled_panel_failures():
    public = SimpleNamespace(
        sync=AsyncMock(return_value=PanelSyncResult(False, "edit"))
    )
    manager = object.__new__(OrganizerPanelManager)
    manager.public_manager = public
    manager.sync = AsyncMock(return_value=PanelSyncResult(False, "fetch"))

    assert run(manager.secondary_sync()) == ["public panel", "organizer panel"]


def test_secondary_sync_preserves_success_and_exception_reporting():
    public = SimpleNamespace(sync=AsyncMock(return_value=PanelSyncResult(True)))
    manager = object.__new__(OrganizerPanelManager)
    manager.public_manager = public
    manager.sync = AsyncMock(side_effect=RuntimeError("persist failed"))

    assert run(manager.secondary_sync()) == ["organizer panel"]


def test_open_cells_use_one_targeted_values_batch():
    spreadsheet = SimpleNamespace(values_batch_update=AsyncMock())
    worksheet = SimpleNamespace(spreadsheet=spreadsheet)
    repository = LiveArenaRepository("sheet")
    repository.config = {"TOURNAMENTS_TAB": "TOURNAMENTS"}
    row = 7

    with (
        patch(
            "modules.community.live_arena.repository.aget_worksheet",
            AsyncMock(return_value=worksheet),
        ),
        patch(
            "modules.community.live_arena.repository.acall_with_backoff",
            AsyncMock(),
        ) as call,
    ):
        run(
            repository.update_tournament_cells(
                row,
                {
                    "status": "signup_open",
                    "signup_opens_at_utc": "2026-08-07T12:00:00Z",
                },
            )
        )

    assert call.await_count == 1
    body = call.await_args.kwargs["body"]
    status_col = chr(65 + TOURNAMENT_HEADERS.index("status"))
    opens_col = chr(65 + TOURNAMENT_HEADERS.index("signup_opens_at_utc"))
    assert body == {
        "valueInputOption": "RAW",
        "data": [
            {"range": f"'TOURNAMENTS'!{status_col}{row}", "values": [["signup_open"]]},
            {
                "range": f"'TOURNAMENTS'!{opens_col}{row}",
                "values": [["2026-08-07T12:00:00Z"]],
            },
        ],
    }


def test_roster_actions_include_remove_restore_and_refresh():
    labels = [
        getattr(item, "label", None) or getattr(item, "placeholder", None)
        for item in RosterActions(object()).children
    ]
    assert labels == ["Remove Participant", "Restore Participant", "Refresh"]


def test_reconcile_missing_participant_role_reports_failure_without_crashing():
    manager = SimpleNamespace(
        sheet_id="sheet",
        data=AsyncMock(
            return_value=(
                {"PARTICIPANT_ROLE_ID": "999"},
                SimpleNamespace(tournament_id="cup"),
                [
                    {
                        "tournament_id": "cup",
                        "discord_user_id": "10",
                        "status": "confirmed",
                    }
                ],
                {},
                {"missing": [SimpleNamespace(id=10)], "extra": [], "unresolved": []},
            )
        ),
        sync=AsyncMock(return_value=PanelSyncResult(True)),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1, roles=[]),
        guild=SimpleNamespace(get_role=lambda _role_id: None),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    view = ConfirmReconcile(manager)

    with patch.object(OrganizerView, "authorized", AsyncMock(return_value=True)):
        run(view.children[0].callback(interaction))

    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert "Added: **0**" in embed.description
    assert "Failures: **1**" in embed.description
    manager.sync.assert_awaited_once()


def test_reconcile_failed_add_does_not_inflate_already_correct():
    role = SimpleNamespace(id=999)
    already = SimpleNamespace(id=10)
    missing = SimpleNamespace(
        id=11, add_roles=AsyncMock(side_effect=RuntimeError("no"))
    )
    manager = SimpleNamespace(
        sheet_id="sheet",
        data=AsyncMock(
            return_value=(
                {"PARTICIPANT_ROLE_ID": "999"},
                SimpleNamespace(tournament_id="cup"),
                [
                    {
                        "tournament_id": "cup",
                        "discord_user_id": str(member.id),
                        "status": "confirmed",
                    }
                    for member in (already, missing)
                ],
                {},
                {"missing": [missing], "extra": [], "unresolved": []},
            )
        ),
        sync=AsyncMock(return_value=PanelSyncResult(True)),
    )
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=1, roles=[]),
        guild=SimpleNamespace(get_role=lambda _role_id: role),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )

    with patch.object(OrganizerView, "authorized", AsyncMock(return_value=True)):
        run(ConfirmReconcile(manager).children[0].callback(interaction))

    description = interaction.followup.send.await_args.kwargs["embed"].description
    assert "Added: **0**" in description
    assert "Already correct: **1**" in description
    assert "Failures: **1**" in description


def test_pr5_message_contracts_are_additive_and_schema_is_frozen():
    assert PR5_CONFIG_KEYS == (
        "MESSAGES_TAB",
        "SIGNUP_CHANNEL_ID",
        "ORGANIZER_CHANNEL_ID",
        "ORGANIZER_ROLE_ID",
        "PARTICIPANT_ROLE_ID",
        "PUBLIC_PANEL_MESSAGE_ID",
        "ORGANIZER_PANEL_MESSAGE_ID",
    )
    assert PR5_MESSAGES["signup_closed"] == {
        "tournament_name",
        "confirmed_count",
    }
    assert PR5_MESSAGES["organizer_panel"] == {
        "tournament_name",
        "status",
        "confirmed_count",
        "max_participants",
    }
    assert "parity_summary" not in PR5_MESSAGES["organizer_panel"]
    assert {
        "organizer_roster_open",
        "organizer_roster_ready",
        "organizer_roster_odd",
        "organizer_statuses",
        "organizer_roles_ok",
        "organizer_roles_missing",
        "organizer_roster_view",
        "organizer_roster_participants",
        "organizer_roster_participant_line",
    }.issubset(PR5_MESSAGES)
    assert "signup_closed" not in REQUIRED_MESSAGES
    assert "organizer_panel" not in REQUIRED_MESSAGES
    assert PARTICIPANT_HEADERS[-4:] == (
        "withdrawn_at_utc",
        "withdrawal_reason",
        "updated_at_utc",
        "notes",
    )
    assert PARTICIPANT_AVAILABILITY_HEADERS == (
        "tournament_id",
        "discord_user_id",
        "slot_id",
        "created_at_utc",
        "updated_at_utc",
        "notes",
    )
    assert AUDIT_LOG_HEADERS == (
        "event_id",
        "tournament_id",
        "event_type",
        "actor_discord_user_id",
        "target_discord_user_id",
        "details",
        "created_at_utc",
    )


def test_organizer_message_loading_isolated_from_player_rows():
    matrix = [
        list(MESSAGE_HEADERS),
        [
            "organizer_panel",
            "Panel {tournament_name}",
            "{status} {confirmed_count}/{max_participants}",
            "#123456",
            "TRUE",
            "",
        ],
    ]
    with patch(
        "modules.community.live_arena.messages.afetch_values",
        AsyncMock(return_value=matrix),
    ):
        templates = run(load_messages("sheet", "MESSAGES", {"organizer_panel"}))
    assert set(templates) == {"organizer_panel"}
    assert isinstance(
        templates["organizer_panel"].embed(
            tournament_name="Cup",
            status="draft",
            confirmed_count=0,
            max_participants=32,
        ),
        discord.Embed,
    )


@pytest.mark.parametrize(
    "status,enabled",
    [
        ("draft", [True, False, False]),
        ("signup_open", [False, True, False]),
        ("signup_closed", [False, False, True]),
    ],
)
def test_transition_button_state_contract(status, enabled):
    buttons = OrganizerView(SimpleNamespace(), status).children[:3]
    assert [not item.disabled for item in buttons] == enabled


def test_authorization_denial_is_embed_and_does_not_invoke_handler():
    manager = SimpleNamespace(sheet_id="sheet")
    interaction = SimpleNamespace(
        user=SimpleNamespace(roles=[SimpleNamespace(id=4)]),
        response=SimpleNamespace(send_message=AsyncMock()),
    )
    with patch(
        "modules.community.live_arena.organizer_panel.load_pr5_config",
        AsyncMock(return_value=({"ORGANIZER_ROLE_ID": "5"}, [])),
    ):
        assert run(OrganizerView(manager).authorized(interaction)) is False
    kwargs = interaction.response.send_message.await_args.kwargs
    assert isinstance(kwargs["embed"], discord.Embed)
    assert kwargs["ephemeral"] is True


def test_role_parity_reports_missing_extra_and_unresolved_exactly():
    role = SimpleNamespace(members=[SimpleNamespace(id=1), SimpleNamespace(id=9)])
    members = {1: role.members[0], 2: SimpleNamespace(id=2)}
    guild = SimpleNamespace(
        get_role=lambda _id: role,
        get_member=lambda member_id: members.get(member_id),
    )
    participants = [
        {"tournament_id": "cup", "discord_user_id": uid, "status": "confirmed"}
        for uid in ("1", "2", "3")
    ]
    parity = run(role_parity(guild, {"PARTICIPANT_ROLE_ID": "8"}, participants, "cup"))
    assert [m.id for m in parity["missing"]] == [2]
    assert [m.id for m in parity["extra"]] == [9]
    assert parity["unresolved"] == ["3"]


def test_roster_embed_uses_sheet_copy_and_user_select_controls():
    manager = SimpleNamespace(
        sheet_id="sheet",
        data=AsyncMock(
            return_value=(
                {"MESSAGES_TAB": "MESSAGES"},
                SimpleNamespace(
                    tournament_id="cup",
                    tournament_name="Cup",
                    status="signup_open",
                    min_participants=2,
                    max_participants=16,
                ),
                [
                    {
                        "tournament_id": "cup",
                        "display_name_at_signup": "Ada",
                        "discord_user_id": "1",
                        "clan_tag_at_signup": "C1C",
                        "status": "confirmed",
                        "timezone": "Europe/London",
                    }
                ],
                {"confirmed": 1, "withdrawn": 0, "removed": 0, "disqualified": 0},
                {
                    "missing": [SimpleNamespace(display_name="Ada", id=1)],
                    "extra": [],
                    "unresolved": [],
                    "role_missing": False,
                },
            )
        ),
    )
    templates = {
        "organizer_roster_view": MessageTemplate(
            "organizer_roster_view",
            "SHEET ROSTER",
            "{tournament_name} {status} {confirmed_count}/{max_participants}",
            0x5F6368,
        ),
        "organizer_roster_open": MessageTemplate(
            "organizer_roster_open",
            "SHEET READINESS",
            "{confirmed_count} {player_word} minimum {min_participants}",
            0x5F6368,
        ),
        "organizer_statuses": MessageTemplate(
            "organizer_statuses",
            "SHEET STATUSES",
            "{confirmed_count}/{withdrawn_count}/{removed_count}/{disqualified_count}",
            0x5F6368,
        ),
        "organizer_roles_missing": MessageTemplate(
            "organizer_roles_missing",
            "SHEET ROLE ISSUE",
            "Missing {missing_participants}",
            0x5F6368,
        ),
        "organizer_roster_participants": MessageTemplate(
            "organizer_roster_participants",
            "SHEET PLAYERS",
            "{participant_lines}",
            0x5F6368,
        ),
        "organizer_roster_participant_line": MessageTemplate(
            "organizer_roster_participant_line",
            "",
            "{participant_name}|{clan_tag}|{participant_status}|{timezone}",
            0x5F6368,
        ),
    }
    with patch(
        "modules.community.live_arena.organizer_panel.load_messages",
        AsyncMock(return_value=templates),
    ):
        embed = run(roster_embed(manager, object()))

    assert isinstance(embed, discord.Embed)
    assert embed.title == "SHEET ROSTER"
    assert "Registration open" in embed.description
    fields = {field.name: field.value for field in embed.fields}
    assert fields["SHEET READINESS"] == "1 player minimum 2"
    assert fields["SHEET STATUSES"] == "1/0/0/0"
    assert fields["SHEET ROLE ISSUE"] == "Missing Ada"
    assert fields["SHEET PLAYERS"] == "Ada|C1C|confirmed|Europe/London"
    visible = "\n".join([embed.description or ""] + list(fields.values()))
    assert "parity" not in visible.lower()
    assert "EVEN" not in visible and "ODD" not in visible
    assert (
        sum(
            isinstance(item, discord.ui.UserSelect)
            for item in RosterActions(manager).children
        )
        == 2
    )


@pytest.mark.parametrize("deadline", ["", "not-a-date", "2026-08-06T00:00:00Z"])
def test_open_rejects_blank_invalid_or_nonfuture_deadline_without_mutation(deadline):
    repo = SimpleNamespace(
        initialize=AsyncMock(),
        participants=AsyncMock(return_value=[]),
        update_tournament_cells=AsyncMock(),
        append_audit=AsyncMock(),
    )
    service = OrganizerService(
        "sheet", repository=repo, clock=lambda: datetime(2026, 8, 7, tzinfo=UTC)
    )
    service.context = AsyncMock(
        return_value=(
            {"ACTIVE_TOURNAMENT_ID": "cup"},
            (2, {"status": "draft", "signup_closes_at_utc": deadline}),
            [],
            [],
        )
    )
    with patch(
        "modules.community.live_arena.organizer.load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": "cup"}),
    ):
        with pytest.raises(Exception, match="valid future"):
            run(service.transition("open", "7"))
    repo.update_tournament_cells.assert_not_awaited()
    repo.append_audit.assert_not_awaited()


@pytest.mark.parametrize(
    "action,old,new,event",
    [
        ("open", "draft", "signup_open", "registration_opened"),
        ("close", "signup_open", "signup_closed", "registration_closed"),
        ("reopen", "signup_closed", "signup_open", "registration_reopened"),
    ],
)
def test_transitions_use_exact_targeted_mutations_and_audits(action, old, new, event):
    repo = SimpleNamespace(
        participants=AsyncMock(return_value=[]),
        update_tournament_cells=AsyncMock(),
        append_audit=AsyncMock(),
    )
    now = datetime(2026, 8, 7, tzinfo=UTC)
    service = OrganizerService("sheet", repository=repo, clock=lambda: now)
    service.context = AsyncMock(
        return_value=(
            {"ACTIVE_TOURNAMENT_ID": "cup"},
            (9, {"status": old, "signup_closes_at_utc": "2026-08-08T00:00:00Z"}),
            [],
            [],
        )
    )
    with patch(
        "modules.community.live_arena.organizer.load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": "cup"}),
    ):
        run(service.transition(action, "7"))
    expected = {"status": new}
    if action == "open":
        expected["signup_opens_at_utc"] = "2026-08-07T00:00:00Z"
    repo.update_tournament_cells.assert_awaited_once_with(9, expected)
    assert repo.append_audit.await_args.args[0]["event_type"] == event
