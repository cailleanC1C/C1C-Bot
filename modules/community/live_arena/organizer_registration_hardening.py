"""Harden organizer registration controls and keep the registration panel phase-aware."""

from __future__ import annotations

import logging

import discord

from shared.theme import colors

from modules.community.live_arena.organizer_panel import ConfirmTransition
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.organizer_registration_hardening")
_installed = False

_REGISTRATION_OPEN_ACTIONS = {
    "Close Registration",
    "View Roster",
    "Reconcile Roles",
    "Repair Discord State",
    "Player History",
}
_REGISTRATION_DRAFT_ACTIONS = {
    "Open Registration",
    "View Roster",
    "Reconcile Roles",
    "Repair Discord State",
    "Player History",
}
_REGISTRATION_CLOSED_BASE = {
    "Reopen Registration",
    "View Roster",
    "Reconcile Roles",
    "Repair Discord State",
    "Player History",
}
_Q1_BY_STATE = {
    "": {"Generate Q1 Draw"},
    "proposed": {"Approve Draw", "Regenerate Draw", "Swap Players"},
    "active": {
        "Close Current Round",
        "Review Result Issues",
        "Competition Ops",
        "View Standings",
    },
    "completed": {
        "View Standings",
        "Preview Next Swiss",
        "Regenerate Swiss Preview",
        "Approve & Publish Swiss",
        "Repair Swiss Conflict",
    },
}


def _response_done(interaction) -> bool:
    checker = getattr(interaction.response, "is_done", None)
    return bool(checker()) if callable(checker) else False


async def _defer_now(interaction) -> None:
    """Acknowledge the Discord interaction before any Sheet/config work."""
    if _response_done(interaction):
        return
    defer = getattr(interaction.response, "defer", None)
    if callable(defer):
        await defer(ephemeral=True)


class FastCloseConfirmView(ConfirmTransition):
    """Confirmation whose callback acknowledges before organizer authorization."""

    def __init__(self, manager):
        discord.ui.View.__init__(self, timeout=300)
        self.manager = manager
        self.action = "close"

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, _button):
        from modules.community.live_arena.organizer_panel import OrganizerView, execute_transition

        await _defer_now(interaction)
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await execute_transition(interaction, self.manager, "close")


def _prune_registration_controls(view, manager, status):
    """Hide controls belonging to tournament phases the organizer cannot use yet."""
    if status == "draft":
        allowed = _REGISTRATION_DRAFT_ACTIONS
    elif status == "signup_open":
        allowed = _REGISTRATION_OPEN_ACTIONS
    elif status == "signup_closed":
        qstatus = str(getattr(manager, "_qualification_q1_status", "") or "").lower()
        allowed = _REGISTRATION_CLOSED_BASE | _Q1_BY_STATE.get(qstatus, set())
    else:
        return view

    for item in list(view.children):
        label = str(getattr(item, "label", "") or "")
        if label and label not in allowed:
            view.remove_item(item)
    return view


async def _send_close_prompt(interaction, manager) -> None:
    """Handle Close Registration from the button callback itself.

    Existing persistent OrganizerButton instances capture their original bound
    transition handler when they are created. Intercepting at OrganizerButton.callback
    therefore guarantees the live persistent button uses the hardened path even when
    the view instance was constructed before this install hook ran.
    """
    from modules.community.live_arena import organizer_panel

    if manager is None:
        raise RuntimeError("Live Arena organizer manager is unavailable")

    await _defer_now(interaction)
    if not await organizer_panel.OrganizerView(manager).authorized(interaction):
        return
    try:
        _, _tournament, _, counts, _ = await manager.data(interaction.guild)
        confirmed = int(counts.get("confirmed", 0) or 0)
        warning = ""
        if confirmed % 2:
            warning = (
                " The confirmed roster is odd; no player will be auto-demoted. "
                "Qualification Round 1 will randomly assign one bye before pairing "
                "the remaining players."
            )
        embed = discord.Embed(
            title="Confirm close registration",
            description=f"Close registration with **{confirmed}** confirmed players?{warning}",
            color=colors.c1c_blue,
        )
        await organizer_panel._send_ephemeral(
            interaction,
            embed=embed,
            view=FastCloseConfirmView(manager),
        )
    except Exception as exc:
        log.exception("Live Arena close-registration preflight failed after acknowledgement")
        await organizer_panel._send_ephemeral(interaction, embed=error_embed(exc))


def _button_manager(button):
    owner = getattr(getattr(button, "handler", None), "__self__", None)
    return getattr(owner, "manager", None)


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import organizer_panel

    # Make all defer helpers idempotent. Some handlers already defer internally;
    # callback-level acknowledgement must never trigger InteractionResponded.
    organizer_panel._defer_ephemeral = _defer_now

    # Patch the button callback itself rather than OrganizerView.transition. Live
    # persistent buttons keep the bound handler captured at construction time, so
    # replacing the transition method alone does not affect an already-built view.
    original_button_callback = organizer_panel.OrganizerButton.callback

    async def hardened_button_callback(self, interaction):
        if getattr(self, "action", None) == "close":
            await _send_close_prompt(interaction, _button_manager(self))
            return
        await original_button_callback(self, interaction)

    organizer_panel.OrganizerButton.callback = hardened_button_callback

    # Prune the final, fully-decorated view at the manager sync boundary. This runs
    # after all later Live Arena modules have added their controls, so the actual
    # Discord message receives the lifecycle-appropriate subset rather than the
    # complete control wall.
    original_sync = organizer_panel.OrganizerPanelManager.sync

    async def hardened_sync(self):
        original_view = self.view

        def pruned_view(status=None):
            return _prune_registration_controls(original_view(status), self, status)

        self.view = pruned_view
        try:
            return await original_sync(self)
        finally:
            self.view = original_view

    organizer_panel.OrganizerPanelManager.sync = hardened_sync
