"""Final round-close milestone UX and Swiss preview wording fixes.

Keeps the competition state contract unchanged while making a successful round
closure obvious to organizers and in the Victory Ledger.
"""

from __future__ import annotations

import re

import discord

from shared.sheets.async_core import sheet_read_scope
from shared.theme import colors

from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import _text, load_config
from modules.community.live_arena.views import error_embed

_installed = False
_FLOAT_RE = re.compile(
    r"(?P<name>.+?) \((?P<src>\d+-\d+)\) floated (?P<direction>down|up) to (?P<dst>\d+-\d+)"
)


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


async def _close_round_callback(self, interaction) -> None:
    """Acknowledge immediately, then close and reconcile with one shared read scope."""
    from modules.community.live_arena import runtime_hooks
    from modules.community.live_arena.competition_resolution import CompetitionResolutionService
    from modules.community.live_arena.organizer_panel import OrganizerView

    if not await OrganizerView(self.manager).authorized(interaction):
        return

    round_name = "current round"
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
        await _respond_progress(interaction, round_name)

        warnings: list[str] = []
        with sheet_read_scope():
            closed = await service.close_round(
                str(interaction.user.id), _text(closable[0]["round_id"])
            )
            warnings.extend(await runtime_hooks._best_effort_competition_sync(self.manager))
            try:
                result = await self.manager.sync()
                if getattr(result, "ok", True) is False:
                    warnings.append("organizer panel")
            except Exception:
                runtime_hooks.log.exception(
                    "Live Arena organizer panel refresh after round close failed"
                )
                warnings.append("organizer panel")

        name = _text(closed.get("round_name")) or round_name
        embed = discord.Embed(
            title=f"{name} finished",
            description=(
                "All results are locked and the final standings for this round have been recorded."
            ),
            color=colors.c1c_blue,
        )
        warnings = list(dict.fromkeys(warnings))
        if warnings:
            embed.add_field(
                name="Discord refresh warning",
                value=(
                    "The round itself is safely closed. These Discord items did not fully refresh:\n"
                    + "\n".join(f"• {item}" for item in warnings)
                    + "\n\nUse **Repair Tournament** if they do not update automatically."
                )[:1024],
                inline=False,
            )
        await _replace_progress(interaction, embed)
    except Exception as exc:
        runtime_log = __import__(
            "modules.community.live_arena.runtime_hooks", fromlist=["log"]
        ).log
        runtime_log.exception("Live Arena round close action failed")
        if interaction.response.is_done():
            await _replace_progress(interaction, error_embed(exc))
        else:
            await interaction.response.send_message(embed=error_embed(exc), ephemeral=True)


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import runtime_hooks, swiss, swiss_panel, swiss_runtime

    # Immediate feedback and a clear success/warning distinction for Finish Round.
    runtime_hooks.CloseCurrentRoundButton.callback = _close_round_callback

    # Preserve the existing overview renderer, then make closed qualification
    # rounds visibly final rather than silently changing "State: Round closed".
    original_overview = runtime_hooks._competition_overview_embed

    def overview_with_final_outcome(tournament, round_row, matches, standings):
        embed = original_overview(tournament, round_row, matches, standings)
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
        for field in embed.fields:
            field.value = normalize_float_rationale(str(field.value))
        return embed

    swiss_panel.preview_embed = preview_with_direction
    swiss_runtime.preview_embed = preview_with_direction
