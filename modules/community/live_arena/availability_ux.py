"""Weekly availability UX hardening for Live Arena.

This layer keeps the existing Sheet-backed slot model while making the player-facing
editor explicitly weekly, date-free, and reusable after signup.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

import discord

from shared.theme import colors

from modules.community.live_arena import entry_views, registration, views

_WEEKDAY_NAMES = (
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
)
_WEEKDAY_SHORT = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_UPDATE_ALLOWED_STATUSES = {"signup_open", "signup_closed", "active"}

_original_prepare_availability = views._prepare_availability
_original_timezone_select_init = views.TimezoneSelectView.__init__
_original_get_registration = registration.RegistrationService.get_registration


def _weekday_key(slot) -> int:
    return slot.local_start.weekday()


def _weekly_grouped_lines(localized_slots, selected_ids) -> tuple[str, int, int]:
    selected = {str(value) for value in selected_ids}
    grouped: dict[int, list] = defaultdict(list)
    for slot in localized_slots:
        if slot.slot_id in selected:
            grouped[_weekday_key(slot)].append(slot)

    lines = []
    for weekday in range(7):
        slots = grouped.get(weekday, [])
        if not slots:
            continue
        slots = sorted(slots, key=lambda item: item.local_start.time())
        lines.append(
            f"**{_WEEKDAY_NAMES[weekday]}**\n"
            + ", ".join(
                f"{slot.local_start:%H:%M}–{slot.local_end:%H:%M}" for slot in slots
            )
        )

    detail = "\n".join(lines) or "No saved weekly windows."
    if len(detail) > 3500:
        detail = detail[:3497] + "…"
    return detail, len(selected), len(grouped)


async def _prepare_availability_weekly(
    manager,
    member,
    timezone: str,
    *,
    service=None,
    snapshot=None,
):
    view = await _original_prepare_availability(
        manager,
        member,
        timezone,
        service=service,
        snapshot=snapshot,
    )
    if snapshot is not None:
        view._weekly_snapshot = replace(snapshot, timezone=timezone)
    return view


def _availability_init(
    self,
    manager,
    service,
    preparation,
    timezone: str,
    member,
    *,
    selected=(),
    updating=False,
):
    discord.ui.View.__init__(self, timeout=900)
    self.manager, self.service, self.preparation = manager, service, preparation
    self.timezone, self.member, self.updating = timezone, member, updating

    grouped: dict[int, list] = defaultdict(list)
    for slot in preparation.localized_slots:
        grouped[_weekday_key(slot)].append(slot)
    self.days = [weekday for weekday in range(7) if grouped.get(weekday)]
    self.grouped = {weekday: grouped[weekday] for weekday in self.days}
    if not self.days:
        raise registration.RegistrationError("no enabled availability slots are configured")
    if any(len(values) > 25 for values in self.grouped.values()):
        raise registration.RegistrationError(
            "a local weekday exceeds Discord's 25-option component limit"
        )

    self.index = 0
    enabled_ids = {slot.slot_id for slot in preparation.localized_slots}
    self.selected = {str(value) for value in selected if str(value) in enabled_ids}
    self._build()


def _day_handler(self, weekday: int):
    async def handler(interaction):
        self.index = self.days.index(weekday)
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    return handler


def _availability_build(self):
    self.clear_items()
    weekday = self.days[self.index]
    day_slots = sorted(self.grouped[weekday], key=lambda item: item.local_start.time())
    options = [
        discord.SelectOption(
            label=f"{slot.local_start:%H:%M}–{slot.local_end:%H:%M}",
            value=slot.slot_id,
            default=slot.slot_id in self.selected,
        )
        for slot in day_slots
    ]
    select = discord.ui.Select(
        placeholder="Select ALL weekly times that usually work for you",
        options=options,
        min_values=0,
        max_values=len(options),
        row=0,
    )

    async def selected(interaction):
        self.selected.difference_update(slot.slot_id for slot in day_slots)
        self.selected.update(select.values)
        self._build()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    select.callback = selected
    self.add_item(select)

    for candidate in self.days:
        row = 1 if candidate <= 4 else 2
        style = (
            discord.ButtonStyle.primary
            if candidate == weekday
            else discord.ButtonStyle.secondary
        )
        self.add_item(
            views.ActionButton(
                _WEEKDAY_SHORT[candidate],
                style,
                row,
                _day_handler(self, candidate),
            )
        )

    self.add_item(
        views.ActionButton(
            "Clear Day",
            discord.ButtonStyle.secondary,
            2,
            self.clear_day,
        )
    )
    if self.updating:
        self.add_item(
            views.ActionButton(
                "Change Timezone",
                discord.ButtonStyle.secondary,
                3,
                self.change_timezone,
            )
        )
    self.add_item(
        views.ActionButton(
            "Review Changes" if self.updating else "Review Registration",
            discord.ButtonStyle.primary,
            3,
            self.review,
        )
    )


def _availability_counts(self):
    lookup = {
        slot.slot_id: slot for values in self.grouped.values() for slot in values
    }
    return len(self.selected), len(
        {_weekday_key(lookup[value]) for value in self.selected if value in lookup}
    )


def _availability_embed(self):
    count, days = self.counts()
    weekday = self.days[self.index]
    return discord.Embed(
        title=f"Weekly Availability — {_WEEKDAY_NAMES[weekday]}",
        description=(
            f"Times shown in **{views._timezone_display(self.timezone)}**.\n\n"
            "**These are recurring day-of-the-week windows. They repeat every week "
            "during the tournament and are not tied to a specific calendar date.**\n\n"
            "Select every two-hour window that would usually work on this weekday. "
            "Use the **MON–SUN** buttons to move between weekdays.\n"
            "You need at least **3 windows across 2 different weekdays**.\n\n"
            f"Selected: **{count} of minimum 3** windows across "
            f"**{days} of minimum 2** weekdays."
        ),
        color=colors.c1c_blue,
    )


def _availability_review_embed(self):
    detail, count, day_count = _weekly_grouped_lines(
        self.preparation.localized_slots, self.selected
    )
    action = "changes" if self.updating else "registration"
    return discord.Embed(
        title=f"Review Live Arena {action}",
        color=colors.c1c_blue,
        description=(
            f"**Tournament:** {self.preparation.tournament['tournament_name']}\n"
            f"**Timezone:** {views._timezone_display(self.timezone)}\n"
            f"**Windows:** {count}\n"
            f"**Local days:** {day_count}\n\n"
            "**These are weekly recurring windows, not specific calendar dates.**\n\n"
            f"{detail}"
        ),
    )


async def _change_timezone(self, interaction):
    snapshot = getattr(self, "_weekly_snapshot", None)
    if snapshot is None:
        await interaction.response.edit_message(
            embed=views.timezone_prompt_embed(self.timezone),
            view=views.TimezoneSelectView(self.manager),
        )
        return

    snapshot = replace(
        snapshot,
        timezone=self.timezone,
        selected_slot_ids=tuple(sorted(self.selected)),
    )
    await interaction.response.edit_message(
        embed=views.timezone_prompt_embed(self.timezone),
        view=views.TimezoneSelectView(
            self.manager,
            service=self.service,
            snapshot=snapshot,
        ),
    )


def _timezone_prompt_embed(current_timezone: str = "") -> discord.Embed:
    if current_timezone:
        description = (
            f"**Current timezone:** {views._timezone_display(current_timezone)}\n\n"
            "Keep your current timezone or choose a different one below. The bot uses "
            "your timezone to show weekly availability and match times in local time, "
            "including daylight-saving changes.\n\n"
            "If none of the listed regions fits, choose **Other / My timezone isn't listed**."
        )
        title = "Review your timezone"
    else:
        description = (
            "Choose the region that best matches where you live. The bot uses this to "
            "show weekly availability and future match times in your local time, "
            "including daylight-saving changes automatically.\n\n"
            "Your availability is saved as recurring **day-of-the-week** windows, not "
            "specific calendar dates.\n\n"
            "If none of the listed regions fits, choose **Other / My timezone isn't listed**."
        )
        title = "Choose your timezone"
    return discord.Embed(title=title, description=description, color=colors.c1c_blue)


async def _keep_current_timezone(self, interaction):
    current = getattr(self.snapshot, "timezone", "") if self.snapshot else ""
    if not current:
        await interaction.response.send_message(
            embed=views.error_embed("No current timezone is saved."), ephemeral=True
        )
        return
    await interaction.response.defer()
    try:
        view = await views._prepare_availability(
            self.manager,
            interaction.user,
            current,
            service=self.service,
            snapshot=self.snapshot,
        )
    except Exception as exc:
        views._log_timezone_flow_error(exc, interaction.user.id, updating=True)
        await interaction.edit_original_response(
            embed=views._timezone_player_error(exc), view=self
        )
        return
    await interaction.edit_original_response(embed=view.embed(), view=view)


def _timezone_select_init(self, manager, *, service=None, snapshot=None):
    _original_timezone_select_init(
        self, manager, service=service, snapshot=snapshot
    )
    if snapshot is not None and getattr(snapshot, "timezone", ""):
        self.add_item(
            views.ActionButton(
                "Keep Current Timezone",
                discord.ButtonStyle.primary,
                1,
                _keep_current_timezone.__get__(self, views.TimezoneSelectView),
            )
        )


def _registration_embed(snapshot):
    detail, count, day_count = _weekly_grouped_lines(
        snapshot.localized_slots, snapshot.selected_slot_ids
    )
    description = (
        f"**Tournament:** {snapshot.tournament['tournament_name']}\n"
        f"**Status:** {snapshot.status or 'unknown'}\n"
        f"**Timezone:** "
        f"{views._timezone_display(snapshot.timezone) if snapshot.timezone else 'Not saved'}\n"
        f"**Windows:** {count}\n"
        f"**Local days:** {day_count}\n\n"
        "**These availability windows repeat every week during the tournament. "
        "They are not tied to specific calendar dates.**\n\n"
        f"{detail}"
    )
    if snapshot.status == "withdrawn" and snapshot.tournament_status == "signup_open":
        description += "\n\nUse **Join Tournament** on the public panel to register again."
    elif snapshot.status in {"removed", "disqualified"}:
        description += (
            "\n\nThis status cannot be restored through player self-service."
        )
    return discord.Embed(
        title="My Live Arena registration",
        description=description,
        color=colors.c1c_blue,
    )


def _registration_actions_init(self, manager, service, snapshot):
    discord.ui.View.__init__(self, timeout=900)
    self.manager, self.service, self.snapshot = manager, service, snapshot
    if snapshot.can_update:
        self.add_item(
            views.ActionButton(
                "Update Availability",
                discord.ButtonStyle.primary,
                0,
                self.update,
            )
        )
    if snapshot.can_withdraw:
        self.add_item(
            views.ActionButton(
                "Withdraw", discord.ButtonStyle.danger, 0, self.withdraw
            )
        )


async def _registration_actions_update(self, interaction):
    await interaction.response.defer()
    try:
        view = await views._prepare_availability(
            self.manager,
            interaction.user,
            self.snapshot.timezone,
            service=self.service,
            snapshot=self.snapshot,
        )
    except Exception as exc:
        views._log_timezone_flow_error(exc, interaction.user.id, updating=True)
        await interaction.edit_original_response(embed=views._player_error(exc), view=self)
        return
    await interaction.edit_original_response(embed=view.embed(), view=view)


async def _get_registration_weekly(self, user_id: str):
    snapshot = await _original_get_registration(self, user_id)
    can_update = (
        snapshot.status == "confirmed"
        and snapshot.tournament_status in _UPDATE_ALLOWED_STATUSES
    )
    if snapshot.can_update == can_update:
        return snapshot
    return replace(snapshot, can_update=can_update)


async def _update_availability_weekly(
    self, user_id: str, timezone: str, slot_ids: list[str]
) -> None:
    config, tournament, _, slots = await self._context()
    tournament_id = config["ACTIVE_TOURNAMENT_ID"]
    async with registration._locks[(self.sheet_id, tournament_id)]:
        self._require_status(tournament, _UPDATE_ALLOWED_STATUSES)
        participants, availability = (
            await self.repository.participants(),
            await self.repository.availability(),
        )
        row = self._confirmed(participants, tournament_id, str(user_id))
        selected = registration.validate_availability(
            timezone, slot_ids, slots, tournament["signup_closes_at_utc"]
        )
        now = registration.utc_iso(self.clock())
        old_p, old_a = (
            [dict(item) for item in participants],
            [dict(item) for item in availability],
        )
        row.update(timezone=timezone, updated_at_utc=now)
        await self.repository.persist_core_state(
            participants,
            self._replacement(
                availability, tournament_id, str(user_id), selected, now
            ),
            previous_participants=old_p,
            previous_availability=old_a,
        )
        await self._audit(
            tournament_id,
            str(user_id),
            "availability_updated",
            {"timezone": timezone, "availability_count": len(selected)},
            now,
        )


def install() -> None:
    """Install the weekly availability behavior over the legacy date-based UI."""

    views._grouped_lines = _weekly_grouped_lines
    views._prepare_availability = _prepare_availability_weekly
    views.timezone_prompt_embed = _timezone_prompt_embed
    views.registration_embed = _registration_embed

    views.AvailabilityView.__init__ = _availability_init
    views.AvailabilityView._build = _availability_build
    views.AvailabilityView.counts = _availability_counts
    views.AvailabilityView.embed = _availability_embed
    views.AvailabilityView.review_embed = _availability_review_embed
    views.AvailabilityView.change_timezone = _change_timezone

    views.TimezoneSelectView.__init__ = _timezone_select_init

    views.RegistrationActionsView.__init__ = _registration_actions_init
    views.RegistrationActionsView.update = _registration_actions_update

    registration.RegistrationService.get_registration = _get_registration_weekly
    registration.RegistrationService.update_availability = _update_availability_weekly

    # entry_views imported these symbols directly before installers run, so keep its
    # module-level references aligned with the patched player UI.
    entry_views.registration_embed = _registration_embed
    entry_views.timezone_prompt_embed = _timezone_prompt_embed
