"""Organizer-only constrained manual repair control for invalid Swiss previews."""

from __future__ import annotations

import logging

import discord

from shared.theme import colors

from modules.community.live_arena.organizer_panel import OrganizerView
from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import _text
from modules.community.live_arena.swiss import SwissQualificationService
from modules.community.live_arena.swiss_manual import parse_manual_pairs, repair_preview_pairings
from modules.community.live_arena.swiss_panel import _target_round, preview_embed
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.swiss_manual_panel")
_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import qualification_panel

    original_install = qualification_panel.install_qualification

    def install_with_manual_swiss(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_swiss_manual_installed", False):
            return True
        manager._swiss_manual_installed = True
        base_view = manager.view

        def view(status=None):
            result = base_view(status)
            add_item = getattr(result, "add_item", None)
            if callable(add_item):
                add_item(
                    RepairSwissConflictButton(
                        manager,
                        disabled=status is not None and status != "active",
                    )
                )
            return result

        manager.view = view
        return True

    qualification_panel.install_qualification = install_with_manual_swiss


class RepairSwissConflictButton(discord.ui.Button):
    def __init__(self, manager, *, disabled=False):
        super().__init__(
            label="Repair Swiss Conflict",
            custom_id="live_arena:organizer:swiss:manual_repair",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self.manager = manager

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        try:
            service = SwissQualificationService(self.manager.sheet_id)
            await service.initialize()
            target = await _target_round(service)
            snapshot = await service.snapshot(target)
            if snapshot.round_row is None or _text(snapshot.round_row.get("status")) != "preview":
                raise RegistrationError("There is no Swiss preview available for manual repair")
            await interaction.response.send_modal(
                SwissManualRepairModal(self.manager, target)
            )
        except Exception as exc:
            log.exception("Live Arena Swiss manual repair preflight failed")
            if interaction.response.is_done():
                await interaction.followup.send(embed=error_embed(exc), ephemeral=True)
            else:
                await interaction.response.send_message(embed=error_embed(exc), ephemeral=True)


class SwissManualRepairModal(discord.ui.Modal, title="Repair Swiss Conflict"):
    pairings = discord.ui.TextInput(
        label="Replacement pairings",
        placeholder="123456-789012, 345678-901234",
        style=discord.TextStyle.paragraph,
        min_length=3,
        max_length=1500,
    )

    def __init__(self, manager, round_number: int):
        super().__init__(timeout=600)
        self.manager = manager
        self.round_number = round_number

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            service = SwissQualificationService(self.manager.sheet_id)
            await service.initialize()
            pairs = parse_manual_pairs(str(self.pairings.value))
            snapshot = await repair_preview_pairings(
                service,
                str(interaction.user.id),
                self.round_number,
                pairs,
            )
            try:
                await self.manager.sync()
            except Exception:
                log.exception("Live Arena organizer sync after Swiss manual repair failed")
            embed = preview_embed(snapshot, official=False)
            embed.title = f"Qualification Round {self.round_number} · Repaired Organizer Preview"
            embed.description = (
                "Only the hard-rule-conflicted subset was changed. The full round passed "
                "the no-rematch, adjacent-record, unique-player, and roster validation checks."
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            log.exception("Live Arena Swiss manual repair failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)
