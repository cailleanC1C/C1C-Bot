from __future__ import annotations

from types import SimpleNamespace

from modules.community.live_arena.swiss_provisional import (
    _players_from_roster_with_provisional_zeroes,
)


def standing(uid, wins, losses, gd, sos, rank):
    return SimpleNamespace(
        discord_user_id=str(uid),
        match_wins=wins,
        match_losses=losses,
        game_differential=gd,
        strength_of_opponents=sos,
        rank=rank,
    )


def test_unfinished_q1_player_remains_pairable_as_provisional_zero_zero():
    roster = [
        {"discord_user_id": "1", "display_name_at_signup": "P1"},
        {"discord_user_id": "2", "display_name_at_signup": "P2"},
        {"discord_user_id": "3", "display_name_at_signup": "P3"},
        {"discord_user_id": "4", "display_name_at_signup": "P4"},
    ]
    standings = [
        standing("1", 1, 0, 2, 0, 1),
        standing("2", 0, 1, -2, 1, 3),
        standing("3", 0, 1, -1, 1, 2),
    ]

    players = _players_from_roster_with_provisional_zeroes(roster, standings)
    by_id = {player.user_id: player for player in players}

    assert set(by_id) == {"1", "2", "3", "4"}
    assert by_id["4"].record_label == "0-0"
    # Provisional 0-0/GD 0 ranks above finalized 0-1 players with negative GD.
    assert by_id["4"].ranking_index < by_id["3"].ranking_index
    assert by_id["4"].ranking_index < by_id["2"].ranking_index
