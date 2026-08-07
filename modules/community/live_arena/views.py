"""Discord views for Live Arena player signup and self-service."""

from __future__ import annotations

import logging
from collections import defaultdict

import discord

from shared.theme import colors

from modules.community.live_arena.messages import discord_timestamp, load_messages, load_pr3_config
from modules.community.live_arena.registration import (
    RegistrationError,
    RegistrationService,
    SignupPreparation,
    localize_availability,
    validate_availability,
)
from modules.community.live_arena.service import LiveArenaConfigError

log = logging.getLogger("c1c.community.live_arena.views")


def error_embed(message: object) -> discord.Embed:
    return discord.Embed(title="Live Arena", description=str(message), color=colors.c1c_blue)


def _player_error(exc: Exception) -> discord.Embed:
    if isinstance(exc, (RegistrationError, LiveArenaConfigError)):
        return error_embed(exc)
    return error_embed("Something went wrong. Please try again later.")


def _grouped_lines(localized_slots, selected_ids) -> tuple[str, int, int]:
    selected = set(selected_ids)
    grouped = defaultdict(list)
    for slot in localized_slots:
        if slot.slot_id in selected:
            grouped[slot.local_start.date()].append(slot)
    lines = [
        f"**{day:%A, %d %B}**\n"
        + ", ".join(f"{slot.local_start:%H:%M}–{slot.local_end:%H:%M}" for slot in grouped[day])
        for day in sorted(grouped)
    ]
    detail = "\n".join(lines) or "No saved windows."
    if len(detail) > 3500:
        detail = detail[:3497] + "…"
    return detail, len(selected), len(grouped)


