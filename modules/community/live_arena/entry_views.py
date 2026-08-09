"""Persistent player entry controls for Live Arena registration."""

from __future__ import annotations

import logging

import discord

from shared.theme import colors

from modules.community.live_arena.registration import RegistrationService
from modules.community.live_arena.views import (
    RegistrationActionsView,
    TimezoneSelectView,
    _player_error,
    error_embed,
    registration_embed,
    timezone_prompt_embed,
)

log = logging.getLogger("c1c.community.live_arena.entry_views")


async def _send_my_registration(manager, interaction: discord.Interaction) -> None:
    service = (manager.service_factory or RegistrationService)(manager.sheet_id)
    try:
        await service.initialize()
        snapshot = await service.get_registration(str(interaction.user.id))
        if snapshot.participant is None:
            await interaction.followup.send(
                embed=error_embed(
                    "You are not currently registered. Use **Join Tournament** to register."
                ),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=registration_embed(snapshot),
            view=RegistrationActionsView(manager, service, snapshot),
            ephemeral=True,
        )
    except Exception as exc:
        log.exception(
            "❌ Live Arena self-service — load failed • user=%s",
            interaction.user.id,
        )
        await interaction.followup.send(embed=_player_error(exc), ephemeral=True)


class MyRegistrationShortcutView(discord.ui.View):
    """Transient shortcut shown when a confirmed player presses Join again."""

    def __init__(self, manager):
        super().__init__(timeout=900)
        self.manager = manager

    @discord.ui.button(label="My Registration", style=discord.ButtonStyle.secondary)
    async def my_registration(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await _send_my_registration(self.manager, interaction)


class RegistrationEntryView(discord.ui.View):
    """Persistent public registration controls with status-aware Join routing."""

    def __init__(self, manager):
        super().__init__(timeout=None)
        self.manager = manager

    @discord.ui.button(
        label="Join Tournament",
        custom_id="live_arena:join",
        style=discord.ButtonStyle.primary,
    )
    async def join(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        service = (self.manager.service_factory or RegistrationService)(
            self.manager.sheet_id
        )
        try:
            await service.initialize()
            snapshot = await service.get_registration(str(interaction.user.id))
        except Exception as exc:
            log.exception(
                "❌ Live Arena signup — registration preflight failed • user=%s",
                interaction.user.id,
            )
            await interaction.followup.send(embed=_player_error(exc), ephemeral=True)
            return

        if snapshot.status == "confirmed":
            tournament_name = snapshot.tournament["tournament_name"]
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Already registered",
                    description=(
                        f"You're already registered for **{tournament_name}**. "
                        "Use **My Registration** to view or update your availability."
                    ),
                    color=colors.c1c_blue,
                ),
                view=MyRegistrationShortcutView(self.manager),
                ephemeral=True,
            )
            return

        if snapshot.status in {"removed", "disqualified"}:
            await interaction.followup.send(
                embed=error_embed(
                    f"Your registration is currently **{snapshot.status}** and cannot "
                    "be restored through player self-service. Please contact a "
                    "tournament organizer."
                ),
                ephemeral=True,
            )
            return

        if snapshot.status and snapshot.status != "withdrawn":
            await interaction.followup.send(
                embed=error_embed(
                    f"Your registration has unsupported status **{snapshot.status}**. "
                    "Please contact a tournament organizer."
                ),
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            embed=timezone_prompt_embed(),
            view=TimezoneSelectView(self.manager),
            ephemeral=True,
        )

    @discord.ui.button(
        label="My Registration",
        custom_id="live_arena:my_registration",
        style=discord.ButtonStyle.secondary,
    )
    async def my_registration(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        await _send_my_registration(self.manager, interaction)
