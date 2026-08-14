import discord

from modules.community.live_arena import round_close_milestone_ux as ux


def test_float_direction_is_corrected_for_weaker_player_moving_up():
    rationale = (
        "Swiss adjacent-group float · Atlantic5penguin (0-1) "
        "floated down to 0-0 · no-rematch constraint preserved"
    )
    assert ux.normalize_float_rationale(rationale) == (
        "Swiss adjacent-group float · Atlantic5penguin (0-1) "
        "floated up to 0-0 · no-rematch constraint preserved"
    )


def test_float_direction_keeps_stronger_player_moving_down():
    rationale = (
        "Swiss adjacent-group float · JB0223 (1-0) "
        "floated down to 0-0 · no-rematch constraint preserved"
    )
    assert ux.normalize_float_rationale(rationale) == rationale


def test_closed_qualification_overview_becomes_final_outcome(monkeypatch):
    from modules.community.live_arena import qualification_panel

    monkeypatch.setattr(
        qualification_panel,
        "_format_timestamp",
        lambda value, style="F": "FINALIZED_AT",
    )
    embed = discord.Embed(
        title="Qualification Round 1",
        description="old active/closed summary",
    )
    round_row = {
        "status": "closed",
        "round_stage": "qualification",
        "round_name": "Qualification Round 1",
        "completed_at_utc": "2026-08-14T20:02:00Z",
    }
    matches = [
        {"status": "double_forfeit"},
        {"status": "finalized"},
        {"status": "finalized"},
        {"status": "finalized"},
    ]

    result = ux._closed_round_embed(
        embed,
        {"tournament_name": "C1C Live Arena Trial Cup"},
        round_row,
        matches,
    )

    assert result.title == "Qualification Round 1 · Final Outcome"
    assert "**Status:** Final outcome" in result.description
    assert "**Finalized:** FINALIZED_AT" in result.description
    assert "Completed: **4 / 4**" in result.description
    assert "Round deadline" not in result.description


def test_non_closed_round_overview_is_unchanged():
    embed = discord.Embed(title="Qualification Round 2", description="Round is open")
    result = ux._closed_round_embed(
        embed,
        {"tournament_name": "Cup"},
        {"status": "open", "round_stage": "qualification"},
        [],
    )
    assert result is embed
    assert result.title == "Qualification Round 2"
    assert result.description == "Round is open"
