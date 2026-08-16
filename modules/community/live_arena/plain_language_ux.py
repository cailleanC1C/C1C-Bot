"""Plain-language Live Arena UI for players and normal organizers.

Internal tournament mechanics may keep their technical names in code and logs, but
Discord should tell people what to do without requiring tournament vocabulary.
"""

from __future__ import annotations

import discord

from shared.theme import colors

from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import _text

_installed = False


def _qualification_preview(snapshot, *, official: bool) -> discord.Embed:
    number = _text(snapshot.round_row.get("round_number")) if snapshot.round_row else "?"
    embed = discord.Embed(
        title=(
            f"Qualification Round {number} · "
            f"{'Official Matchups' if official else 'Organizer Preview'}"
        ),
        description=(
            "This preview is **not official** and creates no player-facing Discord resources."
            if not official
            else "These qualification matchups have been approved."
        ),
        color=colors.c1c_blue,
    )
    for match in snapshot.matches:
        scheduling = (
            "⚠️ No shared availability found"
            if _text(match.get("has_scheduling_conflict")).lower() == "true"
            else "Shared availability found"
        )
        embed.add_field(
            name=f"Match {_text(match['match_number'])}",
            value=(
                f"**{_text(match['player_a_display_name'])}** vs "
                f"**{_text(match['player_b_display_name'])}**\n"
                "Pairing based on the current qualification standings.\n"
                f"{scheduling}"
            )[:1024],
            inline=False,
        )
    return embed


def _plain_score_validation(round_row, score_a: int, score_b: int) -> None:
    try:
        a, b = int(score_a), int(score_b)
    except (TypeError, ValueError) as exc:
        raise RegistrationError("Result scores must be whole numbers") from exc
    is_final = _text(round_row.get("round_stage")).lower() == "final"
    wins_required = 3 if is_final else 2
    if max(a, b) != wins_required or min(a, b) < 0 or a == b or min(a, b) >= wins_required:
        if is_final:
            raise RegistrationError("The Final result must be 3-0, 3-1, or 3-2")
        raise RegistrationError("The result must be 2-0 or 2-1")


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import competition, result_views, swiss_panel
    from modules.community.live_arena.captains_table_control_center_repair import (
        install as install_control_center_repair,
    )
    from modules.community.live_arena.captains_table_action_state import (
        install as install_control_action_state,
    )

    install_control_center_repair()
    install_control_action_state()
    swiss_panel.preview_embed = _qualification_preview

    original_swiss_button_init = swiss_panel.SwissActionButton.__init__

    def swiss_button_init(self, manager, label: str, action: str, *, disabled=False):
        original_swiss_button_init(self, manager, label, action, disabled=disabled)
        self.label = {
            "preview": "Preview Next Round",
            "regenerate": "Refresh Round Preview",
            "publish": "Approve & Publish Round",
        }.get(action, self.label)

    swiss_panel.SwissActionButton.__init__ = swiss_button_init

    competition._validate_played_score = _plain_score_validation

    original_modal_init = result_views.ReportResultModal.__init__

    def report_modal_init(self, sheet_id: str, match_id: str):
        original_modal_init(self, sheet_id, match_id)
        for child in getattr(self, "children", ()):
            if isinstance(child, discord.ui.TextInput) and _text(getattr(child, "label", "")) == "Final series score":
                child.placeholder = "Example: 2-1. In the Final, one player must reach 3 wins."

    result_views.ReportResultModal.__init__ = report_modal_init
