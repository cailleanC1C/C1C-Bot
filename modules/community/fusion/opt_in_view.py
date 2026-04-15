"""Fusion role and personal-progress button view helpers."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Sequence
from typing import Literal

import discord
from discord.ext import commands

from shared.sheets import fusion as fusion_sheets

log = logging.getLogger("c1c.community.fusion.opt_in")

_FUSION_OPT_IN_CUSTOM_ID = "fusion:opt_in"
_FUSION_OPT_OUT_CUSTOM_ID = "fusion:opt_out"
_FUSION_MY_PROGRESS_CUSTOM_ID = "fusion:my_progress"
_FUSION_PROGRESS_EVENT_CUSTOM_ID = "fusion:progress:event"
_FUSION_PROGRESS_STATUS_CUSTOM_ID = "fusion:progress:status"

_STATUS_ORDER = ("done", "in_progress", "skipped", "not_started")
_STATUS_LABELS = {
    "not_started": "Not Started",
    "in_progress": "In Progress",
    "done": "Done",
    "skipped": "Skipped",
}


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


def _build_progress_summary_embed(
    *,
    target: fusion_sheets.FusionRow,
    events: Sequence[fusion_sheets.FusionEventRow],
    progress_by_event: dict[str, str],
    selected_event_id: str | None = None,
    last_update: tuple[str, str] | None = None,
) -> discord.Embed:
    counts = {status: 0 for status in _STATUS_ORDER}
    for event in events:
        status = progress_by_event.get(event.event_id, "not_started")
        if status not in counts:
            status = "not_started"
        counts[status] += 1

    embed = discord.Embed(
        title=f"My Progress — {target.fusion_name}",
        description="Private tracker for your fusion events.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Done", value=str(counts["done"]), inline=True)
    embed.add_field(name="In Progress", value=str(counts["in_progress"]), inline=True)
    embed.add_field(name="Skipped", value=str(counts["skipped"]), inline=True)
    embed.add_field(name="Not Started", value=str(counts["not_started"]), inline=True)
    embed.add_field(name="Total Events", value=str(len(events)), inline=True)

    if selected_event_id:
        selected = next((event for event in events if event.event_id == selected_event_id), None)
        if selected is not None:
            current = progress_by_event.get(selected.event_id, "not_started")
            embed.add_field(
                name="Selected Event",
                value=f"{selected.event_name} — {_STATUS_LABELS.get(current, 'Not Started')}",
                inline=False,
            )

    if last_update is not None:
        event_name, status = last_update
        embed.add_field(
            name="Last Update",
            value=f"{event_name} → {_STATUS_LABELS.get(status, 'Not Started')}",
            inline=False,
        )

    return embed


class _FusionProgressEventSelect(discord.ui.Select):
    def __init__(self, events: Sequence[fusion_sheets.FusionEventRow], selected_event_id: str | None) -> None:
        options: list[discord.SelectOption] = []
        for event in events[:25]:
            options.append(
                discord.SelectOption(
                    label=event.event_name[:100] or event.event_id[:100],
                    value=event.event_id,
                    default=event.event_id == selected_event_id,
                )
            )
        super().__init__(
            placeholder="Select an event",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=_FUSION_PROGRESS_EVENT_CUSTOM_ID,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, FusionProgressPanelView):
            return
        if interaction.user.id != view.user_id:
            await _send_ephemeral(interaction, "This progress panel belongs to a different user.")
            return

        selected_event_id = self.values[0] if self.values else ""
        view.selected_event_id = selected_event_id or None
        await interaction.response.edit_message(
            embed=view.build_embed(),
            view=view,
        )


class _FusionProgressStatusSelect(discord.ui.Select):
    def __init__(self, selected_status: str | None) -> None:
        options = [
            discord.SelectOption(label="Not Started", value="not_started", default=selected_status == "not_started"),
            discord.SelectOption(label="In Progress", value="in_progress", default=selected_status == "in_progress"),
            discord.SelectOption(label="Done", value="done", default=selected_status == "done"),
            discord.SelectOption(label="Skipped", value="skipped", default=selected_status == "skipped"),
        ]
        super().__init__(
            placeholder="Set status",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=_FUSION_PROGRESS_STATUS_CUSTOM_ID,
            row=1,
            disabled=selected_status is None,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, FusionProgressPanelView):
            return
        if interaction.user.id != view.user_id:
            await _send_ephemeral(interaction, "This progress panel belongs to a different user.")
            return
        if not view.selected_event_id:
            await _send_ephemeral(interaction, "Choose an event first.")
            return

        status = self.values[0] if self.values else "not_started"
        event = view.events_by_id.get(view.selected_event_id)
        if event is None:
            await _send_ephemeral(interaction, "That event is no longer available. Reopen My Progress.")
            return

        now = dt.datetime.now(dt.timezone.utc)
        try:
            await fusion_sheets.upsert_user_event_progress(
                view.fusion_id,
                str(view.user_id),
                event.event_id,
                status,
                now,
            )
        except Exception:
            log.exception(
                "fusion progress status update failed",
                extra={"fusion_id": view.fusion_id, "event_id": event.event_id, "user_id": view.user_id},
            )
            await _send_ephemeral(interaction, "Couldn’t save progress right now. Try again in a moment.")
            return

        view.progress_by_event[event.event_id] = status
        view.last_update = (event.event_name, status)
        view.refresh_items()
        await interaction.response.edit_message(
            embed=view.build_embed(),
            view=view,
        )


class FusionProgressPanelView(discord.ui.View):
    """Ephemeral progress panel for one user and one fusion."""

    def __init__(
        self,
        *,
        user_id: int,
        target: fusion_sheets.FusionRow,
        events: Sequence[fusion_sheets.FusionEventRow],
        progress_by_event: dict[str, str],
    ) -> None:
        super().__init__(timeout=900)
        self.user_id = int(user_id)
        self.fusion_id = target.fusion_id
        self.target = target
        self.events = list(events)
        self.events_by_id = {event.event_id: event for event in self.events}
        self.progress_by_event = dict(progress_by_event)
        self.selected_event_id = self.events[0].event_id if self.events else None
        self.last_update: tuple[str, str] | None = None
        self.refresh_items()

    def refresh_items(self) -> None:
        self.clear_items()
        if not self.events:
            return

        self.add_item(_FusionProgressEventSelect(self.events, self.selected_event_id))
        selected_status = None
        if self.selected_event_id:
            selected_status = self.progress_by_event.get(self.selected_event_id, "not_started")
        self.add_item(_FusionProgressStatusSelect(selected_status))

    def build_embed(self) -> discord.Embed:
        return _build_progress_summary_embed(
            target=self.target,
            events=self.events,
            progress_by_event=self.progress_by_event,
            selected_event_id=self.selected_event_id,
            last_update=self.last_update,
        )


async def _handle_my_progress(interaction: discord.Interaction) -> None:
    try:
        target = await fusion_sheets.get_publishable_fusion()
    except Exception:
        log.exception("fusion my-progress failed to resolve active fusion")
        await _send_ephemeral(interaction, "Couldn’t load fusion progress right now. Try again shortly.")
        return

    if target is None:
        await _send_ephemeral(interaction, "No active fusion is available right now.")
        return

    try:
        events = await fusion_sheets.get_fusion_events(target.fusion_id)
    except Exception:
        log.exception("fusion my-progress failed to load events", extra={"fusion_id": target.fusion_id})
        await _send_ephemeral(interaction, "Couldn’t load events right now. Try again shortly.")
        return

    if not events:
        await _send_ephemeral(interaction, "No fusion events are configured yet.")
        return

    try:
        progress_by_event = await fusion_sheets.get_user_event_progress(
            target.fusion_id,
            str(interaction.user.id),
        )
    except Exception:
        log.exception(
            "fusion my-progress failed to load user progress",
            extra={"fusion_id": target.fusion_id, "user_id": interaction.user.id},
        )
        await _send_ephemeral(interaction, "Couldn’t load your saved progress right now.")
        return

    view = FusionProgressPanelView(
        user_id=interaction.user.id,
        target=target,
        events=events,
        progress_by_event=progress_by_event,
    )
    embed = view.build_embed()
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        return
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class FusionOptInView(discord.ui.View):
    """Persistent button view for fusion role management and private progress."""

    def __init__(self, *, include_opt_buttons: bool = True) -> None:
        super().__init__(timeout=None)
        if not include_opt_buttons:
            self.remove_item(self.opt_in_button)
            self.remove_item(self.opt_out_button)

    @discord.ui.button(
        label="Opt In",
        style=discord.ButtonStyle.success,
        custom_id=_FUSION_OPT_IN_CUSTOM_ID,
        row=0,
    )
    async def opt_in_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await _handle_opt_action(interaction, action="in")

    @discord.ui.button(
        label="Opt Out",
        style=discord.ButtonStyle.secondary,
        custom_id=_FUSION_OPT_OUT_CUSTOM_ID,
        row=0,
    )
    async def opt_out_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await _handle_opt_action(interaction, action="out")

    @discord.ui.button(
        label="My Progress",
        style=discord.ButtonStyle.primary,
        custom_id=_FUSION_MY_PROGRESS_CUSTOM_ID,
        row=0,
    )
    async def my_progress_button(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await _handle_my_progress(interaction)


def build_fusion_opt_in_view(target: fusion_sheets.FusionRow) -> discord.ui.View:
    """Build the reusable fusion button row for announcement and reminders."""

    return FusionOptInView(include_opt_buttons=target.opt_in_role_id is not None)


def register_persistent_fusion_views(bot: commands.Bot) -> None:
    """Register persistent fusion button handlers on startup."""

    bot.add_view(FusionOptInView())


__all__ = [
    "FusionOptInView",
    "build_fusion_opt_in_view",
    "register_persistent_fusion_views",
]
