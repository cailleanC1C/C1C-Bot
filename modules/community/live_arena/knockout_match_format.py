"""Render the Live Arena Final matchup as BO5 without changing qualification cards."""

from __future__ import annotations

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import qualification_panel

    original = qualification_panel.match_embed

    def match_embed_with_final_format(tournament, round_row, match, slots):
        embed = original(tournament, round_row, match, slots)
        stage = str(round_row.get("round_stage", "") or "").strip().lower()
        if stage != "final":
            return embed
        description = str(embed.description or "")
        description = description.replace("**Format:** Best of 3", "**Format:** Best of 5")
        description = description.replace("After the BO3,", "After the BO5,")
        embed.description = description
        return embed

    qualification_panel.match_embed = match_embed_with_final_format
