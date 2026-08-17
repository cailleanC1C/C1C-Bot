"""Final knockout standings and Captain's Table progression boundary.

The knockout result path had two qualification-only assumptions left in the final
runtime stack:

* Victory Ledger deliberately omitted standings outside qualification rounds.
* ``round_finish_refresh`` only re-rendered Captain's Table when Q1 became
  closable, so a Quarterfinal/Semifinal/Final could be ready to finish while the
  persistent organizer panel still showed its previous controls.

This installer is deliberately stage-generic and runs after the existing Live Arena
repair layers.  It does not create new tournament state.  It derives the current
Top-8 order from frozen seeds + finalized knockout matches and makes the already
existing close/preview progression controls visible at the final render boundary.
"""

from __future__ import annotations

import logging

from shared.sheets.async_core import sheet_read_scope

from modules.community.live_arena.competition import (
    MATCH_TERMINAL_STATUSES,
    StandingEntry,
)
from modules.community.live_arena.service import _text, load_config

log = logging.getLogger("c1c.community.live_arena.knockout_progression_finalizer")
_installed = False
_KNOCKOUT_STAGES = {"quarterfinal", "semifinal", "final"}
_CLOSABLE_STATUSES = {"ready_to_close", "correction_in_progress"}


def _has_knockout_round(rounds, tournament_id: str) -> bool:
    return any(
        _text(row.get("tournament_id")) == tournament_id
        and _text(row.get("round_stage")).lower() in _KNOCKOUT_STAGES
        for row in rounds
    )


def _int_score(value: object) -> int:
    try:
        return max(0, int(_text(value) or 0))
    except (TypeError, ValueError):
        return 0


def calculate_knockout_standings(rounds, matches, tournament_id: str) -> list[StandingEntry]:
    """Derive the current Top-8 order from immutable seeds and knockout results.

    Ordering is intentionally simple and stable for the fixed bracket:
    knockout match wins descending, knockout losses ascending, then frozen seed.
    That means QF winners move above QF losers immediately, SF finalists move above
    eliminated semifinalists, and the original qualification seed remains the
    deterministic tie-breaker between players at the same knockout record.
    """

    from modules.community.live_arena import knockout

    seed_row = knockout._seed_row(rounds, tournament_id)
    if seed_row is None:
        return []
    seeds = knockout._read_seeds(seed_row)

    stats: dict[str, dict[str, object]] = {}
    for seed in seeds:
        uid = _text(seed.get("discord_user_id"))
        stats[uid] = {
            "seed": int(seed.get("seed", 0)),
            "display_name": _text(seed.get("display_name")) or uid,
            "match_wins": 0,
            "match_losses": 0,
            "game_wins": 0,
            "game_losses": 0,
        }

    knockout_round_ids = {
        f"{tournament_id}-{meta['suffix']}" for meta in knockout.KNOCKOUT.values()
    }
    for row in matches:
        if _text(row.get("tournament_id")) != tournament_id:
            continue
        if _text(row.get("round_id")) not in knockout_round_ids:
            continue
        if _text(row.get("status")).lower() not in MATCH_TERMINAL_STATUSES:
            continue

        player_a = _text(row.get("player_a_discord_user_id"))
        player_b = _text(row.get("player_b_discord_user_id"))
        score_a = _int_score(row.get("final_score_a"))
        score_b = _int_score(row.get("final_score_b"))
        if player_a in stats:
            stats[player_a]["game_wins"] = int(stats[player_a]["game_wins"]) + score_a
            stats[player_a]["game_losses"] = int(stats[player_a]["game_losses"]) + score_b
        if player_b in stats:
            stats[player_b]["game_wins"] = int(stats[player_b]["game_wins"]) + score_b
            stats[player_b]["game_losses"] = int(stats[player_b]["game_losses"]) + score_a

        winner = _text(row.get("final_winner_discord_user_id"))
        if winner and winner in stats:
            stats[winner]["match_wins"] = int(stats[winner]["match_wins"]) + 1
            loser = player_b if winner == player_a else player_a if winner == player_b else ""
            if loser in stats:
                stats[loser]["match_losses"] = int(stats[loser]["match_losses"]) + 1
        elif _text(row.get("status")).lower() == "double_forfeit":
            for uid in (player_a, player_b):
                if uid in stats:
                    stats[uid]["match_losses"] = int(stats[uid]["match_losses"]) + 1

    ordered = sorted(
        stats.items(),
        key=lambda item: (
            -int(item[1]["match_wins"]),
            int(item[1]["match_losses"]),
            int(item[1]["seed"]),
        ),
    )
    result: list[StandingEntry] = []
    for rank, (uid, row) in enumerate(ordered, 1):
        game_wins = int(row["game_wins"])
        game_losses = int(row["game_losses"])
        result.append(
            StandingEntry(
                discord_user_id=uid,
                display_name=str(row["display_name"]),
                match_wins=int(row["match_wins"]),
                match_losses=int(row["match_losses"]),
                game_wins=game_wins,
                game_losses=game_losses,
                game_differential=game_wins - game_losses,
                strength_of_opponents=0,
                rank=rank,
                tied=False,
            )
        )
    return result


