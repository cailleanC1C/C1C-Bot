from __future__ import annotations

import logging

import discord

log = logging.getLogger("c1c.community.reset_reminders.views")


class ResetReminderView(discord.ui.View):
    def __init__(self, role_id: int, label_opt_in: str, label_opt_out: str):
        super().__init__(timeout=None)
        self.role_id = role_id

        self.add_item(
            _ResetReminderButton(
                role_id=role_id,
                action="in",
                label=label_opt_in,
            )
        )
        self.add_item(
            _ResetReminderButton(
                role_id=role_id,
                action="out",
                label=label_opt_out,
            )
        )


class _ResetReminderButton(discord.ui.Button[ResetReminderView]):
    def __init__(self, *, role_id: int, action: str, label: str) -> None:
        custom_id = f"reset_reminder:{role_id}:{action}"
        style = discord.ButtonStyle.success if action == "in" else discord.ButtonStyle.secondary
        super().__init__(label=label, style=style, custom_id=custom_id)
        self.role_id = role_id
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        member = interaction.user if isinstance(interaction.user, discord.Member) else None
        guild = interaction.guild
        if guild is None or member is None:
            await _reply(interaction, "This button only works inside the server.")
            return

        role = guild.get_role(self.role_id)
        if role is None:
            log.warning(
                "reset reminder role missing",
                extra={"role_id": self.role_id, "guild_id": guild.id, "action": self.action},
            )
            await _reply(interaction, "That reminder role is missing. Please contact staff.")
            return

        try:
            if self.action == "in":
                await member.add_roles(role, reason="reset reminder opt-in")
                await _reply(interaction, f"You are now subscribed to {role.mention} reminders.")
                return

            await member.remove_roles(role, reason="reset reminder opt-out")
            await _reply(interaction, f"You are no longer subscribed to {role.mention} reminders.")
        except discord.Forbidden:
            log.exception(
                "reset reminder role update forbidden",
                extra={"role_id": self.role_id, "guild_id": guild.id, "user_id": member.id, "action": self.action},
            )
            await _reply(interaction, "I do not have permission to update that role.")
        except Exception:
            log.exception(
                "reset reminder role update failed",
                extra={"role_id": self.role_id, "guild_id": guild.id, "user_id": member.id, "action": self.action},
            )
            await _reply(interaction, "Something went wrong while updating your reminder role.")


async def _reply(interaction: discord.Interaction, message: str) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
        return
    await interaction.response.send_message(message, ephemeral=True)


__all__ = ["ResetReminderView"]
