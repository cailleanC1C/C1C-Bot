"""Harden the Create Next Tournament modal handoff.

The tournament-details view is ephemeral and is only created after the invoking user
has already passed the organizer-role check.  Re-authorizing on the next button click
adds an unnecessary Sheet-backed round trip before Discord's interaction response.

Discord can also acknowledge/display a modal before a client/library-side exception is
raised while finishing the response handoff.  Treat that state as successful instead
of letting discord.ui.view emit an opaque unhandled exception with no useful context.
"""

from __future__ import annotations

import logging

import discord

log = logging.getLogger("c1c.community.live_arena.next_tournament_modal_boundary")
_installed = False


def _response_done(interaction) -> bool:
    response = getattr(interaction, "response", None)
    checker = getattr(response, "is_done", None)
    return bool(checker()) if callable(checker) else False


class SafeNextTournamentStartView(discord.ui.View):
    """Ephemeral tournament-details launcher with an observable modal boundary."""

    def __init__(self, manager):
        super().__init__(timeout=900)
        self.manager = manager

    @discord.ui.button(
        label="Enter Tournament Details",
        style=discord.ButtonStyle.primary,
    )
    async def start(self, interaction, _button):
        from modules.community.live_arena import next_tournament
        from modules.community.live_arena.organizer_panel import _send_ephemeral

        user_id = getattr(getattr(interaction, "user", None), "id", "unknown")
        sheet_id = getattr(self.manager, "sheet_id", "unknown")

        try:
            await interaction.response.send_modal(
                next_tournament.NextTournamentBasicsModal(self.manager)
            )
        except Exception as exc:
            # If Discord already accepted the interaction, the modal is live.  Do not
            # turn a successful handoff into the generic discord.ui.view ERROR seen in
            # production.  Keep the anomaly observable with useful context instead.
            if _response_done(interaction):
                log.warning(
                    "Live Arena tournament-details modal acknowledged but response handoff raised • user=%s • sheet=%s • error=%s: %s",
                    user_id,
                    sheet_id,
                    type(exc).__name__,
                    exc,
                    exc_info=True,
                )
                return

            log.exception(
                "Live Arena tournament-details modal launch failed • user=%s • sheet=%s • error=%s: %s",
                user_id,
                sheet_id,
                type(exc).__name__,
                exc,
            )
            try:
                await _send_ephemeral(
                    interaction,
                    embed=next_tournament.error_embed(exc),
                )
            except Exception as send_exc:
                log.exception(
                    "Live Arena tournament-details modal error response failed • user=%s • sheet=%s • error=%s: %s",
                    user_id,
                    sheet_id,
                    type(send_exc).__name__,
                    send_exc,
                )
            return

        log.info(
            "Live Arena tournament-details modal opened • user=%s • sheet=%s",
            user_id,
            sheet_id,
        )

    async def on_error(self, interaction, error, item) -> None:
        """Last-resort diagnostics for future child callback failures."""
        log.exception(
            "Live Arena tournament-details view callback failed • user=%s • sheet=%s • item=%s • error=%s: %s",
            getattr(getattr(interaction, "user", None), "id", "unknown"),
            getattr(self.manager, "sheet_id", "unknown"),
            getattr(item, "custom_id", None) or getattr(item, "label", "unknown"),
            type(error).__name__,
            error,
            exc_info=(type(error), error, error.__traceback__),
        )


def install() -> None:
    """Replace only the ephemeral details-launch view used by the wizard."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import next_tournament

    next_tournament.NextTournamentStartView = SafeNextTournamentStartView
