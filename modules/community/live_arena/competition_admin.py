"""Organizer Discord controls for Live Arena result review and round corrections."""

from __future__ import annotations

import logging

import discord

from shared.theme import colors

from modules.community.live_arena.competition_resolution import CompetitionResolutionService
from modules.community.live_arena.organizer_panel import OrganizerView
from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import _text, load_config
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.competition_admin")


class ReviewResultIssuesButton(discord.ui.Button):
    def __init__(self, manager, *, disabled=False):
        super().__init__(
            label="Review Result Issues",
            custom_id="live_arena:organizer:results:review",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self.manager = manager

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            service = CompetitionResolutionService(self.manager.sheet_id)
            await service.initialize()
            rows = await service.reviewable_matches()
            if not rows:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="No result issues",
                        description="There are no disputed, late-review, or correction-round results waiting for organizer action.",
                        color=colors.c1c_blue,
                    ),
                    ephemeral=True,
                )
                return
            view = ResultIssueSelectView(self.manager, rows)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Review result issues",
                    description="Choose the matchup that needs an organizer ruling.",
                    color=colors.c1c_blue,
                ),
                view=view,
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Live Arena organizer result-review list failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


class ResultIssueSelectView(discord.ui.View):
    def __init__(self, manager, rows):
        super().__init__(timeout=600)
        self.manager = manager
        self.add_item(ResultIssueSelect(manager, rows))


