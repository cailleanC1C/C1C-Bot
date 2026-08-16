"""Final Live Arena progression boundary after qualification.

Closed rounds are historical state. They still need one canonical Victory Ledger
refresh, but they must not keep mutating old matchup threads on every startup.
That retry storm consumed Discord work and, more importantly, enough Google Sheets
reads to make the post-Top-8 Captain's Table refresh hit the per-user quota.

This layer is installed last so the same authoritative control-center state drives
both the status copy and the visible progression action. It also gives Top-8 and
knockout approval writes fresh Sheet read scopes before the final panel refresh.
"""

from __future__ import annotations

import logging

import discord

from shared.sheets.async_core import sheet_read_scope
from shared.theme import colors

from modules.community.live_arena.service import _text
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.knockout_transition_repair")
_installed = False

_KNOCKOUT_ORDER = ("final", "semifinal", "quarterfinal")
_PREVIEW_STATUSES = {"preview", "approved", "proposed"}
_OPEN_STATUSES = {"active", "published", "open", "published/open"}
_CLOSABLE_STATUSES = {"ready_to_close", "correction_in_progress"}


def _round_stage(row) -> str:
    return _text(row.get("round_stage")).lower()


def _round_status(row) -> str:
    return _text(row.get("status")).lower()


def _latest_knockout_round(state):
    for stage in _KNOCKOUT_ORDER:
        found = [
            row
            for row in state.rounds
            if _text(row.get("tournament_id")) == state.tournament_id
            and _round_stage(row) == stage
        ]
        if found:
            return found[0]
    return None


def _q3_closed(state) -> bool:
    from modules.community.live_arena import knockout

    q3 = knockout._round_by_id(
        state.rounds, state.tournament_id, f"{state.tournament_id}-Q3"
    )
    return q3 is not None and _round_status(q3) == "closed"


def _apply_progression_state(manager, state) -> None:
    """Make the final Captain's Table controls agree with its rendered state."""
    if not _q3_closed(state):
        return

    from modules.community.live_arena import captains_table_quota_safe, knockout

    allowed = set(getattr(manager, "_captains_table_allowed", None) or ())
    if not allowed:
        allowed = set(captains_table_quota_safe._safe_panel_actions(manager, "active"))

    # These actions are mutually exclusive at the final progression boundary.
    allowed.difference_update(
        {
            "Close Current Round",
            "Freeze Top 8",
            "Record BO3 Tiebreak",
            "Approve & Open Knockout",
        }
    )

    seed_row = knockout._seed_row(state.rounds, state.tournament_id)
    if seed_row is None:
        if state.unsupported_tie:
            manager._captains_table_allowed = allowed
            return
        if state.tiebreak_required and not state.tiebreak_complete:
            allowed.add("Record BO3 Tiebreak")
        else:
            allowed.add("Freeze Top 8")
        manager._captains_table_allowed = allowed
        return

    current = _latest_knockout_round(state)
    if current is None:
        manager._captains_table_allowed = allowed
        return

    status = _round_status(current)
    if status in _PREVIEW_STATUSES:
        allowed.add("Approve & Open Knockout")
    elif status in _CLOSABLE_STATUSES:
        allowed.add("Close Current Round")
    # An open round intentionally has no Finish Round button until it is actually
    # ready to close. A closed stage waits for normal next-preview reconciliation.

    manager._captains_table_allowed = allowed


def _stage_summary_with_preview(original, state):
    current = _latest_knockout_round(state)
    if current is not None and _round_status(current) in _PREVIEW_STATUSES:
        stage = _round_stage(current)
        labels = {
            "quarterfinal": ("Quarterfinals", "Semifinals"),
            "semifinal": ("Semifinals", "Final"),
            "final": ("Final", "Tournament complete"),
        }
        label, next_step = labels[stage]
        return (
            label,
            f"Review the {label.lower()} matchups and start the round.",
            next_step,
        )
    return original(state)


async def _sync_closed_round(bot, qualification_service, snapshot) -> list[str]:
    """Reconcile historical rounds without touching their old matchup threads."""
    from modules.community.live_arena import victory_ledger_final_refresh as ledger

    round_row = getattr(snapshot, "round_row", None)
    if round_row is None:
        return []

    warnings: list[str] = []
    try:
        await ledger._force_overview_refresh(bot, qualification_service, snapshot)
    except Exception as exc:
        log.exception(
            "Live Arena closed-round Victory Ledger refresh failed • round=%s • error=%s: %s",
            _text(round_row.get("round_id")),
            type(exc).__name__,
            exc,
        )
        warnings.append("Victory Ledger overview")

    try:
        await ledger._sync_round_ready_alert(
            bot,
            qualification_service.sheet_id,
            round_row,
            [dict(row) for row in snapshot.matches],
        )
    except Exception as exc:
        log.exception(
            "Live Arena closed-round alert cleanup failed • round=%s • error=%s: %s",
            _text(round_row.get("round_id")),
            type(exc).__name__,
            exc,
        )
        warnings.append("round closure alert")

    return list(dict.fromkeys(warnings))


