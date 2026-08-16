"""Migration-safe Live Arena result lifecycle repairs.

Production registration preloads the result lifecycle copy, so the matchup starter
no longer owns dispute actions. This final installer keeps the narrow legacy
fallback for pre-registration construction and also repairs two migration paths
that must be safe for tournaments already in progress:

* finalized pre-feature matches may have old result messages with no lifecycle
  footer and must reconcile without trying to render placeholder-bearing titles;
* Captain's Table lifecycle action lookup must use the tournament snapshot ID,
  not assume QualificationRepository.config contains ACTIVE_TOURNAMENT_ID.
"""

from __future__ import annotations

import logging

import discord

from modules.community.live_arena.competition import MATCH_TERMINAL_STATUSES
from modules.community.live_arena.competition_resolution import CompetitionResolutionService
from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import _text
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.result_lifecycle_fallback")
_installed = False


async def _reconcile_lifecycle_message_migration_safe(
    result_lifecycle_ux,
    bot,
    sheet_id: str,
    thread,
    match,
) -> None:
    """Reconcile old and new result messages without requiring new-message metadata."""
    message = await result_lifecycle_ux._find_lifecycle_message(
        thread,
        bot,
        _text(match.get("match_id")),
    )
    if message is None:
        # A match finalized before lifecycle messages existed is valid historical
        # state. There is nothing to repair and startup must not warn/retry.
        return

    status = _text(match.get("status"))
    if status == "pending_confirmation":
        await message.edit(view=result_lifecycle_ux.ResultDecisionView(sheet_id))
        return

    current_title = ""
    if getattr(message, "embeds", None):
        current_title = _text(getattr(message.embeds[0], "title", ""))

    # Read the configured title literally. Calling _title() here is invalid for
    # templates whose *description* has placeholders such as participant_mention
    # and score; that was the production startup regression for finalized Q2 rows.
    final_titles = {
        result_lifecycle_ux._template("result_confirmed_player", sheet_id).title,
        result_lifecycle_ux._template("result_confirmed_staff", sheet_id).title,
        result_lifecycle_ux._template("result_finalized_expired", sheet_id).title,
        result_lifecycle_ux._template("result_finalized_organizer", sheet_id).title,
    }

    if status == "disputed":
        if current_title.startswith("Result reported"):
            embed = result_lifecycle_ux._template(
                "result_disputed_player", sheet_id
            ).embed(
                participant_mention=(
                    f"<@{_text(match.get('disputed_by_discord_user_id'))}>"
                ),
                score=result_lifecycle_ux._score_for_reporter(match),
            )
            embed.set_footer(
                text=(
                    f"{result_lifecycle_ux._RESULT_MARKER_PREFIX}"
                    f"{_text(match.get('match_id'))}"
                )
            )
            await message.edit(embed=embed, view=None)
        else:
            await message.edit(view=None)
        return

    if status == "finalized" and current_title not in final_titles:
        if _text(match.get("confirmed_by_discord_user_id")):
            embed = result_lifecycle_ux._template(
                "result_confirmed_player", sheet_id
            ).embed(
                participant_mention=(
                    f"<@{_text(match.get('confirmed_by_discord_user_id'))}>"
                ),
                score=result_lifecycle_ux._score_for_reporter(match, final=True),
            )
        elif _text(match.get("finalized_by_discord_user_id")) == "system":
            embed = result_lifecycle_ux._template(
                "result_finalized_expired", sheet_id
            ).embed(
                score=result_lifecycle_ux._score_for_reporter(match, final=True)
            )
        else:
            embed = result_lifecycle_ux._template(
                "result_finalized_organizer", sheet_id
            ).embed(
                score=result_lifecycle_ux._score_for_reporter(match, final=True)
            )
        embed.set_footer(
            text=(
                f"{result_lifecycle_ux._RESULT_MARKER_PREFIX}"
                f"{_text(match.get('match_id'))}"
            )
        )
        await message.edit(embed=embed, view=None)
        return

    # Forfeit/bye/double-forfeit and already-updated lifecycle messages only need
    # stale controls removed. They are terminal without requiring a new message.
    await message.edit(view=None)


