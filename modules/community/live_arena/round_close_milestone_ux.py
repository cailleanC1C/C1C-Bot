"""Final round-close milestone UX and progression reconciliation.

A successful round close is a write boundary. Anything that decides what comes
next must read the Sheet again after that write, otherwise an outer read cache can
keep seeing the just-closed round as ``ready_to_close`` and the organizer UI gets
stuck on the previous stage.

The progression path here is deliberately stage-generic: Q1 -> Q2, Q2 -> Q3,
Q3 -> Top 8 controls, Quarterfinal -> Semifinal, Semifinal -> Final, and Final ->
Finish Tournament all converge on the same close/reconcile/render sequence.
"""

from __future__ import annotations

import logging
import re

import discord

from shared.sheets.async_core import sheet_read_scope
from shared.theme import colors

from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import _text, load_config
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.round_close_milestone_ux")
_installed = False
_FLOAT_RE = re.compile(
    r"(?P<name>.+?) \((?P<src>\d+-\d+)\) floated (?P<direction>down|up) to (?P<dst>\d+-\d+)"
)
_NEXT_KNOCKOUT_STAGE = {
    "quarterfinal": "semifinal",
    "semifinal": "final",
}


def _record_strength(record: str) -> tuple[int, int]:
    wins_text, losses_text = str(record).split("-", 1)
    return int(wins_text), -int(losses_text)


def normalize_float_rationale(rationale: str) -> str:
    """Make Swiss float direction agree with the two records being described."""
    text = str(rationale or "")
    match = _FLOAT_RE.search(text)
    if match is None:
        return text
    try:
        src = _record_strength(match.group("src"))
        dst = _record_strength(match.group("dst"))
    except (TypeError, ValueError):
        return text
    if src == dst:
        return text
    wanted = "down" if src > dst else "up"
    if wanted == match.group("direction"):
        return text
    start, end = match.span("direction")
    return text[:start] + wanted + text[end:]


def _closed_round_embed(base_embed, tournament, round_row, matches):
    """Turn a closed qualification overview into an unmistakable final outcome."""
    if _text(round_row.get("status")) != "closed":
        return base_embed
    if _text(round_row.get("round_stage")).lower() != "qualification":
        return base_embed

    from modules.community.live_arena import qualification_panel

    terminal = {"finalized", "forfeit", "double_forfeit", "bye"}
    completed = sum(_text(row.get("status")) in terminal for row in matches)
    round_name = _text(round_row.get("round_name")) or "Qualification Round"
    completed_at = _text(round_row.get("completed_at_utc"))
    finalized = (
        qualification_panel._format_timestamp(completed_at, "F")
        if completed_at
        else "Recorded"
    )
    base_embed.title = f"{round_name} · Final Outcome"
    base_embed.description = (
        f"**{_text(tournament.get('tournament_name'))}**\n"
        "**Status:** Final outcome\n"
        f"**Finalized:** {finalized}\n"
        f"Completed: **{completed} / {len(matches)}**"
    )
    return base_embed


async def _respond_progress(interaction, round_name: str) -> None:
    await interaction.response.send_message(
        embed=discord.Embed(
            title=f"Finishing {round_name}…",
            description="Locking the final results and updating the tournament panels.",
            color=colors.c1c_blue,
        ),
        ephemeral=True,
    )


async def _replace_progress(interaction, embed: discord.Embed) -> None:
    edit = getattr(interaction, "edit_original_response", None)
    if callable(edit):
        await edit(embed=embed)
        return
    await interaction.followup.send(embed=embed, ephemeral=True)


async def _sync_preview_without_recursive_panel_refresh(manager, sync_call) -> None:
    """Publish one preview while reserving the final panel render for a fresh scope."""
    previous = bool(getattr(manager, "_captains_table_stage_reconciling", False))
    manager._captains_table_stage_reconciling = True
    try:
        await sync_call()
    finally:
        manager._captains_table_stage_reconciling = previous


async def _ensure_next_stage_state(manager, closed_round) -> None:
    """Idempotently ensure the next data/preview state after a closed round.

    The normal competition sync already owns stage progression. This is a final
    safety net at the actual Finish Round boundary so a wrapper/cache regression
    cannot leave organizers at a dead end. It never skips organizer decisions:
    Q3 closure exposes Top 8 locking rather than freezing seeds automatically.
    """
    stage = _text(closed_round.get("round_stage")).lower()

    if stage == "qualification":
        try:
            number = int(_text(closed_round.get("round_number")) or 0)
        except ValueError:
            return
        if number not in {1, 2}:
            # Q3 deliberately transitions to the manual Lock Top 8 decision.
            return

        from modules.community.live_arena.swiss import SwissQualificationService
        from modules.community.live_arena import swiss_runtime

        target_number = number + 1
        service = SwissQualificationService(manager.sheet_id)
        await service.initialize()
        snapshot = await service.snapshot(target_number)
        if snapshot.round_row is None:
            snapshot = await service.generate_preview("system", target_number)

        status = _text(snapshot.round_row.get("status")).lower()
        if status in {"preview", "approved"}:
            await _sync_preview_without_recursive_panel_refresh(
                manager,
                lambda: swiss_runtime._sync_preview_message(manager, service, snapshot),
            )
        return

    next_stage = _NEXT_KNOCKOUT_STAGE.get(stage)
    if next_stage is None:
        # Final closure intentionally transitions to Finish Tournament controls.
        return

    from modules.community.live_arena.knockout import KnockoutService
    from modules.community.live_arena import knockout_runtime

    service = KnockoutService(manager.sheet_id)
    await service.initialize()
    snapshot = await service.snapshot(next_stage)
    if snapshot.round_row is None:
        snapshot = await service.generate_next_preview("system", next_stage)

    if _text(snapshot.round_row.get("status")).lower() == "preview":
        await _sync_preview_without_recursive_panel_refresh(
            manager,
            lambda: knockout_runtime._sync_preview_message(manager, service, snapshot),
        )


