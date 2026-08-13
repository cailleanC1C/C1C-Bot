"""Discord controls for Live Arena 6B-4 competition operations."""

from __future__ import annotations

import logging

import discord

from shared.theme import colors

from modules.community.live_arena.competition_operations import CompetitionOperationsService
from modules.community.live_arena.organizer_panel import OrganizerView
from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import _text
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.competition_operations_runtime")
_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import qualification_panel, result_views

    original_match_view_init = result_views.MatchResultView.__init__

    def match_view_init_with_scheduling(self, sheet_id: str, **kwargs):
        original_match_view_init(self, sheet_id, **kwargs)
        if not any(
            getattr(item, "custom_id", "") == "live_arena:match:report_scheduling_problem"
            for item in self.children
        ):
            self.add_item(ReportSchedulingProblemButton(sheet_id))

    result_views.MatchResultView.__init__ = match_view_init_with_scheduling

    original_install = qualification_panel.install_qualification

    def install_with_operations(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_competition_operations_installed", False):
            return True
        manager._competition_operations_installed = True
        base_view = manager.view

        def view(status=None):
            result = base_view(status)
            add_item = getattr(result, "add_item", None)
            if callable(add_item):
                add_item(
                    CompetitionOperationsButton(
                        manager,
                        disabled=status is not None and status != "active",
                    )
                )
            return result

        manager.view = view
        return True

    qualification_panel.install_qualification = install_with_operations


class ReportSchedulingProblemButton(discord.ui.Button):
    def __init__(self, sheet_id: str):
        super().__init__(
            label="Scheduling Problem",
            style=discord.ButtonStyle.secondary,
            custom_id="live_arena:match:report_scheduling_problem",
        )
        self.sheet_id = sheet_id

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            service = CompetitionOperationsService(self.sheet_id)
            await service.initialize()
            matches = await service.repository.matches()
            found = [
                row
                for row in matches
                if _text(row.get("thread_id")) == str(interaction.channel_id)
            ]
            if len(found) != 1:
                raise RegistrationError("This Duelling Deck thread could not be resolved uniquely")
            updated = await service.report_scheduling_problem(
                str(interaction.user.id), _text(found[0].get("match_id"))
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Scheduling problem recorded",
                    description=(
                        "The matchup remains valid. You and your opponent still have **24 hours** "
                        "to work out a time before it enters the organizer scheduling-review queue."
                    ),
                    color=colors.c1c_blue,
                ),
                ephemeral=True,
            )
            try:
                await interaction.channel.send(
                    embed=discord.Embed(
                        title="Scheduling assistance requested",
                        description=(
                            f"<@{interaction.user.id}> reported a scheduling problem. "
                            "The match is still active; organizer review becomes available after the 24-hour grace period."
                        ),
                        color=colors.c1c_blue,
                    ),
                    allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
                )
            except Exception:
                log.exception("Live Arena scheduling-problem thread notice failed")
        except Exception as exc:
            log.exception("Live Arena scheduling-problem report failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


class CompetitionOperationsButton(discord.ui.Button):
    def __init__(self, manager, *, disabled=False):
        super().__init__(
            label="Competition Ops",
            style=discord.ButtonStyle.secondary,
            custom_id="live_arena:organizer:competition_ops",
            disabled=disabled,
        )
        self.manager = manager

    async def callback(self, interaction: discord.Interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Organizer Actions",
                description=(
                    "Use these when a matchup needs staff intervention.\n\n"
                    "**Scheduling Queue** — see matches where players asked for scheduling help.\n"
                    "**Extend Round** — move the current round deadline.\n"
                    "**Mandatory Time** — set a required match time when players cannot agree.\n"
                    "**Resolve Scheduling Issue** — resolve a scheduling case with a forfeit or double forfeit.\n"
                    "**Withdraw Player** — remove a player who leaves after the tournament has started.\n\n"
                    "All rulings require a reason and are recorded in the tournament audit trail."
                ),
                color=colors.c1c_blue,
            ),
            view=CompetitionOperationsView(self.manager),
            ephemeral=True,
        )


