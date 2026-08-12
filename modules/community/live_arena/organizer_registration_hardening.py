"""Harden organizer registration controls and keep the registration panel phase-aware."""

from __future__ import annotations

import logging

import discord

from shared.theme import colors

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
    await interaction.response.defer(ephemeral=True)


class FastCloseConfirmView(discord.ui.View):
    """Confirmation whose callback acknowledges before organizer authorization."""

    def __init__(self, manager):
        super().__init__(timeout=300)
        self.manager = manager

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


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import organizer_panel, qualification_panel

    original_transition = organizer_panel.OrganizerView.transition

    async def hardened_transition(self, interaction, action):
        if action != "close":
            await original_transition(self, interaction, action)
            return

        # Priority bug fix: Discord must be acknowledged before authorization,
        # config reads, roster counts, or any other Sheet/network I/O.
        await _defer_now(interaction)
        if not await self.authorized(interaction):
            return
        try:
            _, _tournament, _, counts, _ = await self.manager.data(interaction.guild)
            confirmed = int(counts.get("confirmed", 0) or 0)
            warning = ""
            if confirmed % 2:
                warning = (
                    " The confirmed roster is odd, so Qualification Round 1 will "
                    "randomly assign one bye before pairing the remaining players."
                )
            embed = discord.Embed(
                title="Confirm close registration",
                description=(
                    f"Close registration with **{confirmed}** confirmed players?{warning}"
                ),
                color=colors.c1c_blue,
            )
            await interaction.followup.send(
                embed=embed,
                view=FastCloseConfirmView(self.manager),
                ephemeral=True,
            )
        except Exception as exc:
            log.exception(
                "Live Arena close-registration preflight failed after acknowledgement"
            )
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)

    organizer_panel.OrganizerView.transition = hardened_transition

    # Install last around the accumulated organizer-panel decorators so the
    # registration phases do not display every future-stage control at once.
    original_install = qualification_panel.install_qualification

    def install_with_registration_pruning(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_registration_control_pruning_installed", False):
            return True
        manager._registration_control_pruning_installed = True
        base_view = manager.view

        def view(status=None):
            return _prune_registration_controls(base_view(status), manager, status)

        manager.view = view
        return True

    qualification_panel.install_qualification = install_with_registration_pruning