async def _allowed_panel_actions_snapshot_safe(manager, simulation_ux_finalizer):
    """Mirror lifecycle actions using the authoritative tournament snapshot ID."""
    from modules.community.live_arena.qualification import QualificationService

    tournament = await simulation_ux_finalizer.load_tournament_snapshot(manager.sheet_id)
    status = _text(tournament.status)
    maintenance = {"Repair Discord State", "Player History"}
    roster = {"View Roster", "Reconcile Roles"}

    if status == "draft":
        return {"Open Registration"} | roster | maintenance
    if status == "signup_open":
        return {"Close Registration"} | roster | maintenance
    if status == "completed":
        return {
            "Archive Tournament",
            "Create Next Tournament",
            "View Standings",
        } | maintenance
    if status == "archived":
        return {"Create Next Tournament"} | maintenance

    service = QualificationService(manager.sheet_id)
    await service.initialize()
    tid = _text(getattr(tournament, "tournament_id", ""))
    if not tid:
        raise RegistrationError("Active tournament ID could not be resolved")
    rounds = [
        row
        for row in await service.repository.rounds()
        if _text(row.get("tournament_id")) == tid
    ]

    if status == "signup_closed":
        q1 = simulation_ux_finalizer._qualification_round(rounds, 1)
        if q1 is None:
            return {"Reopen Registration", "Generate Q1 Draw"} | roster | maintenance
        if _text(q1.get("status")) in {"proposed", "preview"}:
            return {
                "Reopen Registration",
                "Approve Draw",
                "Regenerate Draw",
                "Swap Players",
            } | roster | maintenance
        return {"Reopen Registration"} | roster | maintenance

    if status != "active":
        return roster | maintenance

    open_rounds = [
        row
        for row in rounds
        if _text(row.get("status"))
        in {
            "active",
            "published",
            "open",
            "published/open",
            "ready_to_close",
            "correction_in_progress",
        }
    ]
    if open_rounds:
        current = max(open_rounds, key=simulation_ux_finalizer._round_sort_key)
        actions = {
            "Close Current Round",
            "Review Result Issues",
            "Competition Ops",
        } | roster | maintenance
        if _text(current.get("round_stage")).lower() == "qualification":
            actions.add("View Standings")
        return actions

    previews = [
        row
        for row in rounds
        if _text(row.get("status")) in {"preview", "approved", "proposed"}
    ]
    if previews:
        current = max(previews, key=simulation_ux_finalizer._round_sort_key)
        stage = _text(current.get("round_stage")).lower()
        if stage == "qualification":
            return {
                "View Standings",
                "Preview Next Swiss",
                "Regenerate Swiss Preview",
                "Approve & Publish Swiss",
                "Repair Swiss Conflict",
                "Reopen Closed Round",
            } | roster | maintenance
        return {
            "Approve & Open Knockout",
            "Record BO3 Tiebreak",
            "View Standings",
        } | roster | maintenance

    final = simulation_ux_finalizer._stage_round(rounds, "final")
    if final is not None and _text(final.get("status")) == "closed":
        return {"Complete Tournament", "View Standings"} | roster | maintenance

    knockout_exists = any(
        _text(row.get("round_stage")).lower()
        in {"quarterfinal", "semifinal", "final"}
        for row in rounds
    )
    if knockout_exists:
        return {"View Standings", "Reopen Closed Round"} | roster | maintenance

    closed_q = {
        int(_text(row.get("round_number")) or 0)
        for row in rounds
        if _text(row.get("round_stage")).lower() == "qualification"
        and _text(row.get("status")) == "closed"
    }
    if 3 in closed_q:
        return {
            "View Standings",
            "Freeze Top 8",
            "Record BO3 Tiebreak",
            "Reopen Closed Round",
        } | roster | maintenance
    if closed_q & {1, 2}:
        return {
            "View Standings",
            "Preview Next Swiss",
            "Regenerate Swiss Preview",
            "Approve & Publish Swiss",
            "Repair Swiss Conflict",
            "Reopen Closed Round",
        } | roster | maintenance
    return roster | maintenance


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import (
        result_lifecycle_ux,
        result_views,
        simulation_ux_finalizer,
        simulation_ux_hardening,
    )

    # Existing narrow fallback: before lifecycle copy is loaded, keep the legacy
    # starter dispute action functional. Production removes it once copy is ready.
    original_init = result_views.MatchResultView.__init__

    def init_with_preload_fallback(self, sheet_id: str, **kwargs):
        original_init(self, sheet_id, **kwargs)
        if result_lifecycle_ux._templates(str(sheet_id)) is not None:
            return
        if any(
            getattr(item, "custom_id", "") == "live_arena:match:dispute_result"
            for item in self.children
        ):
            return

        button = discord.ui.Button(
            label="Dispute Result",
            style=discord.ButtonStyle.danger,
            custom_id="live_arena:match:dispute_result",
            disabled=bool(kwargs.get("dispute_disabled", False)),
        )

        async def callback(interaction: discord.Interaction):
            await interaction.response.defer(ephemeral=True)
            try:
                service = CompetitionResolutionService(str(sheet_id))
                await service.initialize()
                match = await service.match_for_thread(str(interaction.channel_id))
                updated = await service.dispute_result(
                    str(interaction.user.id), _text(match["match_id"])
                )
                result_views.cancel_match_finalizer(
                    str(sheet_id), _text(updated["match_id"])
                )
                await result_views._run_post_mutation_sync(str(sheet_id))
            except RegistrationError as exc:
                await interaction.followup.send(embed=error_embed(exc), ephemeral=True)
            except Exception as exc:
                log.exception("Live Arena legacy dispute fallback failed")
                await interaction.followup.send(embed=error_embed(exc), ephemeral=True)

        button.callback = callback
        self.add_item(button)

    result_views.MatchResultView.__init__ = init_with_preload_fallback

    # Hotfix the real runtime global used by _reconcile_current_round. This keeps
    # already-finalized Q1/Q2 rows migration-safe on redeploy.
    async def reconcile_lifecycle_message(bot, sheet_id, thread, match):
        return await _reconcile_lifecycle_message_migration_safe(
            result_lifecycle_ux,
            bot,
            sheet_id,
            thread,
            match,
        )

    result_lifecycle_ux._reconcile_lifecycle_message = reconcile_lifecycle_message

    # Captain's Table final render calls simulation_ux_finalizer directly, while
    # some older wrappers still hold the simulation_ux_hardening alias. Patch both
    # so neither path assumes repository.config contains ACTIVE_TOURNAMENT_ID.
    async def allowed_panel_actions(manager):
        return await _allowed_panel_actions_snapshot_safe(
            manager,
            simulation_ux_finalizer,
        )

    simulation_ux_finalizer._allowed_panel_actions = allowed_panel_actions
    simulation_ux_hardening._allowed_panel_actions = allowed_panel_actions
