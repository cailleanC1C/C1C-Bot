from modules.community.live_arena.hall_of_fame import build_history


def _tournament(tid, name, status="completed", completed="2026-08-01T00:00:00Z"):
    return {
        "tournament_id": tid,
        "tournament_name": name,
        "status": status,
        "completed_at_utc": completed,
    }


def _match(tid, suffix, a, b, winner, *, status="finalized", number="1"):
    return {
        "tournament_id": tid,
        "round_id": f"{tid}-{suffix}",
        "match_number": number,
        "player_a_discord_user_id": a,
        "player_a_display_name": f"Player {a}",
        "player_b_discord_user_id": b,
        "player_b_display_name": f"Player {b}",
        "status": status,
        "final_winner_discord_user_id": winner,
    }


def test_history_uses_only_completed_or_archived_tournaments():
    tournaments = [
        _tournament("T1", "Cup One"),
        _tournament("T2", "Cup Two", status="active", completed=""),
    ]
    matches = [
        _match("T1", "F", "1", "2", "1"),
        _match("T2", "F", "3", "4", "3"),
    ]
    results, histories = build_history(tournaments, [], [], matches)
    assert [item.tournament_id for item in results] == ["T1"]
    assert set(histories) == {"1", "2"}


def test_history_derives_knockout_finishes_and_match_record():
    tid = "T1"
    tournaments = [_tournament(tid, "Cup One")]
    matches = [
        _match(tid, "Q1", "1", "8", "1"),
        _match(tid, "QF", "1", "4", "1"),
        _match(tid, "QF", "2", "3", "2", number="2"),
        _match(tid, "SF", "1", "2", "1"),
        _match(tid, "F", "1", "5", "1"),
    ]
    results, histories = build_history(tournaments, [], [], matches)
    assert results[0].champion_id == "1"
    assert results[0].runner_up_id == "5"

    champion = histories["1"]
    assert champion.appearances == 1
    assert champion.tournament_wins == 1
    assert champion.runner_up_finishes == 0
    assert champion.top8_finishes == 1
    assert champion.semifinal_appearances == 1
    assert champion.final_appearances == 1
    assert champion.match_wins == 4
    assert champion.match_losses == 0
    assert "Champion" in champion.tournament_lines[0]

    runner = histories["5"]
    assert runner.runner_up_finishes == 1
    assert runner.final_appearances == 1
    assert runner.match_wins == 0
    assert runner.match_losses == 1


def test_byes_and_double_forfeits_do_not_change_match_record():
    tid = "T1"
    tournaments = [_tournament(tid, "Cup One")]
    matches = [
        _match(tid, "Q1", "1", "", "1", status="bye"),
        _match(tid, "Q2", "1", "2", "", status="double_forfeit"),
        _match(tid, "F", "3", "4", "3"),
    ]
    _, histories = build_history(tournaments, [], [], matches)
    assert histories["1"].match_wins == 0
    assert histories["1"].match_losses == 0
    assert histories["2"].match_wins == 0
    assert histories["2"].match_losses == 0


def test_forfeit_counts_as_match_win_and_loss():
    tid = "T1"
    tournaments = [_tournament(tid, "Cup One")]
    matches = [
        _match(tid, "Q1", "1", "2", "1", status="forfeit"),
        _match(tid, "F", "3", "4", "3"),
    ]
    _, histories = build_history(tournaments, [], [], matches)
    assert histories["1"].match_wins == 1
    assert histories["2"].match_losses == 1


def test_cross_tournament_stats_accumulate_without_all_time_ranking():
    tournaments = [
        _tournament("T1", "Cup One", completed="2026-07-01T00:00:00Z"),
        _tournament("T2", "Cup Two", status="archived", completed="2026-08-01T00:00:00Z"),
    ]
    matches = [
        _match("T1", "F", "1", "2", "1"),
        _match("T2", "F", "2", "1", "2"),
    ]
    results, histories = build_history(tournaments, [], [], matches)
    assert [item.tournament_id for item in results] == ["T1", "T2"]
    assert histories["1"].appearances == 2
    assert histories["1"].tournament_wins == 1
    assert histories["1"].runner_up_finishes == 1
    assert histories["1"].match_wins == 1
    assert histories["1"].match_losses == 1
