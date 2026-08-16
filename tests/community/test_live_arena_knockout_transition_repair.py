from __future__ import annotations

from types import SimpleNamespace

import pytest

from modules.community.live_arena import captains_table_control_center as control
from modules.community.live_arena import knockout_transition_repair as repair


def _row(stage: str, status: str, *, round_id: str, number: str = "3"):
    return {
        "tournament_id": "LA-TEST",
        "round_id": round_id,
        "round_name": round_id,
        "round_stage": stage,
        "round_number": number,
        "status": status,
    }


def _state(*, knockout_status: str | None = "preview", seeded: bool = True):
    rounds = [
        _row("qualification", "closed", round_id="LA-TEST-Q3"),
        _row("qualification_tiebreak", "resolved", round_id="LA-TEST-TB"),
    ]
    if seeded:
        rounds.append(_row("top8_seeding", "frozen", round_id="LA-TEST-TOP8"))
    if knockout_status is not None:
        rounds.append(
            _row(
                "quarterfinal",
                knockout_status,
                round_id="LA-TEST-QF",
                number="4",
            )
        )
    return control.ControlState(
        tournament_id="LA-TEST",
        rounds=rounds,
        matches=[],
        standings=[],
        tie_groups=[],
        tiebreak_matches=[],
        tiebreak_resolved=True,
    )


def test_frozen_top8_with_qf_preview_replaces_stale_lock_and_finish_actions():
    state = _state(knockout_status="preview", seeded=True)
    manager = SimpleNamespace(
        _captains_table_allowed={
            "Close Current Round",
            "Freeze Top 8",
            "Record BO3 Tiebreak",
            "View Standings",
            "Repair Discord State",
        }
    )

    repair._apply_progression_state(manager, state)

    assert "Approve & Open Knockout" in manager._captains_table_allowed
    assert "Close Current Round" not in manager._captains_table_allowed
    assert "Freeze Top 8" not in manager._captains_table_allowed
    assert "Record BO3 Tiebreak" not in manager._captains_table_allowed
    assert "View Standings" in manager._captains_table_allowed


def test_qf_ready_to_close_exposes_finish_round_not_approval():
    state = _state(knockout_status="ready_to_close", seeded=True)
    manager = SimpleNamespace(
        _captains_table_allowed={"Approve & Open Knockout", "View Standings"}
    )

    repair._apply_progression_state(manager, state)

    assert "Close Current Round" in manager._captains_table_allowed
    assert "Approve & Open Knockout" not in manager._captains_table_allowed
    assert "Freeze Top 8" not in manager._captains_table_allowed


def test_qf_open_has_no_premature_finish_round():
    state = _state(knockout_status="active", seeded=True)
    manager = SimpleNamespace(
        _captains_table_allowed={
            "Close Current Round",
            "Approve & Open Knockout",
            "View Standings",
        }
    )

    repair._apply_progression_state(manager, state)

    assert "Close Current Round" not in manager._captains_table_allowed
    assert "Approve & Open Knockout" not in manager._captains_table_allowed


def test_unresolved_qualification_tiebreak_still_exposes_tiebreak_action():
    state = control.ControlState(
        tournament_id="LA-TEST",
        rounds=[_row("qualification", "closed", round_id="LA-TEST-Q3")],
        matches=[],
        standings=[],
        tie_groups=[["1", "2"]],
        tiebreak_matches=[
            {
                "match_id": "LA-TEST-TB-M01",
                "status": "published",
                "player_a_discord_user_id": "1",
                "player_b_discord_user_id": "2",
            }
        ],
        tiebreak_resolved=False,
    )
    manager = SimpleNamespace(
        _captains_table_allowed={"Close Current Round", "Freeze Top 8"}
    )

    repair._apply_progression_state(manager, state)

    assert "Record BO3 Tiebreak" in manager._captains_table_allowed
    assert "Close Current Round" not in manager._captains_table_allowed
    assert "Freeze Top 8" not in manager._captains_table_allowed


def test_preview_stage_summary_tells_organizer_to_review_and_start():
    state = _state(knockout_status="preview", seeded=True)

    summary = repair._stage_summary_with_preview(
        lambda _state: ("wrong", "wrong", "wrong"), state
    )

    assert summary == (
        "Quarterfinals",
        "Review the quarterfinals matchups and start the round.",
        "Semifinals",
    )


@pytest.mark.asyncio
async def test_closed_round_refreshes_ledger_and_alert_without_thread_work(monkeypatch):
    from modules.community.live_arena import victory_ledger_final_refresh as ledger

    calls = []

    async def overview(bot, service, snapshot):
        calls.append(("overview", snapshot.status))
        return True

    async def alert(bot, sheet_id, round_row, matches):
        calls.append(("alert", sheet_id, round_row["status"], len(matches)))

    monkeypatch.setattr(ledger, "_force_overview_refresh", overview)
    monkeypatch.setattr(ledger, "_sync_round_ready_alert", alert)

    snapshot = SimpleNamespace(
        status="closed",
        round_row={
            "round_id": "LA-TEST-Q3",
            "round_stage": "qualification",
            "status": "closed",
        },
        matches=({"match_id": "M1", "status": "finalized"},),
    )
    service = SimpleNamespace(sheet_id="sheet")

    warnings = await repair._sync_closed_round(SimpleNamespace(), service, snapshot)

    assert warnings == []
    assert calls == [
        ("overview", "closed"),
        ("alert", "sheet", "closed", 1),
    ]
