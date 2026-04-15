"""Fusion role opt-in/out button view helpers."""

from __future__ import annotations

import logging
from typing import Literal

import discord
from discord.ext import commands

from shared.sheets import fusion as fusion_sheets

log = logging.getLogger("c1c.community.fusion.opt_in")

_FUSION_OPT_IN_CUSTOM_ID = "fusion:opt_in"
_FUSION_OPT_OUT_CUSTOM_ID = "fusion:opt_out"


async def _send_ephemeral(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
        return
    await interaction.response.send_message(message, ephemeral=True)


async def _resolve_member(interaction: discord.Interaction) -> discord.Member | None:
    if isinstance(interaction.user, discord.Member):
        return interaction.user

    guild = interaction.guild
    if guild is None:
        return None

    member = guild.get_member(interaction.user.id)
    if member is not None:
        return member

    try:
        return await guild.fetch_member(interaction.user.id)
    except Exception:
        return None


async def _resolve_opt_in_role(interaction: discord.Interaction) -> tuple[discord.Member | None, discord.Role | None]:
    target = await fusion_sheets.get_publishable_fusion()
    role_id = target.opt_in_role_id if target is not None else None
    if role_id is None:
        await _send_ephemeral(interaction, "No fusion opt-in role is configured.")
        return None, None

    guild = interaction.guild
    if guild is None:
        await _send_ephemeral(interaction, "Fusion role actions only work in a server.")
        return None, None

    member = await _resolve_member(interaction)
    if member is None:
        await _send_ephemeral(interaction, "Couldn’t resolve your member record right now.")
        return None, None

    role = guild.get_role(role_id)
    if role is None:
        log.warning(
            "fusion opt-in role missing in guild",
            extra={"guild_id": guild.id, "role_id": role_id},
        )
        await _send_ephemeral(interaction, "Fusion role is missing in this server.")
        return member, None

    return member, role


async def _handle_opt_action(interaction: discord.Interaction, *, action: Literal["in", "out"]) -> None:
    try:
        member, role = await _resolve_opt_in_role(interaction)
    except Exception:
        log.exception("fusion opt button failed to resolve role")
        await _send_ephemeral(interaction, "Temporary issue. Try again shortly.")
        return

    if member is None or role is None:
        return

    has_role = role in member.roles

    if action == "in":
        if has_role:
            await _send_ephemeral(interaction, "Already opted in.")
            return
        try:
            await member.add_roles(role, reason="Fusion role opt-in button")
        except Exception:
            log.exception(
                "fusion opt-in add role failed",
                extra={"guild_id": member.guild.id, "user_id": member.id, "role_id": role.id},
            )
            await _send_ephemeral(interaction, "Couldn’t update your fusion role right now.")
            return
        await _send_ephemeral(interaction, "Opted in. You’ll get fusion pings.")
        return

    if not has_role:
        await _send_ephemeral(interaction, "You’re already opted out.")
        return

    try:
        await member.remove_roles(role, reason="Fusion role opt-out button")
    except Exception:
        log.exception(
            "fusion opt-out remove role failed",
            extra={"guild_id": member.guild.id, "user_id": member.id, "role_id": role.id},
        )
        await _send_ephemeral(interaction, "Couldn’t update your fusion role right now.")
        return

    await _send_ephemeral(interaction, "Opted out. No more fusion pings.")


class FusionOptInView(discord.ui.View):
    """Persistent button view for fusion opt-in role management."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Opt In",
        style=discord.ButtonStyle.success,
        custom_id=_FUSION_OPT_IN_CUSTOM_ID,
    )
    async def opt_in_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await _handle_opt_action(interaction, action="in")

    @discord.ui.button(
        label="Opt Out",
        style=discord.ButtonStyle.secondary,
        custom_id=_FUSION_OPT_OUT_CUSTOM_ID,
    )
    async def opt_out_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await _handle_opt_action(interaction, action="out")


def build_fusion_opt_in_view(target: fusion_sheets.FusionRow) -> discord.ui.View | None:
    """Build the reusable fusion button row when the role is configured."""

    if target.opt_in_role_id is None:
        return None
    return FusionOptInView()


def register_persistent_fusion_views(bot: commands.Bot) -> None:
    """Register persistent fusion button handlers on startup."""

    bot.add_view(FusionOptInView())


__all__ = [
    "FusionOptInView",
    "build_fusion_opt_in_view",
    "register_persistent_fusion_views",
]
