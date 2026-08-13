"""Organizer-friendly scheduling resolution flow for Live Arena.

The competition service keeps its stable ``forfeit_a`` / ``forfeit_b`` action
contract, but organizers never need to see or type those implementation codes.
"""

from __future__ import annotations

import logging

import discord

from shared.theme import colors

from modules.community.live_arena.competition_operations import CompetitionOperationsService
from modules.community.live_arena.organizer_panel import OrganizerView
from modules.community.live_arena.service import _text
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.scheduling_resolution_ux")
_installed = False


def _clip(value: str, limit: int) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _player_name(match: dict[str, object], side: str) -> str:
    name = _text(match.get(f"player_{side}_display_name"))
    if name:
        return name
    user_id = _text(match.get(f"player_{side}_discord_user_id"))
    return f"Player {1 if side == 'a' else 2}" if not user_id else f"Player {1 if side == 'a' else 2} ({user_id})"


def _match_label(match: dict[str, object]) -> str:
    number = _text(match.get("match_number"))
    prefix = f"M{number} · " if number else ""
    return _clip(
        f"{prefix}{_player_name(match, 'a')} vs {_player_name(match, 'b')}",
        100,
    )


def _outcome_text(match: dict[str, object], action: str) -> str:
    if action == "forfeit_a":
        return f"{_player_name(match, 'a')} forfeits"
    if action == "forfeit_b":
        return f"{_player_name(match, 'b')} forfeits"
    return "Both players forfeit"


