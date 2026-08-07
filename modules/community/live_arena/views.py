"""Discord views for the first player-facing Live Arena signup slice."""

from __future__ import annotations

import logging
from collections import defaultdict

import discord

from shared.theme import colors

from modules.community.live_arena.messages import (
    discord_timestamp,
    load_messages,
    load_pr3_config,
)
from modules.community.live_arena.registration import (
    RegistrationError,
    RegistrationService,
    validate_availability,
)

log = logging.getLogger("c1c.community.live_arena.views")


def error_embed(message: object) -> discord.Embed:
    return discord.Embed(
        title="Live Arena signup", description=str(message), color=colors.c1c_blue
    )


class JoinTournamentView(discord.ui.View):
    def __init__(self, manager):
        super().__init__(timeout=None)
        self.manager = manager

    @discord.ui.button(
        label="Join Tournament",
        custom_id="live_arena:join",
        style=discord.ButtonStyle.primary,
    )
    async def join(self, interaction: discord.Interaction, _button: discord.ui.Button):
        # Opening the modal is deliberately the first and only operation here.
        await interaction.response.send_modal(TimezoneModal(self.manager))


class TimezoneModal(discord.ui.Modal, title="Live Arena signup"):
    timezone_input = discord.ui.TextInput(
        label="Timezone",
        required=True,
        placeholder="Europe/Vienna, America/New_York, Asia/Kolkata",
    )

    def __init__(self, manager):
        super().__init__()
        self.manager = manager

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        member = interaction.user
        roles = [str(role.id) for role in getattr(member, "roles", [])]
        service = (self.manager.service_factory or RegistrationService)(
            self.manager.sheet_id
        )
        try:
            await service.initialize()
            preparation = await service.prepare_signup(
                str(member.id), roles, str(self.timezone_input)
            )
            config, _ = await load_pr3_config(self.manager.sheet_id)
            await load_messages(self.manager.sheet_id, config["MESSAGES_TAB"])
            view = AvailabilityView(
                self.manager, service, preparation, str(self.timezone_input), member
            )
            await interaction.followup.send(
                embed=view.embed(), view=view, ephemeral=True
            )
        except Exception as exc:
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


class AvailabilityView(discord.ui.View):
    def __init__(self, manager, service, preparation, timezone: str, member):
        super().__init__(timeout=900)
        self.manager, self.service, self.preparation = manager, service, preparation
        self.timezone, self.member = timezone, member
        grouped = defaultdict(list)
        for slot in preparation.localized_slots:
            grouped[slot.local_start.date()].append(slot)
        self.days = sorted(grouped)
        self.grouped = dict(grouped)
        if any(len(values) > 25 for values in grouped.values()):
            raise RegistrationError(
                "a local day exceeds Discord's 25-option component limit"
            )
        self.index = 0
        self.selected: set[str] = set()
        self._build()

    def _build(self):
        self.clear_items()
        day = self.days[self.index]
        options = [
            discord.SelectOption(
                label=f"{slot.local_start:%H:%M}–{slot.local_end:%H:%M}",
                value=slot.slot_id,
                default=slot.slot_id in self.selected,
            )
            for slot in self.grouped[day]
        ]
        select = discord.ui.Select(
            placeholder="Select available windows",
            options=options,
            min_values=0,
            max_values=len(options),
            row=0,
        )

        async def selected(interaction):
            day_ids = {slot.slot_id for slot in self.grouped[day]}
            self.selected.difference_update(day_ids)
            self.selected.update(select.values)
            self._build()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        select.callback = selected
        self.add_item(select)
        self.add_item(
            ActionButton(
                "Previous Day",
                discord.ButtonStyle.secondary,
                1,
                self.previous,
                disabled=self.index == 0,
            )
        )
        self.add_item(
            ActionButton(
                "Next Day",
                discord.ButtonStyle.secondary,
                1,
                self.next,
                disabled=self.index == len(self.days) - 1,
            )
        )
        self.add_item(
            ActionButton("Clear Day", discord.ButtonStyle.secondary, 2, self.clear_day)
        )
        self.add_item(
            ActionButton(
                "Review Registration", discord.ButtonStyle.primary, 2, self.review
            )
        )

    def counts(self):
        lookup = {
            slot.slot_id: slot for values in self.grouped.values() for slot in values
        }
        return len(self.selected), len(
            {lookup[value].local_start.date() for value in self.selected}
        )

    def embed(self):
        count, days = self.counts()
        current = self.days[self.index]
        return discord.Embed(
            title=f"Availability — {current:%A, %d %B}",
            description=f"Times shown in **{self.timezone}**. Selected: **{count}** windows across **{days}** local days.",
            color=colors.c1c_blue,
        )

    async def previous(self, interaction):
        self.index -= 1
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def next(self, interaction):
        self.index += 1
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def clear_day(self, interaction):
        self.selected.difference_update(
            slot.slot_id for slot in self.grouped[self.days[self.index]]
        )
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def review(self, interaction):
        try:
            validate_availability(
                self.timezone,
                list(self.selected),
                list(self.preparation.slots),
                self.preparation.tournament["signup_closes_at_utc"],
            )
        except Exception as exc:
            await interaction.response.send_message(
                embed=error_embed(exc), ephemeral=True
            )
            return
        await interaction.response.edit_message(
            embed=self.review_embed(), view=ReviewView(self)
        )

    def review_embed(self):
        count, day_count = self.counts()
        lines = []
        for day in self.days:
            chosen = [s for s in self.grouped[day] if s.slot_id in self.selected]
            if chosen:
                lines.append(
                    f"**{day:%A, %d %B}**\n"
                    + ", ".join(
                        f"{s.local_start:%H:%M}–{s.local_end:%H:%M}" for s in chosen
                    )
                )
        detail = "\n".join(lines)
        if len(detail) > 3500:
            detail = detail[:3497] + "…"
        return discord.Embed(
            title="Review Live Arena registration",
            color=colors.c1c_blue,
            description=(
                f"**Tournament:** {self.preparation.tournament['tournament_name']}\n"
                f"**Timezone:** {self.timezone}\n**Windows:** {count}\n**Local days:** {day_count}\n\n{detail}"
            ),
        )


