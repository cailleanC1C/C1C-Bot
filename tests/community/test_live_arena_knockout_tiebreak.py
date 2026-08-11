from modules.community.live_arena.competition import StandingEntry
from modules.community.live_arena.knockout_tiebreak import _affected_groups, _apply_resolutions


def entry(uid: str, rank: int, *, tied: bool):
    return StandingEntry(
        discord_user_id=uid,
        display_name=f"P{uid}",
        match_wins=2,
        match_losses=1,
        game_wins=4,
        game_losses=2,
        game_differential=2,
        strength_of_opponents=5,
        rank=rank,
        tied=tied,
    )


def test_recorded_tiebreak_only_reorders_exact_tied_group():
    standings = [
        entry("1", 1, tied=False),
        entry("2", 2, tied=False),
        entry("3", 3, tied=True),
        entry("4", 3, tied=True),
        entry("5", 5, tied=False),
    ]
    affected = [standings[2], standings[3]]
    resolutions = [{"group": ["3", "4"], "order": ["4", "3"]}]

    ordered = _apply_resolutions(standings, affected, resolutions)

    assert [row.discord_user_id for row in ordered] == ["1", "2", "4", "3", "5"]


def test_missing_or_partial_tiebreak_resolution_does_not_guess():
    standings = [entry("3", 3, tied=True), entry("4", 3, tied=True)]
    affected = list(standings)

    assert _apply_resolutions(standings, affected, []) is None
    assert _apply_resolutions(
        standings,
        affected,
        [{"group": ["3", "4"], "order": ["3"]}],
    ) is None


def test_three_player_tie_stays_one_organizer_resolved_group():
    affected = [entry("7", 7, tied=True), entry("8", 7, tied=True), entry("9", 7, tied=True)]

    assert _affected_groups(affected) == [["7", "8", "9"]]
    ordered = _apply_resolutions(
        affected,
        affected,
        [{"group": ["7", "8", "9"], "order": ["9", "7", "8"]}],
    )
    assert [row.discord_user_id for row in ordered] == ["9", "7", "8"]