def _knockout_standings_text(standings, templates) -> str:
    if standings:
        lines = [
            templates["round_overview_standing_line"].render(
                rank=entry.rank,
                record=entry.match_record,
                player_mention=f"<@{entry.discord_user_id}>",
            )[1]
            for entry in standings
        ]
        return "\n".join(lines)[:4096]
    return templates["round_standings_empty"].render()[0]


async def _load_knockout_standings(sheet_id: str, round_row) -> list[StandingEntry]:
    from modules.community.live_arena import knockout

    tournament_id = _text(round_row.get("tournament_id"))
    if not tournament_id:
        return []
    service = knockout.KnockoutService(sheet_id)
    await service.initialize()
    rounds = await service.repository.rounds()
    matches = await service.repository.matches()
    return calculate_knockout_standings(rounds, matches, tournament_id)


async def _render_round_overview_with_knockout_standings(original, **kwargs):
    """Append the normal Sheet-driven standings embed to knockout ledgers too."""
    from modules.community.live_arena import round_overview, victory_ledger_final_refresh

    round_row = kwargs.get("round_row") or {}
    stage = _text(round_row.get("round_stage")).lower()
    embeds = list(await original(**kwargs))
    if stage not in _KNOCKOUT_STAGES:
        return embeds

    standings = list(kwargs.get("standings") or ())
    if not standings:
        standings = await _load_knockout_standings(
            str(kwargs.get("sheet_id") or ""), round_row
        )
    templates = await round_overview._templates(str(kwargs.get("sheet_id") or ""))
    standings_embed = templates["round_overview_standings"].embed(
        standings_lines=_knockout_standings_text(standings, templates)
    )

    # The existing final renderer stamps the last embed before this layer appends
    # the knockout standings. Move that stamp so Last updated remains on the true
    # final embed rather than the Matchups embed.
    label = victory_ledger_final_refresh._last_updated_labels.get(
        str(kwargs.get("sheet_id") or "")
    )
    if embeds and label:
        footer = _text(getattr(getattr(embeds[-1], "footer", None), "text", ""))
        if footer == label:
            embeds[-1].remove_footer()
            embeds[-1].timestamp = None
    embeds.append(standings_embed)
    return victory_ledger_final_refresh._stamp_last_updated(
        embeds, str(kwargs.get("sheet_id") or "")
    )


def _display_knockout_standings(state) -> None:
    if not _has_knockout_round(state.rounds, state.tournament_id):
        return
    standings = calculate_knockout_standings(
        state.rounds, state.matches, state.tournament_id
    )
    if standings:
        state.standings = standings