class ReviewView(discord.ui.View):
    def __init__(self, availability):
        super().__init__(timeout=900)
        self.availability = availability

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction, _button):
        self.availability._build()
        await interaction.response.edit_message(
            embed=self.availability.embed(), view=self.availability
        )

    @discord.ui.button(label="Submit Registration", style=discord.ButtonStyle.primary)
    async def submit(self, interaction, _button):
        await interaction.response.defer(ephemeral=True)
        flow, member = self.availability, interaction.user
        try:
            config, _ = await load_pr3_config(flow.manager.sheet_id)
            messages = await load_messages(
                flow.manager.sheet_id, config["MESSAGES_TAB"]
            )
            await flow.service.register(
                str(member.id),
                member.display_name,
                [str(role.id) for role in getattr(member, "roles", [])],
                flow.timezone,
                list(flow.selected),
            )
        except Exception as exc:
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)
            return
        embed = messages["signup_confirmed"].embed(
            participant=member.mention,
            tournament_name=flow.preparation.tournament["tournament_name"],
            signup_deadline=discord_timestamp(
                flow.preparation.tournament["signup_closes_at_utc"]
            ),
        )
        role_id = int(config["PARTICIPANT_ROLE_ID"])
        role = interaction.guild.get_role(role_id) if interaction.guild else None
        if role is None:
            embed.add_field(
                name="Role sync warning",
                value="Your tournament role could not be synced automatically.",
                inline=False,
            )
            log.error(
                "❌ Live Arena role — missing • tournament=%s • user=%s • role=%s",
                flow.preparation.config["ACTIVE_TOURNAMENT_ID"],
                member.id,
                role_id,
            )
        elif role not in getattr(member, "roles", []):
            try:
                await member.add_roles(role, reason="Live Arena registration confirmed")
            except Exception:
                embed.add_field(
                    name="Role sync warning",
                    value="Your tournament role could not be synced automatically.",
                    inline=False,
                )
                log.exception(
                    "❌ Live Arena role — assignment failed • tournament=%s • user=%s • role=%s",
                    flow.preparation.config["ACTIVE_TOURNAMENT_ID"],
                    member.id,
                    role_id,
                )
        await interaction.followup.send(embed=embed, ephemeral=True)
        try:
            await flow.manager.sync()
        except Exception:
            log.exception("⚠️ Live Arena panel — post-registration refresh failed")


class ActionButton(discord.ui.Button):
    def __init__(self, label, style, row, handler, disabled=False):
        super().__init__(label=label, style=style, row=row, disabled=disabled)
        self.handler = handler

    async def callback(self, interaction):
        await self.handler(interaction)
