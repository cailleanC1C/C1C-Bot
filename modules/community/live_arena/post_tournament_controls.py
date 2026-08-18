"""Final persistence/acknowledgement boundary for post-tournament organizer controls.

The Captain's Table survives longer than a process lifetime.  Its rendered components
therefore need a persistent callback registry that is independent of whichever
25-component organizer view happened to be registered during startup.

This module is intentionally installed last.  It also acknowledges the Create New
Tournament interaction before any Sheet/config work and gives archived tournaments
an archived-specific lifecycle summary at the final Captain's Table render boundary.
"""

from __future__ import annotations

import contextvars
import logging

import discord

from modules.community.live_arena.service import _text, load_tournament_snapshot

log = logging.getLogger("c1c.community.live_arena.post_tournament_controls")
_installed = False

_RENDER_TOURNAMENT_STATUS: contextvars.ContextVar[str] = contextvars.ContextVar(
    "live_arena_captains_table_tournament_status", default=""
)


def _response_done(interaction) -> bool:
    response = getattr(interaction, "response", None)
    checker = getattr(response, "is_done", None)
    return bool(checker()) if callable(checker) else False


async def _acknowledge(interaction) -> None:
    """Acknowledge before organizer authorization or any Sheet/config read."""
    if _response_done(interaction):
        return
    response = getattr(interaction, "response", None)
    defer = getattr(response, "defer", None)
    if not callable(defer):
        raise RuntimeError("Discord interaction response cannot be deferred")
    await defer(ephemeral=True)


class CreateNextTournamentPersistentView(discord.ui.View):
    """Callback-only registry for the long-lived Captain's Table button."""

    def __init__(self, manager):
        super().__init__(timeout=None)
        from modules.community.live_arena.next_tournament import CreateNextTournamentButton

        self.add_item(CreateNextTournamentButton(manager))


def _register_create_next_persistent_view(bot, manager) -> None:
    """Register/replace the callback on every Live Arena ready pass.

    discord.py keys persistent dispatch by component type/custom_id.  Re-registering
    this one-control view replaces any stale callback captured by an older organizer
    view without affecting the other Captain's Table controls.
    """

    add_view = getattr(bot, "add_view", None)
    if not callable(add_view):
        raise RuntimeError("Discord bot cannot register persistent views")
    add_view(CreateNextTournamentPersistentView(manager))
    log.info(
        "Live Arena Create New Tournament persistent callback registered • sheet=%s",
        getattr(manager, "sheet_id", "unknown"),
    )


async def _create_next_callback(self, interaction) -> None:
    """Persistent Create New Tournament callback with immediate acknowledgement."""

    from modules.community.live_arena import next_tournament
    from modules.community.live_arena.organizer_panel import OrganizerView, _send_ephemeral
    from modules.community.live_arena.views import error_embed

    user_id = getattr(getattr(interaction, "user", None), "id", "unknown")
    log.info(
        "Live Arena Create New Tournament clicked • user=%s • sheet=%s",
        user_id,
        getattr(self.manager, "sheet_id", "unknown"),
    )

    try:
        await _acknowledge(interaction)
    except Exception as exc:
        log.exception(
            "Live Arena Create New Tournament acknowledgement failed • user=%s • error=%s: %s",
            user_id,
            type(exc).__name__,
            exc,
        )
        return

    log.info("Live Arena Create New Tournament acknowledged • user=%s", user_id)

    try:
        if not await OrganizerView(self.manager).authorized(interaction):
            log.info(
                "Live Arena Create New Tournament rejected • user=%s • reason=not_authorized",
                user_id,
            )
            return

        messages = await next_tournament._load_next_messages(
            self.manager.sheet_id, {"next_tournament_intro"}
        )
        await _send_ephemeral(
            interaction,
            embed=messages["next_tournament_intro"].embed(),
            view=next_tournament.NextTournamentStartView(self.manager),
        )
        log.info("Live Arena Create New Tournament wizard opened • user=%s", user_id)
    except Exception as exc:
        log.exception(
            "Live Arena Create New Tournament start failed • user=%s • error=%s: %s",
            user_id,
            type(exc).__name__,
            exc,
        )
        try:
            await _send_ephemeral(interaction, embed=error_embed(exc))
        except Exception as send_exc:
            log.exception(
                "Live Arena Create New Tournament error response failed • user=%s • error=%s: %s",
                user_id,
                type(send_exc).__name__,
                send_exc,
            )


def _archived_stage_summary() -> tuple[str, str, str]:
    return (
        "Tournament archived",
        "Nothing. This tournament is complete and archived.",
        "Create a new tournament",
    )


def install() -> None:
    """Install the final post-tournament interaction/render boundary."""

    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import captains_table_control_center as control
    from modules.community.live_arena import next_tournament, panel

    # The rendered button already has a stable custom_id, but the callback previously
    # did Sheet-backed authorization before acknowledging the interaction.  Replace
    # the callback at the class boundary so both message-specific and persistent
    # dispatch use the same fast-ack path.
    next_tournament.CreateNextTournamentButton.callback = _create_next_callback

    # Register the Create New Tournament callback independently from the large
    # Captain's Table view.  The organizer surface can overflow/prune controls by
    # phase, while this one callback must always survive a process restart.
    original_register_live_arena = panel.register_live_arena

    async def register_live_arena_with_post_tournament_callback(bot):
        manager = await original_register_live_arena(bot)
        if manager is None:
            return None
        organizer = getattr(manager, "organizer_manager", None)
        if organizer is None:
            log.warning(
                "Live Arena Create New Tournament persistent callback skipped • reason=organizer_manager_missing"
            )
            return manager
        try:
            _register_create_next_persistent_view(bot, organizer)
        except Exception as exc:
            # Keep Live Arena available even if this non-destructive callback repair
            # fails; the explicit log makes the failure observable.
            log.exception(
                "Live Arena Create New Tournament persistent callback registration failed • error=%s: %s",
                type(exc).__name__,
                exc,
            )
        return manager

    panel.register_live_arena = register_live_arena_with_post_tournament_callback

    # Captain's Table stage copy is calculated deep inside the final renderer.  Carry
    # the authoritative tournament status through a ContextVar so the existing field
    # builder can render archived-specific copy without a second Discord edit.
    original_stage_summary = control._stage_summary

    def stage_summary_with_archived_state(state):
        if _RENDER_TOURNAMENT_STATUS.get() == "archived":
            return _archived_stage_summary()
        return original_stage_summary(state)

    control._stage_summary = stage_summary_with_archived_state

    original_render_control_center = control._render_control_center

    async def render_control_center_with_tournament_status(manager, state):
        status = ""
        try:
            tournament = await load_tournament_snapshot(manager.sheet_id)
            status = _text(getattr(tournament, "status", "")).lower()
        except Exception as exc:
            log.exception(
                "Live Arena Captain's Table tournament-status preflight failed • tournament=%s • error=%s: %s",
                getattr(state, "tournament_id", ""),
                type(exc).__name__,
                exc,
            )
        token = _RENDER_TOURNAMENT_STATUS.set(status)
        try:
            return await original_render_control_center(manager, state)
        finally:
            _RENDER_TOURNAMENT_STATUS.reset(token)

    control._render_control_center = render_control_center_with_tournament_status