def _has_closable_round(rounds, tournament_id: str) -> bool:
    return any(
        _text(row.get("tournament_id")) == tournament_id
        and _text(row.get("status")).lower() in _CLOSABLE_STATUSES
        for row in rounds
    )


async def _sync_competition_and_refresh_closable_panel(manager, base_sync):
    """Refresh Captain's Table whenever any live round becomes finishable."""
    from modules.community.live_arena.competition_resolution import (
        CompetitionResolutionService,
    )

    with sheet_read_scope():
        warnings = list(await base_sync(manager))
        try:
            service = CompetitionResolutionService(manager.sheet_id)
            await service.initialize()
            config = await load_config(manager.sheet_id)
            rounds = await service.repository.rounds()
            should_refresh = _has_closable_round(
                rounds, config["ACTIVE_TOURNAMENT_ID"]
            )
        except Exception as exc:
            log.warning(
                "Live Arena current-round status refresh after result mutation failed • error=%s: %s",
                type(exc).__name__,
                exc,
            )
            return list(dict.fromkeys(warnings))

        if not should_refresh:
            return list(dict.fromkeys(warnings))

        try:
            result = await manager.sync()
            if getattr(result, "ok", True) is False:
                warnings.append("organizer panel")
        except Exception as exc:
            log.exception(
                "Live Arena Captain's Table refresh after closable round failed • error=%s: %s",
                type(exc).__name__,
                exc,
            )
            warnings.append("organizer panel")
        return list(dict.fromkeys(warnings))


def install() -> None:
    """Install after every existing Live Arena render/progression repair layer."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import (
        captains_table_control_center as control,
        round_finish_refresh,
        round_overview,
        round_overview_migration,
    )

    # 1) The existing post-result hook is already the correct live/startup boundary;
    # replace only its Q1-specific decision helper so QF/SF/Final can refresh the
    # same persistent Captain's Table when they become ready to finish.
    round_finish_refresh._sync_competition_and_maybe_panel = (
        _sync_competition_and_refresh_closable_panel
    )

    # 2) Keep qualification tiebreak calculations qualification-based, then swap
    # only the display standings after the frozen Top 8 has entered knockout play.
    original_state = control._ensure_tiebreak_sheet_state

    async def ensure_state_with_knockout_order(service):
        state = await original_state(service)
        _display_knockout_standings(state)
        return state

    control._ensure_tiebreak_sheet_state = ensure_state_with_knockout_order

    original_resolved = control._resolved_standings

    def resolved_standings_for_current_stage(state):
        if _has_knockout_round(state.rounds, state.tournament_id):
            return state.standings
        return original_resolved(state)

    control._resolved_standings = resolved_standings_for_current_stage

    # 3) The canonical overview renderer remains Sheet-driven.  Knockout rounds now
    # receive the same standings template instead of being hard-coded to two embeds.
    original_render = round_overview.render_round_overview_embeds

    async def render_with_knockout_standings(**kwargs):
        return await _render_round_overview_with_knockout_standings(
            original_render, **kwargs
        )

    round_overview.render_round_overview_embeds = render_with_knockout_standings

    # The old migration guard expected exactly two embeds for knockout rounds.
    # Three is now canonical; the final Victory Ledger refresh owns the actual
    # rewrite, so do not make the legacy shape-check fight the new renderer.
    original_migration = round_overview_migration.ensure_existing_overview_payload

    async def ensure_existing_overview_payload(bot, qualification_service, snapshot):
        round_row = getattr(snapshot, "round_row", None)
        if (
            round_row is not None
            and _text(round_row.get("round_stage")).lower() in _KNOCKOUT_STAGES
            and _text(round_row.get("overview_message_id"))
        ):
            return True
        return await original_migration(bot, qualification_service, snapshot)

    round_overview_migration.ensure_existing_overview_payload = (
        ensure_existing_overview_payload
    )