def install() -> None:
    """Replace only the organizer scheduling-ruling entry point with a guided flow."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import competition_operations_runtime as runtime

    async def guided_operations_callback(self, interaction: discord.Interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        view = runtime.CompetitionOperationsView(self.manager)
        for child in list(view.children):
            if getattr(child, "label", None) == "Resolve Scheduling Issue":
                view.remove_item(child)
        view.add_item(GuidedResolveSchedulingButton(self.manager))
        await interaction.response.send_message(
            embed=discord.Embed(
                title="Organizer Actions",
                description=(
                    "Use these when a matchup needs staff intervention.\n\n"
                    "**Scheduling Queue** — see matches where players asked for scheduling help.\n"
                    "**Extend Round** — move the current round deadline.\n"
                    "**Mandatory Time** — set a required match time when players cannot agree.\n"
                    "**Resolve Scheduling Issue** — choose the affected match, then choose exactly who forfeits.\n"
                    "**Withdraw Player** — remove a player who leaves after the tournament has started.\n\n"
                    "All rulings require a reason and are recorded in the tournament audit trail."
                ),
                color=colors.c1c_blue,
            ),
            view=view,
            ephemeral=True,
        )

    runtime.CompetitionOperationsButton.callback = guided_operations_callback


class GuidedResolveSchedulingButton(discord.ui.Button):
    def __init__(self, manager):
        super().__init__(
            label="Resolve Scheduling Issue",
            style=discord.ButtonStyle.danger,
            custom_id="live_arena:organizer:scheduling_resolution:guided",
        )
        self.manager = manager

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            service = CompetitionOperationsService(self.manager.sheet_id)
            await service.initialize()
            matches = await service.scheduling_review_queue()
            if not matches:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="No scheduling cases ready",
                        description=(
                            "There are currently no scheduling cases that are ready for an organizer ruling. "
                            "A reported scheduling problem enters this queue after its 24-hour grace period."
                        ),
                        color=colors.c1c_blue,
                    ),
                    ephemeral=True,
                )
                return

            await interaction.followup.send(
                embed=discord.Embed(
                    title="Resolve Scheduling Issue",
                    description=(
                        "**Step 1 of 2:** Choose the matchup that needs a ruling. "
                        "You will choose the actual player outcome on the next screen."
                    ),
                    color=colors.c1c_blue,
                ),
                view=SchedulingMatchPickerView(self.manager, matches),
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Live Arena guided scheduling match picker failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


class SchedulingMatchPicker(discord.ui.Select):
    def __init__(self, manager, matches: list[dict[str, object]]):
        self.manager = manager
        self.matches = {_text(row.get("match_id")): dict(row) for row in matches}
        options = [
            discord.SelectOption(
                label=_match_label(row),
                value=_text(row.get("match_id")),
                description=_clip(_text(row.get("match_id")), 100),
            )
            for row in matches[:25]
            if _text(row.get("match_id"))
        ]
        super().__init__(
            placeholder="Choose the matchup",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        match = self.matches.get(self.values[0])
        if match is None:
            await interaction.response.send_message(
                embed=discord.Embed(
                    title="Match unavailable",
                    description="That scheduling case is no longer available. Reopen Organizer Actions and try again.",
                    color=colors.c1c_blue,
                ),
                ephemeral=True,
            )
            return
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="Choose Scheduling Outcome",
                description=(
                    f"**{_match_label(match)}**\n\n"
                    "**Step 2 of 2:** Choose the player who forfeits, or choose both players. "
                    "You will then be asked for the required reason."
                ),
                color=colors.c1c_blue,
            ),
            view=SchedulingOutcomeView(self.manager, match),
        )


class SchedulingMatchPickerView(discord.ui.View):
    def __init__(self, manager, matches: list[dict[str, object]]):
        super().__init__(timeout=600)
        self.manager = manager
        self.add_item(SchedulingMatchPicker(manager, matches))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await OrganizerView(self.manager).authorized(interaction)


class SchedulingOutcomeButton(discord.ui.Button):
    def __init__(self, manager, match: dict[str, object], action: str):
        style = discord.ButtonStyle.danger
        super().__init__(
            label=_clip(_outcome_text(match, action), 80),
            style=style,
            custom_id=f"live_arena:organizer:scheduling_resolution:{action}",
        )
        self.manager = manager
        self.match = dict(match)
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(
            SchedulingResolutionReasonModal(self.manager, self.match, self.action)
        )


class SchedulingOutcomeView(discord.ui.View):
    def __init__(self, manager, match: dict[str, object]):
        super().__init__(timeout=600)
        self.manager = manager
        for action in ("forfeit_a", "forfeit_b", "double_forfeit"):
            self.add_item(SchedulingOutcomeButton(manager, match, action))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return await OrganizerView(self.manager).authorized(interaction)


class SchedulingResolutionReasonModal(discord.ui.Modal, title="Resolve Scheduling Issue"):
    reason = discord.ui.TextInput(
        label="Reason",
        placeholder="Explain why this scheduling ruling is being made",
        style=discord.TextStyle.paragraph,
        max_length=1000,
    )

    def __init__(self, manager, match: dict[str, object], action: str):
        super().__init__(timeout=600)
        self.manager = manager
        self.match = dict(match)
        self.action = action

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            service = CompetitionOperationsService(self.manager.sheet_id)
            await service.initialize()
            row = await service.resolve_scheduling(
                str(interaction.user.id),
                _text(self.match.get("match_id")),
                self.action,
                reason=str(self.reason.value).strip(),
            )
            await _sync(self.manager)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Scheduling issue resolved",
                    description=(
                        f"**{_match_label(self.match)}**\n"
                        f"Outcome: **{_outcome_text(self.match, self.action)}**\n"
                        f"Match status: **{_text(row.get('status'))}**"
                    ),
                    color=colors.c1c_blue,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Live Arena guided scheduling ruling failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


async def _sync(manager) -> None:
    sync = getattr(manager, "_competition_sync", None)
    if callable(sync):
        try:
            await sync()
        except Exception:
            log.exception("Live Arena guided scheduling post-mutation sync failed")
    try:
        await manager.sync()
    except Exception:
        log.exception("Live Arena organizer panel refresh after guided scheduling ruling failed")