class JoinTournamentView(discord.ui.View):
    def __init__(self, manager):
        super().__init__(timeout=None)
        self.manager = manager

    @discord.ui.button(label="Join Tournament", custom_id="live_arena:join", style=discord.ButtonStyle.primary)
    async def join(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.send_modal(TimezoneModal(self.manager))

    @discord.ui.button(label="My Registration", custom_id="live_arena:my_registration", style=discord.ButtonStyle.secondary)
    async def my_registration(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        service = (self.manager.service_factory or RegistrationService)(self.manager.sheet_id)
        try:
            await service.initialize()
            snapshot = await service.get_registration(str(interaction.user.id))
            if snapshot.participant is None:
                await interaction.followup.send(
                    embed=error_embed("You are not currently registered. Use **Join Tournament** to register."),
                    ephemeral=True,
                )
                return
            await interaction.followup.send(
                embed=registration_embed(snapshot),
                view=RegistrationActionsView(self.manager, service, snapshot),
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("❌ Live Arena self-service — load failed • user=%s", interaction.user.id)
            await interaction.followup.send(embed=_player_error(exc), ephemeral=True)


class TimezoneModal(discord.ui.Modal, title="Live Arena signup"):
    timezone_input = discord.ui.TextInput(label="Timezone", required=True, placeholder="Europe/Vienna, America/New_York, Asia/Kolkata")

    def __init__(self, manager):
        super().__init__()
        self.manager = manager

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        member = interaction.user
        service = (self.manager.service_factory or RegistrationService)(self.manager.sheet_id)
        try:
            await service.initialize()
            preparation = await service.prepare_signup(
                str(member.id), [str(role.id) for role in getattr(member, "roles", [])], str(self.timezone_input)
            )
            config, _ = await load_pr3_config(self.manager.sheet_id)
            await load_messages(self.manager.sheet_id, config["MESSAGES_TAB"])
            view = AvailabilityView(self.manager, service, preparation, str(self.timezone_input), member)
            await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True)
        except Exception as exc:
            log.exception("❌ Live Arena signup — preflight failed • user=%s", member.id)
            await interaction.followup.send(embed=_player_error(exc), ephemeral=True)


class UpdateTimezoneModal(discord.ui.Modal, title="Update Live Arena availability"):
    def __init__(self, manager, service, snapshot):
        super().__init__()
        self.manager, self.service, self.snapshot = manager, service, snapshot
        self.timezone_input = discord.ui.TextInput(
            label="Timezone", required=True, default=snapshot.timezone,
            placeholder="Europe/Vienna, America/New_York, Asia/Kolkata",
        )
        self.add_item(self.timezone_input)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            timezone = str(self.timezone_input)
            localized = localize_availability(
                timezone, list(self.snapshot.slots), self.snapshot.tournament["signup_closes_at_utc"]
            )
            preparation = SignupPreparation(
                self.snapshot.config, self.snapshot.tournament, self.snapshot.slots, tuple(localized)
            )
            view = AvailabilityView(
                self.manager, self.service, preparation, timezone, interaction.user,
                selected=self.snapshot.selected_slot_ids, updating=True,
            )
            await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True)
        except Exception as exc:
            if not isinstance(exc, (RegistrationError, LiveArenaConfigError)):
                log.exception(
                    "❌ Live Arena self-service — availability preparation failed • tournament=%s • user=%s • action=update_timezone",
                    self.snapshot.config["ACTIVE_TOURNAMENT_ID"],
                    interaction.user.id,
                )
            await interaction.followup.send(embed=_player_error(exc), ephemeral=True)


class AvailabilityView(discord.ui.View):
    def __init__(self, manager, service, preparation, timezone: str, member, *, selected=(), updating=False):
        super().__init__(timeout=900)
        self.manager, self.service, self.preparation = manager, service, preparation
        self.timezone, self.member, self.updating = timezone, member, updating
        grouped = defaultdict(list)
        for slot in preparation.localized_slots:
            grouped[slot.local_start.date()].append(slot)
        self.days, self.grouped = sorted(grouped), dict(grouped)
        if not self.days:
            raise RegistrationError("no enabled availability slots are configured")
        if any(len(values) > 25 for values in grouped.values()):
            raise RegistrationError("a local day exceeds Discord's 25-option component limit")
        self.index = 0
        enabled_ids = {slot.slot_id for slot in preparation.localized_slots}
        self.selected = {str(value) for value in selected if str(value) in enabled_ids}
        self._build()

    def _build(self):
        self.clear_items()
        day = self.days[self.index]
        options = [discord.SelectOption(label=f"{slot.local_start:%H:%M}–{slot.local_end:%H:%M}", value=slot.slot_id, default=slot.slot_id in self.selected) for slot in self.grouped[day]]
        select = discord.ui.Select(placeholder="Select available windows", options=options, min_values=0, max_values=len(options), row=0)

        async def selected(interaction):
            self.selected.difference_update(slot.slot_id for slot in self.grouped[day])
            self.selected.update(select.values)
            self._build()
            await interaction.response.edit_message(embed=self.embed(), view=self)

        select.callback = selected
        self.add_item(select)
        self.add_item(ActionButton("Previous Day", discord.ButtonStyle.secondary, 1, self.previous, disabled=self.index == 0))
        self.add_item(ActionButton("Next Day", discord.ButtonStyle.secondary, 1, self.next, disabled=self.index == len(self.days) - 1))
        self.add_item(ActionButton("Clear Day", discord.ButtonStyle.secondary, 2, self.clear_day))
        self.add_item(ActionButton("Review Changes" if self.updating else "Review Registration", discord.ButtonStyle.primary, 2, self.review))

    def counts(self):
        lookup = {slot.slot_id: slot for values in self.grouped.values() for slot in values}
        return len(self.selected), len({lookup[value].local_start.date() for value in self.selected})

    def embed(self):
        count, days = self.counts()
        return discord.Embed(
            title=f"Availability — {self.days[self.index]:%A, %d %B}",
            description=f"Times shown in **{self.timezone}**. Selected: **{count}** windows across **{days}** local days.",
            color=colors.c1c_blue,
        )

    async def previous(self, interaction):
        self.index -= 1; self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def next(self, interaction):
        self.index += 1; self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def clear_day(self, interaction):
        self.selected.difference_update(slot.slot_id for slot in self.grouped[self.days[self.index]])
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    async def review(self, interaction):
        try:
            validate_availability(self.timezone, list(self.selected), list(self.preparation.slots), self.preparation.tournament["signup_closes_at_utc"])
        except Exception as exc:
            await interaction.response.send_message(embed=_player_error(exc), ephemeral=True)
            return
        await interaction.response.edit_message(embed=self.review_embed(), view=ReviewView(self))

    def review_embed(self):
        detail, count, day_count = _grouped_lines(self.preparation.localized_slots, self.selected)
        action = "changes" if self.updating else "registration"
        return discord.Embed(
            title=f"Review Live Arena {action}", color=colors.c1c_blue,
            description=(f"**Tournament:** {self.preparation.tournament['tournament_name']}\n**Timezone:** {self.timezone}\n**Windows:** {count}\n**Local days:** {day_count}\n\n{detail}"),
        )


class ReviewView(discord.ui.View):
    def __init__(self, availability):
        super().__init__(timeout=900)
        self.availability = availability
        self.add_item(ActionButton("Back", discord.ButtonStyle.secondary, 0, self.back))
        self.add_item(ActionButton("Save Changes" if availability.updating else "Submit Registration", discord.ButtonStyle.primary, 0, self.submit))

    async def back(self, interaction):
        self.availability._build()
        await interaction.response.edit_message(embed=self.availability.embed(), view=self.availability)

    async def submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        flow, member = self.availability, interaction.user
        try:
            config, _ = await load_pr3_config(flow.manager.sheet_id)
            messages = await load_messages(flow.manager.sheet_id, config["MESSAGES_TAB"])
            if flow.updating:
                await flow.service.update_availability(str(member.id), flow.timezone, list(flow.selected))
                embed = messages["availability_updated"].embed(participant=member.mention, tournament_name=flow.preparation.tournament["tournament_name"])
            else:
                await flow.service.register(str(member.id), member.display_name, [str(role.id) for role in getattr(member, "roles", [])], flow.timezone, list(flow.selected))
                embed = messages["signup_confirmed"].embed(participant=member.mention, tournament_name=flow.preparation.tournament["tournament_name"], signup_deadline=discord_timestamp(flow.preparation.tournament["signup_closes_at_utc"]))
        except Exception as exc:
            if flow.updating and not isinstance(exc, (RegistrationError, LiveArenaConfigError)):
                log.exception(
                    "❌ Live Arena self-service — save failed • tournament=%s • user=%s • action=update_availability",
                    flow.preparation.config["ACTIVE_TOURNAMENT_ID"],
                    member.id,
                )
            await interaction.followup.send(embed=_player_error(exc), ephemeral=True)
            return
        if flow.updating:
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        await _assign_role(config, flow, interaction, embed)
        await interaction.followup.send(embed=embed, ephemeral=True)
        try:
            await flow.manager.sync()
        except Exception:
            log.exception("⚠️ Live Arena panel — post-registration refresh failed")


async def _assign_role(config, flow, interaction, embed):
    role_id = int(config["PARTICIPANT_ROLE_ID"])
    member = interaction.user
    role = interaction.guild.get_role(role_id) if interaction.guild else None
    if role is None:
        _role_warning(embed)
        log.error("❌ Live Arena role — missing • tournament=%s • user=%s • role=%s", flow.preparation.config["ACTIVE_TOURNAMENT_ID"], member.id, role_id)
    elif role not in getattr(member, "roles", []):
        try:
            await member.add_roles(role, reason="Live Arena registration confirmed")
        except Exception:
            _role_warning(embed)
            log.exception("❌ Live Arena role — assignment failed • tournament=%s • user=%s • role=%s", flow.preparation.config["ACTIVE_TOURNAMENT_ID"], member.id, role_id)


def _role_warning(embed):
    embed.add_field(name="Role sync warning", value="Your tournament role could not be synced automatically.", inline=False)


def registration_embed(snapshot):
    detail, count, day_count = _grouped_lines(snapshot.localized_slots, snapshot.selected_slot_ids)
    description = (
        f"**Tournament:** {snapshot.tournament['tournament_name']}\n"
        f"**Status:** {snapshot.status or 'unknown'}\n**Timezone:** {snapshot.timezone or 'Not saved'}\n"
        f"**Windows:** {count}\n**Local days:** {day_count}\n\n{detail}"
    )
    if snapshot.status == "withdrawn" and snapshot.tournament_status == "signup_open":
        description += "\n\nUse **Join Tournament** on the public panel to register again."
    elif snapshot.status in {"removed", "disqualified"}:
        description += "\n\nThis status cannot be restored through player self-service."
    return discord.Embed(title="My Live Arena registration", description=description, color=colors.c1c_blue)


class RegistrationActionsView(discord.ui.View):
    def __init__(self, manager, service, snapshot):
        super().__init__(timeout=900)
        self.manager, self.service, self.snapshot = manager, service, snapshot
        if snapshot.can_update:
            self.add_item(ActionButton("Update Availability", discord.ButtonStyle.primary, 0, self.update))
        if snapshot.can_withdraw:
            self.add_item(ActionButton("Withdraw", discord.ButtonStyle.danger, 0, self.withdraw))

    async def update(self, interaction):
        await interaction.response.send_modal(UpdateTimezoneModal(self.manager, self.service, self.snapshot))

    async def withdraw(self, interaction):
        embed = discord.Embed(title="Confirm withdrawal", description=f"Withdraw from **{self.snapshot.tournament['tournament_name']}**? Your saved availability will be retained.", color=colors.c1c_blue)
        await interaction.response.edit_message(embed=embed, view=WithdrawalConfirmationView(self.manager, self.service, self.snapshot))


class WithdrawalConfirmationView(discord.ui.View):
    def __init__(self, manager, service, snapshot):
        super().__init__(timeout=900)
        self.manager, self.service, self.snapshot = manager, service, snapshot

    @discord.ui.button(label="Confirm Withdrawal", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction, _button):
        await interaction.response.send_modal(WithdrawalReasonModal(self.manager, self.service, self.snapshot))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, _button):
        await interaction.response.edit_message(embed=registration_embed(self.snapshot), view=RegistrationActionsView(self.manager, self.service, self.snapshot))


