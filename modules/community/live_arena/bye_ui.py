"""Discord presentation helpers for qualification byes."""

from __future__ import annotations

from modules.community.live_arena import runtime_hooks, swiss_panel
from modules.community.live_arena.bye_support import _is_bye
from modules.community.live_arena.qualification import QualificationSnapshot
from modules.community.live_arena.service import _text

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    original_overview = runtime_hooks._competition_overview_embed
    original_preview = swiss_panel.preview_embed

    def overview_with_byes(tournament, round_row, matches, standings):
        byes = [row for row in matches if _is_bye(row)]
        normal = [row for row in matches if not _is_bye(row)]
        embed = original_overview(tournament, round_row, normal, standings)
        for row in byes:
            embed.add_field(
                name="Qualification bye",
                value=(
                    f"<@{_text(row.get('player_a_discord_user_id'))}> receives the bye. "
                    "Final: **bye** · +1 match win · +2 game differential."
                ),
                inline=False,
            )
        return embed

    def preview_with_byes(snapshot, *, official=False):
        byes = [row for row in snapshot.matches if _is_bye(row)]
        filtered = QualificationSnapshot(
            snapshot.round_row,
            tuple(row for row in snapshot.matches if not _is_bye(row)),
        )
        embed = original_preview(filtered, official=official)
        for row in byes:
            embed.add_field(
                name="Qualification bye",
                value=(
                    f"**{_text(row.get('player_a_display_name'))}** receives the bye. "
                    "No Duelling Deck is created for a bye."
                ),
                inline=False,
            )
        return embed

    runtime_hooks._competition_overview_embed = overview_with_byes
    swiss_panel.preview_embed = preview_with_byes
