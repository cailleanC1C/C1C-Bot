"""Persistent and ephemeral Discord UI for registration."""

from __future__ import annotations
import discord

CUSTOM_IDS = {
    action: f"live_arena:registration:{action}"
    for action in (
        "join_tournament",
        "my_registration",
        "update_availability",
        "withdraw",
        "open_registration",
        "close_registration",
        "reopen_registration",
        "view_roster",
        "refresh_registration",
    )
}


def style(value):
    return {
        "primary": discord.ButtonStyle.primary,
        "secondary": discord.ButtonStyle.secondary,
        "success": discord.ButtonStyle.success,
        "danger": discord.ButtonStyle.danger,
    }.get(str(value).lower(), discord.ButtonStyle.secondary)


class RoutedButton(discord.ui.Button):
    def __init__(self, cog, row):
        action = str(row.get("action", row.get("component_key", ""))).strip()
        super().__init__(
            label=str(row.get("label", "")),
            emoji=str(row.get("emoji") or "") or None,
            style=style(row.get("style")),
            custom_id=CUSTOM_IDS[action],
        )
        self.cog = cog
        self.action = action

    async def callback(self, interaction):
        await self.cog.handle_action(self.action, interaction)


class PersistentPanel(discord.ui.View):
    def __init__(self, cog, rows, actions):
        super().__init__(timeout=None)
        by_action = {
            str(r.get("action", r.get("component_key", ""))).strip(): r
            for r in rows
            if str(r.get("active", "true")).lower() not in {"false", "0", "no"}
        }
        for action in actions:
            if action in by_action:
                self.add_item(RoutedButton(cog, by_action[action]))


class TimezoneModal(discord.ui.Modal):
    def __init__(self, cog, mode, initial=""):
        super().__init__(title="Timezone", timeout=600)
        self.cog = cog
        self.mode = mode
        self.timezone = discord.ui.TextInput(
            label="IANA timezone",
            default=initial,
            placeholder="Europe/Vienna",
            max_length=64,
        )
        self.add_item(self.timezone)

    async def on_submit(self, interaction):
        await self.cog.start_availability(interaction, self.mode, str(self.timezone))


class ConfirmView(discord.ui.View):
    def __init__(self, callback):
        super().__init__(timeout=300)
        self._callback = callback

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, button):
        await self._callback(interaction)
        self.stop()