class ResultIssueSelect(discord.ui.Select):
    def __init__(self, manager, rows):
        options = []
        self.rows = {str(row["match_id"]): row for row in rows}
        for row in rows[:25]:
            label = (
                f"M{_text(row['match_number'])} · "
                f"{_text(row['player_a_display_name'])} vs {_text(row['player_b_display_name'])}"
            )[:100]
            options.append(
                discord.SelectOption(
                    label=label,
                    value=_text(row["match_id"]),
                    description=(f"Status: {_text(row['status'])}")[:100],
                )
            )
        super().__init__(
            placeholder="Choose a matchup",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.manager = manager

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        match = self.rows[self.values[0]]
        reported = "Not currently reported"
        if _text(match.get("reported_score_a")) and _text(match.get("reported_score_b")):
            reported = (
                f"{_text(match['reported_score_a'])}-{_text(match['reported_score_b'])} "
                f"by <@{_text(match['reported_by_discord_user_id'])}>"
            )
        embed = discord.Embed(
            title=(
                f"Match {_text(match['match_number'])} · "
                f"{_text(match['player_a_display_name'])} vs {_text(match['player_b_display_name'])}"
            ),
            description=(
                f"**Status:** `{_text(match['status'])}`\n"
                f"**Reported result:** {reported}\n"
                f"**Thread:** <#{_text(match['thread_id'])}>"
            ),
            color=colors.c1c_blue,
        )
        await interaction.response.edit_message(
            embed=embed,
            view=ResultRulingView(self.manager, _text(match["match_id"])),
        )


class ResultRulingView(discord.ui.View):
    def __init__(self, manager, match_id):
        super().__init__(timeout=600)
        self.manager = manager
        self.match_id = match_id
        actions = (
            ("Accept Report", "accept", discord.ButtonStyle.success),
            ("Correct Score", "correct", discord.ButtonStyle.primary),
            ("Order Replay", "replay", discord.ButtonStyle.secondary),
            ("Player A Forfeits", "forfeit_a", discord.ButtonStyle.danger),
            ("Player B Forfeits", "forfeit_b", discord.ButtonStyle.danger),
            ("Double Forfeit", "double_forfeit", discord.ButtonStyle.danger),
        )
        for label, action, style in actions:
            self.add_item(ResultRulingButton(manager, match_id, label, action, style))


class ResultRulingButton(discord.ui.Button):
    def __init__(self, manager, match_id, label, action, style):
        super().__init__(
            label=label,
            custom_id=f"live_arena:organizer:results:{action}",
            style=style,
        )
        self.manager = manager
        self.match_id = match_id
        self.action = action

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.send_modal(
            ResultRulingModal(self.manager, self.match_id, self.action)
        )


class ResultRulingModal(discord.ui.Modal):
    reason = discord.ui.TextInput(
        label="Organizer ruling reason",
        style=discord.TextStyle.paragraph,
        min_length=3,
        max_length=500,
    )

    def __init__(self, manager, match_id, action):
        title = {
            "accept": "Accept Reported Result",
            "correct": "Correct Match Result",
            "replay": "Order Match Replay",
            "forfeit_a": "Player A Forfeit",
            "forfeit_b": "Player B Forfeit",
            "double_forfeit": "Double Forfeit",
        }[action]
        super().__init__(title=title, timeout=600)
        self.manager = manager
        self.match_id = match_id
        self.action = action
        self.score = None
        if action == "correct":
            self.score = discord.ui.TextInput(
                label="Correct final score",
                placeholder="2-0 or 2-1",
                min_length=3,
                max_length=5,
            )
            self.add_item(self.score)

    async def on_submit(self, interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            service = CompetitionResolutionService(self.manager.sheet_id)
            await service.initialize()
            score_a = score_b = None
            if self.score is not None:
                parts = str(self.score.value).strip().replace("–", "-").replace("—", "-").split("-")
                if len(parts) != 2:
                    raise RegistrationError("Enter the corrected score like 2-0 or 2-1")
                try:
                    score_a, score_b = int(parts[0]), int(parts[1])
                except ValueError as exc:
                    raise RegistrationError("Enter the corrected score like 2-0 or 2-1") from exc
            updated = await service.resolve_match(
                str(interaction.user.id),
                self.match_id,
                self.action,
                reason=str(self.reason.value),
                score_a=score_a,
                score_b=score_b,
            )
            await _post_ruling_notice(self.manager, updated, self.action, str(self.reason.value))
            await _sync_after_mutation(self.manager)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Result ruling saved",
                    description=(
                        f"Match **{_text(updated['match_number'])}** is now "
                        f"`{_text(updated['status'])}`."
                    ),
                    color=colors.c1c_blue,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Live Arena organizer result ruling failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


class ReopenClosedRoundButton(discord.ui.Button):
    def __init__(self, manager, *, disabled=False):
        super().__init__(
            label="Reopen Closed Round",
            custom_id="live_arena:organizer:round:reopen",
            style=discord.ButtonStyle.secondary,
            disabled=disabled,
        )
        self.manager = manager

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            service = CompetitionResolutionService(self.manager.sheet_id)
            await service.initialize()
            config = await load_config(self.manager.sheet_id)
            tid = config["ACTIVE_TOURNAMENT_ID"]
            rounds = [
                row
                for row in await service.repository.rounds()
                if _text(row.get("tournament_id")) == tid
                and _text(row.get("status")) == "closed"
            ]
            if not rounds:
                raise RegistrationError("There is no closed round available to reopen")
            target = max(rounds, key=lambda row: int(_text(row.get("round_number")) or 0))
            reopened = await service.reopen_round(
                str(interaction.user.id), _text(target["round_id"])
            )
            await _sync_after_mutation(self.manager)
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Round reopened for correction",
                    description=(
                        f"**{_text(reopened['round_name'])}** is now visibly in correction. "
                        "The next round cannot be treated as official until this round is closed again."
                    ),
                    color=colors.c1c_blue,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Live Arena round reopen failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


async def _sync_after_mutation(manager) -> None:
    sync = getattr(manager, "_competition_sync", None)
    if callable(sync):
        try:
            await sync()
        except Exception:
            log.exception("Live Arena competition Discord resync failed")
    try:
        await manager.sync()
    except Exception:
        log.exception("Live Arena organizer panel resync failed")


async def _post_ruling_notice(manager, match, action: str, reason: str) -> None:
    thread_id = _text(match.get("thread_id"))
    if not thread_id:
        return
    try:
        thread = manager.bot.get_channel(int(thread_id))
        if thread is None:
            thread = await manager.bot.fetch_channel(int(thread_id))
        label = {
            "accept": "reported result accepted",
            "correct": "result corrected",
            "replay": "replay ordered",
            "forfeit_a": "Player A forfeited",
            "forfeit_b": "Player B forfeited",
            "double_forfeit": "double forfeit applied",
        }[action]
        await thread.send(
            f"⚖️ Organizer ruling: **{label}**. Reason: {reason}",
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception:
        log.exception("Live Arena organizer ruling thread notice failed")
