from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import discord

from modules.community.live_arena import captains_table_control_center as control
from modules.community.live_arena import final_tournament_close_boundary as boundary
from modules.community.live_arena import full_set_scoring


TID = "LA-TEST"


class _Item:
    def __init__(self, label: str):
        self.label = label
        self.row = None


class _View:
    def __init__(self, *labels: str):
        self.children = [_Item(label) for label in labels]

    def remove_item(self, item) -> None:
        self.children.remove(item)


def _state(*extra_rounds):
    q3 = {
        "tournament_id": TID,
        "round_id": f"{TID}-Q3",
        "round_stage": "qualification",
        "round_number": "3",
        "status": "closed",
    }
    seed = {
        "tournament_id": TID,
        "round_id": f"{TID}-TOP8",
        "round_stage": "top8_seeding",
        "round_number": "3",
        "status": "frozen",
    }
    return SimpleNamespace(
        tournament_id=TID,
        rounds=[q3, seed, *extra_rounds],
        matches=[],
        standings=[],
        tie_groups=[],
        tiebreak_matches=[],
        tiebreak_resolved=False,
        unsupported_tie=False,
        tiebreak_required=False,
        tiebreak_complete=False,
    )


def _round(stage: str, number: int, status: str):
    suffix = {"quarterfinal": "QF", "semifinal": "SF", "final": "F"}[stage]
    return {
        "tournament_id": TID,
        "round_id": f"{TID}-{suffix}",
        "round_stage": stage,
        "round_number": str(number),
        "status": status,
    }


def _manager(*actions: str):
    return SimpleNamespace(_captains_table_allowed=set(actions))


def _friendly_labels(actions: set[str]) -> set[str]:
    view = _View(
        "Close Current Round",
        "Complete Tournament",
        "Approve & Open Knockout",
        "Review Result Issues",
        "Repair Discord State",
    )
    full_set_scoring._finalize_visible_view(view, set(actions))
    return {item.label for item in view.children}


def test_final_ready_to_close_keeps_finish_round_visible():
    state = _state(_round("final", 6, "ready_to_close"))
    manager = _manager(
        "Close Current Round",
        "Complete Tournament",
        "Review Result Issues",
        "Repair Discord State",
    )

    actions = boundary._authoritative_actions(manager, state, "active")
    labels = _friendly_labels(actions)

    assert "Close Current Round" in actions
    assert "Complete Tournament" not in actions
    assert "Finish Round" in labels
    assert "Finish Tournament" not in labels


def test_final_closed_exposes_finish_tournament_and_removes_finish_round():
    state = _state(_round("final", 6, "closed"))
    manager = _manager(
        "Close Current Round",
        "Review Result Issues",
        "Repair Discord State",
    )

    actions = boundary._authoritative_actions(manager, state, "active")
    labels = _friendly_labels(actions)

    assert "Close Current Round" not in actions
    assert "Complete Tournament" in actions
    assert "Finish Round" not in labels
    assert "Finish Tournament" in labels


def test_qf_close_to_semifinal_preview_cannot_dead_end():
    state = _state(
        _round("quarterfinal", 4, "closed"),
        _round("semifinal", 5, "preview"),
    )
    manager = _manager(
        "Close Current Round",
        "Review Result Issues",
        "Repair Discord State",
    )

    actions = boundary._authoritative_actions(manager, state, "active")

    assert "Close Current Round" not in actions
    assert "Approve & Open Knockout" in actions


def test_sf_close_to_final_preview_cannot_dead_end():
    state = _state(
        _round("quarterfinal", 4, "closed"),
        _round("semifinal", 5, "closed"),
        _round("final", 6, "preview"),
    )
    manager = _manager(
        "Close Current Round",
        "Review Result Issues",
        "Repair Discord State",
    )

    actions = boundary._authoritative_actions(manager, state, "active")

    assert "Close Current Round" not in actions
    assert "Approve & Open Knockout" in actions


def test_in_progress_final_does_not_expose_finish_round_early():
    state = _state(_round("final", 6, "open"))
    manager = _manager(
        "Close Current Round",
        "Review Result Issues",
        "Repair Discord State",
    )

    actions = boundary._authoritative_actions(manager, state, "active")

    assert "Close Current Round" not in actions
    assert "Complete Tournament" not in actions


def test_captains_table_timestamp_uses_sheet_owned_label():
    class Template:
        def render(self):
            return "Last updated", ""

    embed = discord.Embed(title="Tournament Control")
    moment = datetime(2026, 8, 18, 0, 25, tzinfo=UTC)

    boundary._stamp_control_center(
        embed,
        {"organizer_control_last_updated": Template()},
        now=moment,
    )

    assert embed.footer.text == "Last updated"
    assert embed.timestamp == moment


def test_knockout_control_center_title_is_explicitly_top8(monkeypatch):
    class Template:
        def render(self, **values):
            return "Top 8 standings", values["standings_lines"]

    state = _state(_round("quarterfinal", 4, "closed"))
    state.standings = [SimpleNamespace(discord_user_id="1")]
    embed = discord.Embed(title="Tournament Control")
    embed.add_field(name="Current qualification order", value="old", inline=False)
    monkeypatch.setattr(control, "_standings_lines", lambda _state: "#1 player")

    boundary._replace_knockout_standings_title(
        embed,
        state,
        {"organizer_control_knockout_standings": Template()},
    )

    assert embed.fields[-1].name == "Top 8 standings"
    assert embed.fields[-1].value == "#1 player"