class WithdrawalReasonModal(discord.ui.Modal, title="Confirm Live Arena withdrawal"):
    reason = discord.ui.TextInput(label="Reason", required=False, max_length=500, style=discord.TextStyle.paragraph)

    def __init__(self, manager, service, snapshot):
        super().__init__()
        self.manager, self.service, self.snapshot = manager, service, snapshot

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        member = interaction.user
        try:
            config, _ = await load_pr3_config(self.manager.sheet_id)
            messages = await load_messages(self.manager.sheet_id, config["MESSAGES_TAB"])
            await self.service.withdraw(str(member.id), str(self.reason).strip())
        except Exception as exc:
            if not isinstance(exc, (RegistrationError, LiveArenaConfigError)):
                log.exception(
                    "❌ Live Arena self-service — withdrawal failed • tournament=%s • user=%s • action=withdraw",
                    self.snapshot.config["ACTIVE_TOURNAMENT_ID"],
                    member.id,
                )
            await interaction.followup.send(embed=_player_error(exc), ephemeral=True)
            return
        embed = messages["withdrawal_confirmed"].embed(participant=member.mention, tournament_name=self.snapshot.tournament["tournament_name"])
        await self._remove_role(interaction, config, embed)
        if self.snapshot.tournament_status == "signup_open":
            try:
                await self.manager.sync()
            except Exception:
                log.exception("⚠️ Live Arena panel — post-withdrawal refresh failed • tournament=%s • user=%s", self.snapshot.config["ACTIVE_TOURNAMENT_ID"], member.id)
        await interaction.followup.send(embed=embed, ephemeral=True)

    async def _remove_role(self, interaction, config, embed):
        role_id = int(config["PARTICIPANT_ROLE_ID"])
        member = interaction.user
        role = interaction.guild.get_role(role_id) if interaction.guild else None
        if role is None:
            _role_warning(embed)
            log.error("❌ Live Arena role — removal target missing • tournament=%s • user=%s • role=%s", self.snapshot.config["ACTIVE_TOURNAMENT_ID"], member.id, role_id)
        elif role in getattr(member, "roles", []):
            try:
                await member.remove_roles(role, reason="Live Arena registration withdrawn")
            except Exception:
                _role_warning(embed)
                log.exception("❌ Live Arena role — removal failed • tournament=%s • user=%s • role=%s", self.snapshot.config["ACTIVE_TOURNAMENT_ID"], member.id, role_id)


class ActionButton(discord.ui.Button):
    def __init__(self, label, style, row, handler, disabled=False):
        super().__init__(label=label, style=style, row=row, disabled=disabled)
        self.handler = handler

    async def callback(self, interaction):
        await self.handler(interaction)
