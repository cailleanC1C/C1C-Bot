from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from modules.community.live_arena import (
    qualification,
    result_lifecycle_fallback,
    result_lifecycle_ux,
    simulation_ux_finalizer,
)


def run(awaitable):
    return asyncio.run(awaitable)


def _prime_lifecycle_copy(sheet_id: str) -> None:
    values = {
        "result_confirmed_player": (
            "Result confirmed",
            "{participant_mention} confirmed **{score}**. The result is final immediately.",
        ),
        "result_confirmed_staff": (
            "Result confirmed by organizer",
            "{staff_mention} confirmed **{score}** on behalf of {participant_mention}.",
        ),
        "result_finalized_expired": (
            "Result finalized",
            "**{score}** became final when the objection window expired.",
        ),
        "result_finalized_organizer": (
            "Result resolved",
            "Organizer review finalized the result at **{score}**.",
        ),
        "result_disputed_player": (
            "Result disputed",
            "{participant_mention} disputed **{score}**. This matchup is frozen pending organizer review.",
        ),
        "button_confirm_result": ("Confirm Result", ""),
        "button_dispute_result": ("Dispute Result", ""),
    }
    result_lifecycle_ux._COPY[sheet_id] = {
        key: result_lifecycle_ux.CopyTemplate(key, title, description, 0x1A73E8)
        for key, (title, description) in values.items()
    }


def test_finalized_pre_feature_match_reconciles_without_placeholder_title_render(monkeypatch):
    sheet_id = "sheet-startup-repair"
    _prime_lifecycle_copy(sheet_id)
    old_message = SimpleNamespace(
        embeds=[discord.Embed(title="Result disputed")],
        edit=AsyncMock(),
    )
    monkeypatch.setattr(
        result_lifecycle_ux,
        "_find_lifecycle_message",
        AsyncMock(return_value=old_message),
    )
    match = {
        "match_id": "T-Q2-M01",
        "status": "finalized",
        "player_a_discord_user_id": "1",
        "player_b_discord_user_id": "2",
        "reported_by_discord_user_id": "1",
        "reported_score_a": "0",
        "reported_score_b": "3",
        "final_score_a": "0",
        "final_score_b": "3",
        "confirmed_by_discord_user_id": "",
        "finalized_by_discord_user_id": "900",
    }

    run(
        result_lifecycle_fallback._reconcile_lifecycle_message_migration_safe(
            result_lifecycle_ux,
            SimpleNamespace(user=SimpleNamespace(id=999)),
            sheet_id,
            SimpleNamespace(),
            match,
        )
    )

    old_message.edit.assert_awaited_once()
    kwargs = old_message.edit.await_args.kwargs
    assert kwargs["view"] is None
    assert kwargs["embed"].title == "Result resolved"
    assert "3-0" in kwargs["embed"].description


def test_finalized_match_without_old_lifecycle_message_is_valid_history(monkeypatch):
    sheet_id = "sheet-startup-no-message"
    _prime_lifecycle_copy(sheet_id)
    monkeypatch.setattr(
        result_lifecycle_ux,
        "_find_lifecycle_message",
        AsyncMock(return_value=None),
    )

    run(
        result_lifecycle_fallback._reconcile_lifecycle_message_migration_safe(
            result_lifecycle_ux,
            SimpleNamespace(user=SimpleNamespace(id=999)),
            sheet_id,
            SimpleNamespace(),
            {"match_id": "T-Q1-M01", "status": "finalized"},
        )
    )


def test_captains_table_actions_use_tournament_snapshot_id_not_repository_config(monkeypatch):
    monkeypatch.setattr(
        simulation_ux_finalizer,
        "load_tournament_snapshot",
        AsyncMock(
            return_value=SimpleNamespace(
                tournament_id="T1",
                status="active",
            )
        ),
    )

    class FakeRepository:
        config = {}  # Reproduces the production KeyError path.

        async def rounds(self):
            return [
                {
                    "tournament_id": "T1",
                    "round_id": "T1-Q2",
                    "round_number": "2",
                    "round_stage": "qualification",
                    "status": "open",
                }
            ]

    class FakeQualificationService:
        def __init__(self, _sheet_id):
            self.repository = FakeRepository()

        async def initialize(self):
            return None

    monkeypatch.setattr(qualification, "QualificationService", FakeQualificationService)

    actions = run(
        result_lifecycle_fallback._allowed_panel_actions_snapshot_safe(
            SimpleNamespace(sheet_id="sheet"),
            simulation_ux_finalizer,
        )
    )

    assert "Close Current Round" in actions
    assert "Review Result Issues" in actions
    assert "View Standings" in actions