async def _close_and_reconcile(service, manager, runtime_hooks, *, actor_id: str, round_id: str):
    """Close one round, then cross every write boundary with a fresh read scope.

    There are intentionally four scopes:
    1. close/persist the round;
    2. run the normal stage-aware Discord/data reconciliation from post-close truth;
    3. verify/create the expected next preview from post-reconciliation truth;
    4. render Captain's Table from the final state after any preview write.

    This is what prevents Q2/Q3/knockout progression from inheriting cached rows
    that existed before the close operation.
    """
    with sheet_read_scope():
        closed = await service.close_round(actor_id, round_id)

    warnings: list[str] = []

    # Fresh scope after close: old ``ready_to_close`` rows must not be reusable.
    with sheet_read_scope():
        warnings.extend(await runtime_hooks._best_effort_competition_sync(manager))

    # The normal sync may itself have written the next preview. Read again before
    # deciding whether a progression repair is still needed.
    try:
        with sheet_read_scope():
            await _ensure_next_stage_state(manager, closed)
    except Exception as exc:
        log.exception(
            "Live Arena next-stage progression after round close failed • round=%s • error=%s: %s",
            _text(closed.get("round_id")),
            type(exc).__name__,
            exc,
        )
        warnings.append("next-stage progression")

    # Final visible render must see any preview row written in the previous phase.
    try:
        with sheet_read_scope():
            result = await manager.sync()
        if getattr(result, "ok", True) is False:
            warnings.append("organizer panel")
    except Exception:
        runtime_hooks.log.exception(
            "Live Arena organizer panel refresh after round close failed"
        )
        warnings.append("organizer panel")

    return closed, list(dict.fromkeys(warnings))


async def _close_round_callback(self, interaction) -> None:
    """Acknowledge immediately, close, progress, and render from fresh Sheet truth."""
    from modules.community.live_arena import runtime_hooks
    from modules.community.live_arena.competition_resolution import CompetitionResolutionService
    from modules.community.live_arena.organizer_panel import OrganizerView

    if not await OrganizerView(self.manager).authorized(interaction):
        return

    round_name = "current round"
    await _respond_progress(interaction, round_name)
    try:
        service = CompetitionResolutionService(self.manager.sheet_id)
        await service.initialize()
        rounds = await service.repository.rounds()
        base = await load_config(self.manager.sheet_id)
        tid = base["ACTIVE_TOURNAMENT_ID"]
        closable = [
            row
            for row in rounds
            if _text(row.get("tournament_id")) == tid
            and _text(row.get("status")) in {"ready_to_close", "correction_in_progress"}
        ]
        if len(closable) != 1:
            raise RegistrationError(
                "Exactly one round must be ready to close or in correction before using this action"
            )
        round_name = _text(closable[0].get("round_name")) or round_name

        closed, warnings = await _close_and_reconcile(
            service,
            self.manager,
            runtime_hooks,
            actor_id=str(interaction.user.id),
            round_id=_text(closable[0]["round_id"]),
        )

        name = _text(closed.get("round_name")) or round_name
        embed = discord.Embed(
            title=f"{name} finished",
            description=(
                "All results are locked and the next tournament step is ready in Captain's Table."
            ),
            color=colors.c1c_blue,
        )
        if warnings:
            embed.add_field(
                name="Discord refresh warning",
                value=(
                    "The round itself is safely closed. These tournament items did not fully refresh:\n"
                    + "\n".join(f"• {item}" for item in warnings)
                    + "\n\nUse **Repair Tournament** if they do not update automatically."
                )[:1024],
                inline=False,
            )
        await _replace_progress(interaction, embed)
    except Exception as exc:
        runtime_hooks.log.exception("Live Arena round close action failed")
        await _replace_progress(interaction, error_embed(exc))


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import runtime_hooks, swiss, swiss_panel, swiss_runtime

    # Immediate feedback plus a stage-generic post-close progression boundary.
    runtime_hooks.CloseCurrentRoundButton.callback = _close_round_callback

    # Preserve the existing overview renderer, then make closed qualification
    # rounds visibly final rather than silently changing "State: Round closed".
    original_overview = runtime_hooks._competition_overview_embed

    def overview_with_final_outcome(
        tournament,
        round_row,
        matches,
        standings,
        *,
        guild_id="",
    ):
        embed = original_overview(
            tournament,
            round_row,
            matches,
            standings,
            guild_id=guild_id,
        )
        return _closed_round_embed(embed, tournament, round_row, matches)

    runtime_hooks._competition_overview_embed = overview_with_final_outcome

    # Correct both newly generated Swiss rationale and already-persisted preview
    # wording without changing the Sheet schema/data contract.
    original_pair_swiss = swiss.pair_swiss

    def pair_swiss_with_direction(players, opponent_history):
        pairs = original_pair_swiss(players, opponent_history)
        return [
            swiss.SwissPair(
                pair.player_a,
                pair.player_b,
                normalize_float_rationale(pair.rationale),
            )
            for pair in pairs
        ]

    swiss.pair_swiss = pair_swiss_with_direction

    original_preview = swiss_panel.preview_embed

    def preview_with_direction(snapshot, *, official: bool):
        embed = original_preview(snapshot, official=official)
        for index, field in enumerate(list(embed.fields)):
            embed.set_field_at(
                index,
                name=str(field.name),
                value=normalize_float_rationale(str(field.value)),
                inline=bool(field.inline),
            )
        return embed

    swiss_panel.preview_embed = preview_with_direction
    swiss_runtime.preview_embed = preview_with_direction
