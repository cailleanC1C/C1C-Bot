from types import SimpleNamespace

from modules.recruitment.summary_embed import build_promo_summary_embed


def _visibility_map(answers: dict[str, object]) -> dict[str, dict[str, str]]:
    return {gid: {"state": "show"} for gid in answers}


def test_promo_r_summary_formatting_and_visibility():
    answers = {
        "pr_ign": "Returning Hero",
        "pr_power": 1_250_000,
        "pr_level_detail": "Early Endgame",
        "pr_playstyle": "Competitive",
        "pr_prev_clan": "Old Guard",
        "pr_clan": "Hydra focus",
        "pr_CB": "Brutal",
        "pr_hydra_diff": "Nightmare",
        "pr_hydra_clash": 12_600,
        "pr_chimera_diff": "Normal",
        "pr_chimera_clash": 1_250_000,
        "pr_siege": "No",
        "pr_siege_detail": "Support squads",
        "pr_cvc": 5,
        "pr_cvc_points": 60_000,
        "pr_return_reason": "A" * 210,
        "pr_return_change": "Ready to rejoin",
        "pr_notes": "Eager to help",
    }
    visibility = _visibility_map(answers)
    author = SimpleNamespace(display_name="Lead", display_avatar=None, name="Lead")

    embed = build_promo_summary_embed("promo.r", answers, visibility, author=author)

    assert embed.title == "🔥 C1C • Returning player promo"
    assert "**Player:** Returning Hero" in (embed.description or "")
    assert "Power" in (embed.description or "")

    war_section = next(field for field in embed.fields if field.name.startswith("⚔️"))
    assert "Siege participation" in war_section.value
    assert "Siege setup" not in war_section.value
    assert "CvC priority:** High" in war_section.value
    assert "Minimum CvC points:** 60 K" in war_section.value

    context_section = next(field for field in embed.fields if "Return context" in field.name)
    assert "Reason for break" in context_section.value
    assert "…" in context_section.value


def test_promo_m_inline_pairs_and_siege_detail():
    answers = {
        "pm_ign": "Mover",
        "pm_power": 950,
        "pm_level_detail": "",
        "pm_playstyle": "Casual",
        "pm_current_clan": "Beta",
        "pm_clan_type": "Hydra",
        "pm_CB": "Nightmare",
        "pm_hydra_diff": "",
        "pm_hydra_clash": 320_000,
        "pm_chimera_diff": "Normal",
        "pm_chimera_clash": 0,
        "pm_siege": "Yes",
        "pm_siege_detail": "Defence anchor",
        "pm_cvc": "Medium",
        "pm_cvc_points": None,
        "pm_move_urgency": "Soon",
        "pm_move_date": "Next CvC",
        "pm_move_reason": "Rebalance clans",
        "pm_notes": "Ready to move",
    }
    visibility = _visibility_map(answers)
    embed = build_promo_summary_embed("promo.m", answers, visibility, author=None)

    performance_section = next(
        field for field in embed.fields if field.name.startswith("🧩 Performance")
    )
    assert "Avg Hydra Clash:** 320 K" in performance_section.value

    war_section = next(field for field in embed.fields if field.name.startswith("⚔️"))
    assert "Siege setup" in war_section.value
    assert "CvC priority" in war_section.value
    assert "Minimum CvC points" not in war_section.value


def test_build_promo_leadership_summary_full_sections():
    answers = {
        "pl_player_name": "Lead Recruit",
        "pl_reporter": "Coordinator",
        "pl_current_clan": "Alpha",
        "pl_target_clan": "Beta",
        "pl_power": 12_600,
        "pl_level_detail": "Endgame",
        "pl_playstyle": "Competitive",
        "pl_CB": "Ultra-Nightmare",
        "pl_hydra_diff": "Nightmare",
        "pl_hydra_clash": 12_600,
        "pl_chimera_diff": "Hard",
        "pl_chimera_clash": 1_250_000,
        "pl_siege": "Yes",
        "pl_siege_detail": "Front anchor",
        "pl_cvc": "High",
        "pl_cvc_points": 120_000,
        "pl_move_reason": "Shift leadership coverage",
        "pl_move_urgency": "This cycle",
        "pl_move_window": "After CvC",
        "pl_notes": "Ready for transfer",
    }

    embed = build_promo_summary_embed("promo.l", answers, _visibility_map(answers))

    assert embed.title == "C1C - Leadership move request"
    assert "coordinator will review the move" in (embed.description or "")

    player_section = next(field for field in embed.fields if "Move request" in field.name)
    assert "Lead Recruit" in player_section.value
    assert "Requested new clan / fit" in player_section.value

    performance_section = next(field for field in embed.fields if "Performance snapshot" in field.name)
    assert "Power & level" in performance_section.value
    assert "Hydra" in performance_section.value
    assert "Avg Chimera Clash" in performance_section.value

    war_section = next(field for field in embed.fields if "War modes" in field.name)
    assert "Siege participation" in war_section.value
    assert "CvC" in war_section.value
    assert "Avg CvC points" in war_section.value

    rationale_section = next(
        field for field in embed.fields if "Extra notes" in field.name
    )
    assert "Ready for transfer" in rationale_section.value


