from modules.community.live_arena import qualification_panel


def test_final_match_card_is_bo5_while_quarterfinal_remains_bo3():
    tournament = {"tournament_name": "Trial Cup"}
    match = {
        "match_number": "1",
        "player_a_discord_user_id": "1",
        "player_b_discord_user_id": "2",
        "shared_slot_ids_csv": "",
    }
    base_round = {
        "round_name": "Quarterfinals",
        "round_stage": "quarterfinal",
        "opens_at_utc": "2026-08-20T12:00:00Z",
        "deadline_at_utc": "2026-08-26T12:00:00Z",
    }

    qf = qualification_panel.match_embed(tournament, base_round, match, [])
    assert "**Format:** Best of 3" in qf.description
    assert "After the BO3" in qf.description

    final_round = dict(base_round, round_name="Final", round_stage="final")
    final = qualification_panel.match_embed(tournament, final_round, match, [])
    assert "**Format:** Best of 5" in final.description
    assert "After the BO5" in final.description
    assert "Best of 3" not in final.description
