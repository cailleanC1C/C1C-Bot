"""Migration-safe fallback for MatchResultView construction before lifecycle copy loads.

Production registration preloads the new result lifecycle copy, so the starter
Dispute button is removed and the reported-result message owns the action. Tests
and pre-registration construction may not have that copy yet; preserve the
legacy functional control in that narrow fallback state.
"""

from __future__ import annotations

import logging

import discord

from modules.community.live_arena.competition_resolution import CompetitionResolutionService
from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import _text
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.result_lifecycle_fallback")
_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import result_lifecycle_ux, result_views

    original_init = result_views.MatchResultView.__init__

    def init_with_preload_fallback(self, sheet_id: str, **kwargs):
        original_init(self, sheet_id, **kwargs)
        if result_lifecycle_ux._templates(str(sheet_id)) is not None:
            return
        if any(
            getattr(item, "custom_id", "") == "live_arena:match:dispute_result"
            for item in self.children
        ):
            return

        button = discord.ui.Button(
            label="Dispute Result",
            style=discord.ButtonStyle.danger,
            custom_id="live_arena:match:dispute_result",
            disabled=bool(kwargs.get("dispute_disabled", False)),
        )

        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            try:
                service = CompetitionResolutionService(str(sheet_id))
                await service.initialize()
                match = await service.match_for_thread(str(interaction.channel_id))
                updated = await service.dispute_result(
                    str(interaction.user.id), _text(match["match_id"])
                )
                result_views.cancel_match_finalizer(
                    str(sheet_id), _text(updated["match_id"])
                )
                await result_views._run_post_mutation_sync(str(sheet_id))
            except RegistrationError as exc:
                await interaction.followup.send(embed=error_embed(exc), ephemeral=True)
            except Exception as exc:
                log.exception("Live Arena legacy dispute fallback failed")
                await interaction.followup.send(embed=error_embed(exc), ephemeral=True)

        button.callback = callback
        self.add_item(button)

    result_views.MatchResultView.__init__ = init_with_preload_fallback