def test_build_promo_leadership_summary_handles_missing_optionals():
    answers = {
        "pl_player_name": "Lead Recruit",
        "pl_reporter": "Coordinator",
        "pl_current_clan": "Alpha",
        "pl_target_clan": "",
        "pl_power": None,
        "pl_level_detail": "",
        "pl_playstyle": "Competitive",
        "pl_CB": "Ultra-Nightmare",
        "pl_hydra_diff": "",
        "pl_hydra_clash": None,
        "pl_chimera_diff": "Hard",
        "pl_chimera_clash": None,
        "pl_siege": "No",
        "pl_siege_detail": "",
        "pl_cvc": "",
        "pl_cvc_points": None,
        "pl_move_reason": "",
        "pl_move_urgency": "",
        "pl_move_window": "",
        "pl_notes": None,
    }

    embed = build_promo_summary_embed("promo.l", answers, _visibility_map(answers))

    player_section = next(field for field in embed.fields if "Move request" in field.name)
    assert "Target clan" not in player_section.value

    performance_section = next(field for field in embed.fields if "Performance snapshot" in field.name)
    assert "Power" not in performance_section.value
    assert "Hydra" not in performance_section.value
    assert "Chimera" in performance_section.value

    war_section = next(field for field in embed.fields if "War modes" in field.name)
    assert "Siege participation" in war_section.value
    assert "Siege detail" not in war_section.value
    assert "CvC" not in war_section.value

    rationale_section = next(
        (field for field in embed.fields if "Extra notes" in field.name), None
    )
    assert rationale_section is None


def test_build_promo_leadership_summary_renders_collected_move_payload():
    answers = {
        "pl_private": {"value": "yes", "label": "yes"},
        "pl_discord": "<@1324831292784775309>",
        "pl_reason": "progression mismatch",
        "pl_time": "not urgent",
        "pl_curr_clan": "C1CD",
        "pl_new_clan": "relaxed",
        "pl_CB": {"value": "Easy", "label": "Easy"},
        "pl_hydra_diff": [{"value": "Normal", "label": "Normal"}],
        "pl_hydra_clash": "0",
        "pl_chimera_diff": [{"value": "Easy", "label": "Easy"}],
        "pl_chimera_clash": "0",
        "pl_siege": "no",
        "pl_cvc": "no",
        "pl_various": "I dunno what else to tell you",
    }

    embed = build_promo_summary_embed("promo.l", answers, _visibility_map(answers))

    move_section = next(field for field in embed.fields if "Move request" in field.name)
    assert "Private request:** yes" in move_section.value
    assert "Player:** <@1324831292784775309>" in move_section.value
    assert "Reason:** progression mismatch" in move_section.value
    assert "Urgency / timing:** not urgent" in move_section.value
    assert "Current clan:** C1CD" in move_section.value
    assert "Requested new clan / fit:** relaxed" in move_section.value

    performance_section = next(field for field in embed.fields if "Performance snapshot" in field.name)
    assert "Clan Boss:** Easy" in performance_section.value
    assert "Hydra:** Normal" in performance_section.value
    assert "Avg Hydra Clash:** 0" in performance_section.value
    assert "Chimera:** Easy" in performance_section.value
    assert "Avg Chimera Clash:** 0" in performance_section.value

    war_section = next(field for field in embed.fields if "War modes" in field.name)
    assert "Siege participation:** no" in war_section.value
    assert "CvC:** no" in war_section.value

    notes_section = next(field for field in embed.fields if "Extra notes" in field.name)
    assert notes_section.value == "I dunno what else to tell you"


def test_promo_m_summary_renders_exact_move_payload_context():
    answers = {
        "pm_cur_clan": "C1CD",
        "pm_lead_inform": "no",
        "pm_ign": "smurf",
        "pm_ppower": "12m",
        "pm_playstyle": "active",
        "pm_level": "mid game",
        "pm_level_detail": {"value": "Mid Game", "label": "Mid Game"},
        "pm_new_clan": "chat",
        "pm_CB": {"value": "Nightmare", "label": "Nightmare"},
        "pm_hydra_diff": [{"value": "Brutal", "label": "Brutal"}],
        "pm_hydra_clash": "1b",
        "pm_chimera_diff": [{"value": "Brutal", "label": "Brutal"}],
        "pm_chimera_clash": "1b",
        "pm_siege": "yes",
        "pm_siege_detail": "offense",
        "pm_cvc": {"value": "5", "label": "5"},
        "pm_cvc_points": "500k",
        "pm_move_date": "asap",
    }

    embed = build_promo_summary_embed("promo.m", answers, _visibility_map(answers), author=None)

    move_section = next(field for field in embed.fields if field.name == "🧭 Move request")
    assert "Player:** smurf" in move_section.value
    assert "Current clan:** C1CD" in move_section.value
    assert "Requested new clan / fit:** chat" in move_section.value
    assert "Move date:** asap" in move_section.value
    assert "Lead informed:** no" in move_section.value

    player_section = next(field for field in embed.fields if field.name == "👤 Player snapshot")
    assert "Player power:** 12 M" in player_section.value
    assert "Bracket:** Mid Game" in player_section.value
    assert "Playstyle:** active" in player_section.value
    assert "mid game" not in player_section.value

    performance_section = next(
        field for field in embed.fields if field.name == "🧩 Performance snapshot"
    )
    assert "Clan Boss (one-key top chest):** Nightmare" in performance_section.value
    assert "Hydra:** Brutal" in performance_section.value
    assert "Avg Hydra Clash:** 1b" in performance_section.value
    assert "Chimera:** Brutal" in performance_section.value
    assert "Avg Chimera Clash:** 1b" in performance_section.value

    war_section = next(field for field in embed.fields if field.name == "⚔️ War Modes")
    assert "Siege participation:** yes" in war_section.value
    assert "Siege setup:** offense" in war_section.value
    assert "CvC priority:** High" in war_section.value
    assert "Minimum CvC points:** 500 K" in war_section.value