class CompetitionOperationsView(discord.ui.View):
    def __init__(self, manager):
        super().__init__(timeout=600)
        self.manager = manager

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await OrganizerView(self.manager).authorized(interaction)

    @discord.ui.button(label="Scheduling Queue", style=discord.ButtonStyle.secondary)
    async def queue(self, interaction, _button):
        await interaction.response.defer(ephemeral=True)
        try:
            service = CompetitionOperationsService(self.manager.sheet_id)
            await service.initialize()
            rows = await service.scheduling_review_queue()
            lines = [
                (
                    f"**{_text(row.get('match_id'))}** · "
                    f"<@{_text(row.get('player_a_discord_user_id'))}> vs "
                    f"<@{_text(row.get('player_b_discord_user_id'))}>"
                )
                for row in rows
            ]
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Scheduling review queue",
                    description="\n".join(lines) if lines else "No scheduling cases are due for organizer review.",
                    color=colors.c1c_blue,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)

    @discord.ui.button(label="Extend Round", style=discord.ButtonStyle.primary)
    async def extend_round(self, interaction, _button):
        await interaction.response.send_modal(ExtendRoundModal(self.manager))

    @discord.ui.button(label="Mandatory Time", style=discord.ButtonStyle.primary)
    async def mandatory_time(self, interaction, _button):
        await interaction.response.send_modal(MandatoryTimeModal(self.manager))

    @discord.ui.button(label="Resolve Scheduling Issue", style=discord.ButtonStyle.danger)
    async def scheduling_ruling(self, interaction, _button):
        await interaction.response.send_modal(SchedulingRulingModal(self.manager))

    @discord.ui.button(label="Withdraw Player", style=discord.ButtonStyle.danger)
    async def withdraw_player(self, interaction, _button):
        await interaction.response.send_modal(WithdrawPlayerModal(self.manager))


class ExtendRoundModal(discord.ui.Modal, title="Extend Live Arena Round"):
    round_id = discord.ui.TextInput(label="Round ID", placeholder="LA-2026-TRIAL-01-Q2")
    deadline = discord.ui.TextInput(label="New deadline UTC", placeholder="2026-08-25T20:00:00Z")
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph)

    def __init__(self, manager):
        super().__init__(timeout=600)
        self.manager = manager

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            service = CompetitionOperationsService(self.manager.sheet_id)
            await service.initialize()
            row = await service.extend_round(
                str(interaction.user.id),
                str(self.round_id.value).strip(),
                str(self.deadline.value).strip(),
                reason=str(self.reason.value),
            )
            await _sync(self.manager)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Round extended",
                    description=f"New deadline: **{_text(row.get('deadline_at_utc'))}**",
                    color=colors.c1c_blue,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


class MandatoryTimeModal(discord.ui.Modal, title="Impose Mandatory Match Time"):
    match_id = discord.ui.TextInput(label="Match ID")
    time_utc = discord.ui.TextInput(label="Mandatory time UTC", placeholder="2026-08-24T18:00:00Z")
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph)

    def __init__(self, manager):
        super().__init__(timeout=600)
        self.manager = manager

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            service = CompetitionOperationsService(self.manager.sheet_id)
            await service.initialize()
            row = await service.impose_mandatory_time(
                str(interaction.user.id),
                str(self.match_id.value).strip(),
                str(self.time_utc.value).strip(),
                reason=str(self.reason.value),
            )
            await _sync(self.manager)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Mandatory match time imposed",
                    description=_text(row.get("scheduling_conflict_notes"))[-1500:],
                    color=colors.c1c_blue,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


class SchedulingRulingModal(discord.ui.Modal, title="Resolve Scheduling Case"):
    match_id = discord.ui.TextInput(label="Match ID")
    action = discord.ui.TextInput(label="Action", placeholder="forfeit_a / forfeit_b / double_forfeit")
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph)

    def __init__(self, manager):
        super().__init__(timeout=600)
        self.manager = manager

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            service = CompetitionOperationsService(self.manager.sheet_id)
            await service.initialize()
            row = await service.resolve_scheduling(
                str(interaction.user.id),
                str(self.match_id.value).strip(),
                str(self.action.value).strip().lower(),
                reason=str(self.reason.value),
            )
            await _sync(self.manager)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Scheduling issue resolved",
                    description=(
                        f"**{_text(row.get('match_id'))}** is now **{_text(row.get('status'))}**."
                    ),
                    color=colors.c1c_blue,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


class WithdrawPlayerModal(discord.ui.Modal, title="Withdraw Active Participant"):
    user_id = discord.ui.TextInput(label="Discord user ID")
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph)

    def __init__(self, manager):
        super().__init__(timeout=600)
        self.manager = manager

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            service = CompetitionOperationsService(self.manager.sheet_id)
            await service.initialize()
            row = await service.withdraw_active_participant(
                str(interaction.user.id),
                str(self.user_id.value).strip(),
                reason=str(self.reason.value),
            )
            await _sync(self.manager)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Participant withdrawn",
                    description=(
                        f"<@{_text(row.get('discord_user_id'))}> is now withdrawn. "
                        "Completed results were preserved; unresolved published matches were handled as withdrawal forfeits."
                    ),
                    color=colors.c1c_blue,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


async def _sync(manager) -> None:
    sync = getattr(manager, "_competition_sync", None)
    if callable(sync):
        try:
            await sync()
        except Exception:
            log.exception("Live Arena operations post-mutation sync failed")
    try:
        await manager.sync()
    except Exception:
        log.exception("Live Arena organizer panel refresh after operations mutation failed")
