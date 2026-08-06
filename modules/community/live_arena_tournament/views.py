"""Persistent and ephemeral Discord UI for registration."""

from __future__ import annotations
import discord
from .models import slot_local_datetime

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
        action = str(
            row.get("action_id", row.get("action", row.get("component_key", "")))
        ).strip()
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
            str(
                r.get("action_id", r.get("action", r.get("component_key", "")))
            ).strip(): r
            for r in rows
            if str(r.get("active", "true")).lower() not in {"false", "0", "no"}
        }
        for action in actions:
            if action in by_action:
                self.add_item(RoutedButton(cog, by_action[action]))

    def disable_actions(self, actions):
        for item in self.children:
            item.disabled = getattr(item, "action", "") in actions


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


class SlotSelect(discord.ui.Select):
    def __init__(self, parent, day, slots):
        self.parent_view = parent
        options = [
            discord.SelectOption(
                label=f"{parent.local_datetime(s).strftime('%A %H:%M')} local",
                value=s.slot_id,
                default=s.slot_id in parent.selected,
            )
            for s in slots[:25]
        ]
        super().__init__(
            placeholder=f"{day} availability",
            options=options,
            min_values=0,
            max_values=len(options),
            row=0,
        )

    async def callback(self, interaction):
        visible = {o.value for o in self.options}
        self.parent_view.selected.difference_update(visible)
        self.parent_view.selected.update(self.values)
        await self.parent_view.show_page(interaction)


class AvailabilityView(discord.ui.View):
    """Ephemeral, restart-safe-until-submit selector; Sheets remain the durable state."""

    def __init__(self, cog, mode, timezone, slots, selected=(), anchor_monday=None):
        super().__init__(timeout=600)
        self.cog, self.mode, self.timezone = cog, mode, timezone
        self.slots, self.selected, self.page = slots, set(selected), 0
        self.anchor_monday = anchor_monday
        self.days = sorted({self.local_datetime(s).strftime("%A") for s in slots})
        self._render()

    def local_datetime(self, slot):
        return slot_local_datetime(
            slot, self.timezone, anchor_monday=self.anchor_monday
        )

    def _render(self):
        self.clear_items()
        day = self.days[self.page]
        choices = [
            s for s in self.slots if self.local_datetime(s).strftime("%A") == day
        ]
        self.add_item(SlotSelect(self, day, choices))
        previous = discord.ui.Button(
            label="Previous day", disabled=self.page == 0, row=1
        )
        following = discord.ui.Button(
            label="Next day", disabled=self.page == len(self.days) - 1, row=1
        )
        review = discord.ui.Button(
            label=f"Review ({len(self.selected)})",
            style=discord.ButtonStyle.success,
            row=2,
        )
        previous.callback = self.previous
        following.callback = self.following
        review.callback = self.review
        self.add_item(previous)
        self.add_item(following)
        self.add_item(review)

    async def show_page(self, interaction):
        self._render()
        await interaction.response.edit_message(
            content=f"Choose local-time windows — {self.days[self.page]}", view=self
        )

    async def previous(self, interaction):
        self.page -= 1
        await self.show_page(interaction)

    async def following(self, interaction):
        self.page += 1
        await self.show_page(interaction)

    async def review(self, interaction):
        labels = [
            self.local_datetime(s).strftime("%A %H:%M")
            for s in self.slots
            if s.slot_id in self.selected
        ]
        await interaction.response.edit_message(
            content="Review your availability:\n"
            + ("\n".join(f"• {x}" for x in labels) or "• None selected"),
            view=SubmitAvailabilityView(self),
        )


class SubmitAvailabilityView(discord.ui.View):
    def __init__(self, session):
        super().__init__(timeout=600)
        self.session = session

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, button):
        await self.session.show_page(interaction)

    @discord.ui.button(label="Submit registration", style=discord.ButtonStyle.success)
    async def submit(self, interaction, button):
        await self.session.cog.submit_availability(interaction, self.session)
        self.stop()


class RosterSelect(discord.ui.Select):
    def __init__(self, parent, participants):
        self.parent_view = parent
        super().__init__(
            placeholder="Choose participant",
            options=[
                discord.SelectOption(
                    label=f"Slot {p.get('participant_slot')}: {p.get('display_name_at_signup') or p.get('discord_user_id')}",
                    value=str(p.get("discord_user_id")),
                    description=str(p.get("status")),
                )
                for p in participants[:25]
            ],
        )

    async def callback(self, interaction):
        self.parent_view.user_id = self.values[0]
        await interaction.response.edit_message(view=self.parent_view)


class RosterView(discord.ui.View):
    def __init__(self, cog, participants):
        super().__init__(timeout=300)
        self.cog, self.user_id = cog, None
        if participants:
            self.add_item(RosterSelect(self, participants))

    @discord.ui.button(label="Remove", style=discord.ButtonStyle.danger, row=1)
    async def remove(self, interaction, button):
        await self.cog.organizer_participant(interaction, self.user_id, "removed")

    @discord.ui.button(label="Restore", style=discord.ButtonStyle.success, row=1)
    async def restore(self, interaction, button):
        await self.cog.organizer_participant(interaction, self.user_id, "confirmed")