async def _freeze_top8_callback(self, interaction: discord.Interaction) -> None:
    """Persist Top 8, create/recover QF preview, then render from fresh state."""
    from modules.community.live_arena.knockout import KnockoutService
    from modules.community.live_arena.organizer_panel import OrganizerView
    from modules.community.live_arena import knockout_runtime

    if not await OrganizerView(self.manager).authorized(interaction):
        return
    await interaction.response.defer(ephemeral=True)

    try:
        with sheet_read_scope():
            service = KnockoutService(self.manager.sheet_id)
            await service.initialize()
            seeds = await service.freeze_top8(str(interaction.user.id))

        # A write ends the authority of the preceding read scope. Use a new service
        # and scope so an already-created QF preview is recovered rather than hidden
        # by the pre-freeze/pre-preview cache.
        with sheet_read_scope():
            service = KnockoutService(self.manager.sheet_id)
            await service.initialize()
            preview = await service.snapshot("quarterfinal")
            if preview.round_row is None:
                preview = await service.generate_quarterfinal_preview(
                    str(interaction.user.id)
                )
            await knockout_runtime._sync_preview_message(self.manager, service, preview)
    except Exception as exc:
        log.exception(
            "Live Arena Top 8 persistence/preview failed • error=%s: %s",
            type(exc).__name__,
            exc,
        )
        await interaction.followup.send(embed=error_embed(exc), ephemeral=True)
        return

    panel_warning = ""
    try:
        # The final visible state must be loaded after both writes above. Keeping it
        # in its own scope also prevents stale pre-lock rows from surviving into the
        # Captain's Table control-center render.
        with sheet_read_scope():
            await self.manager.sync()
    except Exception as exc:
        log.exception(
            "Live Arena Captain's Table refresh after Top 8 lock failed • error=%s: %s",
            type(exc).__name__,
            exc,
        )
        panel_warning = (
            "\n\n⚠️ Top 8 and the Quarterfinal preview are saved, but Captain's "
            "Table could not refresh. Use Repair Tournament after the transient error clears."
        )

    lines = [f"**#{seed['seed']}** <@{seed['discord_user_id']}>" for seed in seeds]
    await interaction.followup.send(
        embed=discord.Embed(
            title="Top 8 locked",
            description=(
                "Qualification order is now immutable. The Quarterfinal organizer "
                "preview is ready for review.\n\n"
                + "\n".join(lines)
                + panel_warning
            ),
            color=colors.c1c_blue,
        ),
        ephemeral=True,
    )


def install() -> None:
    """Install after every existing Live Arena render/reconciliation decorator."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import (
        captains_table_action_state,
        captains_table_control_center as control,
        knockout_runtime,
        matchup_thread_ux,
        result_lifecycle_ux,
        runtime_hooks,
    )

    # 1) Closed rounds are historical. Keep their canonical ledger/alert state but
    # skip the stacked result-control, instruction, and lifecycle thread mutations.
    original_sync_round = runtime_hooks._sync_round_discord

    async def sync_round_quiescent(bot, qualification_service, snapshot):
        if _text(getattr(snapshot, "status", "")).lower() == "closed":
            return await _sync_closed_round(bot, qualification_service, snapshot)
        return await original_sync_round(bot, qualification_service, snapshot)

    runtime_hooks._sync_round_discord = sync_round_quiescent

    original_rerender = matchup_thread_ux._rerender_open_match_threads

    async def rerender_live_threads_only(bot, qualification_service, snapshot):
        if _text(getattr(snapshot, "status", "")).lower() == "closed":
            return []
        return await original_rerender(bot, qualification_service, snapshot)

    matchup_thread_ux._rerender_open_match_threads = rerender_live_threads_only

    original_lifecycle = result_lifecycle_ux._reconcile_current_round

    async def reconcile_live_lifecycle_only(bot, qualification_service, snapshot):
        if _text(getattr(snapshot, "status", "")).lower() == "closed":
            return []
        return await original_lifecycle(bot, qualification_service, snapshot)

    result_lifecycle_ux._reconcile_current_round = reconcile_live_lifecycle_only

    # 2) The exact ControlState used for the embed also owns the progression
    # controls. A frozen Top 8 + knockout preview therefore exposes approval even
    # if an earlier quota-safe lookup cached the pre-lock action set.
    captains_table_action_state._apply_final_action_state = _apply_progression_state
    original_stage_summary = control._stage_summary
    control._stage_summary = lambda state: _stage_summary_with_preview(
        original_stage_summary, state
    )

    # 3) Future Top-8 locks cross writes with fresh Sheet read scopes and report
    # panel-refresh failure as a sync warning rather than pretending the lock failed.
    knockout_runtime.FreezeTop8Button.callback = _freeze_top8_callback
