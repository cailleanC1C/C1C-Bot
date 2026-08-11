"""Explicit organizer repair action for Live Arena Discord state."""

from __future__ import annotations

import logging

import discord

from shared.theme import colors

from modules.community.live_arena.organizer_panel import OrganizerView
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.competition_repair")
_installed = False


def install() -> None:
    """Stack a repair control onto the Live Arena organizer view."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import qualification_panel

    original_install = qualification_panel.install_qualification

    def install_qualification_with_repair(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_competition_repair_installed", False):
            return True
        manager._competition_repair_installed = True
        base_view = manager.view

        def view(status=None):
            result = base_view(status)
            add_item = getattr(result, "add_item", None)
            if callable(add_item):
                add_item(
                    RepairCompetitionDiscordButton(
                        manager,
                        disabled=status is not None and status not in {
                            "active",
                            "completed",
                        },
                    )
                )
            return result

        manager.view = view
        return True

    qualification_panel.install_qualification = install_qualification_with_repair


class RepairCompetitionDiscordButton(discord.ui.Button):
    def __init__(self, manager, *, disabled=False):
        super().__init__(
            label="Repair Discord State",
            custom_id="live_arena:organizer:competition:repair_discord",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self.manager = manager

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            sync = getattr(self.manager, "_competition_sync", None)
            if not callable(sync):
                raise RuntimeError("Competition Discord repair is not installed")
            warnings = list(await sync())
            try:
                await self.manager.sync()
            except Exception:
                log.exception("Live Arena organizer panel repair failed")
                warnings.append("organizer panel")
            if warnings:
                embed = discord.Embed(
                    title="Discord repair incomplete",
                    description=(
                        "Sheet tournament state was left unchanged. These Discord items "
                        "still need attention:\n"
                        + "\n".join(f"• {item}" for item in dict.fromkeys(warnings))
                    )[:4096],
                    color=colors.c1c_blue,
                )
            else:
                embed = discord.Embed(
                    title="Discord state repaired",
                    description=(
                        "The current tournament's Discord presentation was re-synced "
                        "from Sheet truth. No competition state was rolled back."
                    ),
                    color=colors.c1c_blue,
                )
        except Exception as exc:
            log.exception("Live Arena explicit Discord repair failed")
            embed = error_embed(exc)
        await interaction.followup.send(embed=embed, ephemeral=True)
