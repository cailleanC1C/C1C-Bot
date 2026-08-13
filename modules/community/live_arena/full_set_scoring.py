"""Use fixed-length Live Arena sets: play all 3 fights, or all 5 in the Final."""

from __future__ import annotations

from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import _text

_installed = False


def _validate_full_set_score(round_row, score_a: int, score_b: int) -> None:
    try:
        a, b = int(score_a), int(score_b)
    except (TypeError, ValueError) as exc:
        raise RegistrationError("Result scores must be whole numbers") from exc

    is_final = _text(round_row.get("round_stage")).lower() == "final"
    fights = 5 if is_final else 3
    if a < 0 or b < 0 or a + b != fights or a == b:
        if is_final:
            raise RegistrationError(
                "Final result must contain all 5 fights: 5-0, 4-1, 3-2, 2-3, 1-4, or 0-5"
            )
        raise RegistrationError(
            "BO3 result must contain all 3 fights: 3-0, 2-1, 1-2, or 0-3"
        )


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import (
        competition,
        competition_resolution,
        qualification_panel,
        result_views,
        simulation_ux_hardening,
    )

    competition._validate_played_score = _validate_full_set_score
    competition_resolution._validate_played_score = _validate_full_set_score

    original_match_embed = qualification_panel.match_embed

    def match_embed_with_full_set_wording(tournament, round_row, match, slots):
        embed = original_match_embed(tournament, round_row, match, slots)
        final = _text(round_row.get("round_stage")).lower() == "final"
        description = str(embed.description or "")
        if final:
            description = description.replace(
                "**Format:** Best of 5",
                "**Format:** Best of 5 · 5 fights · play all 5",
            )
            description = description.replace(
                "**Format:** Best of 3",
                "**Format:** Best of 5 · 5 fights · play all 5",
            )
            description = description.replace("After the BO5,", "After all 5 fights,")
            description = description.replace("After the BO3,", "After all 5 fights,")
        else:
            description = description.replace(
                "**Format:** Best of 3",
                "**Format:** Best of 3 · 3 fights · play all 3",
            )
            description = description.replace("After the BO3,", "After all 3 fights,")
        embed.description = description
        return embed

    qualification_panel.match_embed = match_embed_with_full_set_wording

    try:
        result_views.ReportResultModal.score.placeholder = "3-0 or 2-1 · Final: 5-0, 4-1, 3-2"
    except Exception:
        pass
    try:
        simulation_ux_hardening.HardenedReportResultModal.score.placeholder = (
            "3-0 or 2-1 · Final: 5-0, 4-1, 3-2"
        )
    except Exception:
        pass
