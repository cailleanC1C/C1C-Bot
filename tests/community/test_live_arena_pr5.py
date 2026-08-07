"""Focused PR5 regression tests for organizer production repair behavior."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from modules.community.live_arena.organizer_panel import (
    ConfirmReconcile,
    OrganizerPanelManager,
    OrganizerView,
    RosterActions,
)
from modules.community.live_arena.panel import PanelSyncResult
from modules.community.live_arena.repository import LiveArenaRepository
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
