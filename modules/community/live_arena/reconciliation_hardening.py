"""Keep archived Live Arena history immutable and surface startup reconciliation health."""

from __future__ import annotations

import asyncio
import logging

from shared.sheets.async_core import sheet_read_scope
from shared.sheets.core import is_rate_limited_error

from modules.community.live_arena.competition_resolution import CompetitionResolutionService
from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.reconciliation_hardening")
_installed = False
_TERMINAL_TOURNAMENT_STATUSES = {"completed", "archived"}
_ACTIVE_RESULT_ROUND_STATUSES = {
    "active",
    "published",
    "open",
    "published/open",
    "correction_in_progress",
}


def _tournament_status(tournament) -> str:
    if isinstance(tournament, dict):
        return _text(tournament.get("status")).lower()
    return _text(getattr(tournament, "status", "")).lower()


async def _sync_round_discord(bot, qualification_service, snapshot) -> list[str]:
    """Reconcile one round without reattaching live controls to terminal tournaments."""
    from modules.community.live_arena import runtime_hooks
    from modules.community.live_arena.round_overview import render_round_overview_embeds
    from modules.community.live_arena.victory_ledger_workspace import (
        ensure_workspace,
        sync_round_overview,
    )

    warnings: list[str] = []
    sheet_id = qualification_service.sheet_id
    round_status = _text(snapshot.status).lower()
    matches = [dict(row) for row in snapshot.matches]
    _, (_, tournament), _, _ = await qualification_service.context()
    tournament_status = _tournament_status(tournament)
    tournament_id = _text(snapshot.round_row.get("tournament_id"))
    round_id = _text(snapshot.round_row.get("round_id"))

    if tournament_status in _TERMINAL_TOURNAMENT_STATUSES:
        log.info(
            "Live Arena result controls skipped for terminal tournament • tournament=%s • tournament_status=%s • round=%s • round_status=%s • matches=%s",
            tournament_id,
            tournament_status,
            round_id,
            round_status,
            len(matches),
        )
    else:
        for match in matches:
            thread_id = _text(match.get("thread_id"))
            if not thread_id:
                continue
            match_id = _text(match.get("match_id"))
            match_status = _text(match.get("status"))
            try:
                thread = bot.get_channel(int(thread_id))
                if thread is None:
                    thread = await bot.fetch_channel(int(thread_id))
                report_disabled = not (
                    round_status in _ACTIVE_RESULT_ROUND_STATUSES
                    and match_status in {"published", "open"}
                )
                dispute_disabled = not (
                    round_status in _ACTIVE_RESULT_ROUND_STATUSES
                    and match_status == "pending_confirmation"
                )
                await runtime_hooks._ensure_match_result_view(
                    thread,
                    sheet_id,
                    report_disabled=report_disabled,
                    dispute_disabled=dispute_disabled,
                )
            except Exception as exc:
                log.exception(
                    "Live Arena result-control reconciliation failed • tournament=%s • tournament_status=%s • round=%s • round_status=%s • match=%s • match_status=%s • thread=%s • error=%s: %s",
                    tournament_id,
                    tournament_status or "unknown",
                    round_id,
                    round_status,
                    match_id,
                    match_status,
                    thread_id,
                    type(exc).__name__,
                    exc,
                )
                warnings.append(
                    f"Match {_text(match.get('match_number'))} result controls"
                )

    try:
        workspace = await ensure_workspace(
            bot,
            sheet_id,
            qualification_service.registration_repository,
        )
        guild_id = _text(
            getattr(getattr(workspace.parent, "guild", None), "id", "")
        )
        standings = []
        if _text(snapshot.round_row.get("round_stage")).lower() == "qualification":
            competition_service = CompetitionResolutionService(sheet_id)
            await competition_service.initialize()
            standings = await competition_service.standings()
        embeds = await render_round_overview_embeds(
            sheet_id=sheet_id,
            tournament=tournament,
            round_row=snapshot.round_row,
            matches=matches,
            standings=standings,
            guild_id=guild_id,
        )
        await sync_round_overview(bot, qualification_service, snapshot, embeds)
    except Exception as exc:
        log.exception(
            "Live Arena Victory Ledger workspace synchronization failed • tournament=%s • tournament_status=%s • round=%s • round_status=%s • error=%s: %s",
            tournament_id,
            tournament_status or "unknown",
            round_id,
            round_status,
            type(exc).__name__,
            exc,
        )
        warnings.append("Victory Ledger overview")
    return list(dict.fromkeys(warnings))


async def _run_startup_sync(
    manager,
    organizer,
    qualification_installed: bool,
    refresh_qualification_state,
    reconcile_qualification_publication,
) -> None:
    """Run startup reconciliation and report stage-aware warnings in the final health line."""
    from modules.community.live_arena import panel

    await asyncio.sleep(panel._STARTUP_SYNC_DELAY_SECONDS)

    for attempt in range(1, panel._STARTUP_MAX_ATTEMPTS + 1):
        warnings: list[str] = []
        sheet_reads = 0
        reused_reads = 0
        try:
            with sheet_read_scope() as reads:
                if qualification_installed:
                    try:
                        await refresh_qualification_state(organizer)
                    except Exception as exc:
                        if is_rate_limited_error(exc):
                            raise
                        log.exception(
                            "Live Arena qualification startup state refresh failed • error=%s: %s",
                            type(exc).__name__,
                            exc,
                        )
                        warnings.append("qualification state")

                try:
                    result = await manager.sync()
                    if isinstance(result, panel.PanelSyncResult) and not result.ok:
                        warnings.append("public panel")
                except Exception as exc:
                    if is_rate_limited_error(exc):
                        raise
                    log.exception(
                        "Live Arena public panel startup refresh failed • error=%s: %s",
                        type(exc).__name__,
                        exc,
                    )
                    warnings.append("public panel")

                try:
                    result = await organizer.sync()
                    if isinstance(result, panel.PanelSyncResult) and not result.ok:
                        warnings.append("organizer panel")
                except Exception as exc:
                    if is_rate_limited_error(exc):
                        raise
                    log.exception(
                        "Live Arena organizer panel startup refresh failed • error=%s: %s",
                        type(exc).__name__,
                        exc,
                    )
                    warnings.append("organizer panel")

                sheet_reads = reads.misses
                reused_reads = reads.hits

            if qualification_installed:
                stage_sync = getattr(organizer, "_competition_sync", None)
                if callable(stage_sync):
                    try:
                        competition_warnings = await stage_sync()
                        warnings.extend(list(competition_warnings or []))
                    except Exception as exc:
                        if is_rate_limited_error(exc):
                            raise
                        log.exception(
                            "Live Arena stage-aware startup reconciliation failed • error=%s: %s",
                            type(exc).__name__,
                            exc,
                        )
                        warnings.append("competition Discord state")
                else:
                    try:
                        publication_warnings = (
                            await reconcile_qualification_publication(organizer)
                        )
                        warnings.extend(publication_warnings)
                    except Exception as exc:
                        if is_rate_limited_error(exc):
                            raise
                        log.exception(
                            "Live Arena qualification startup publication retry failed • error=%s: %s",
                            type(exc).__name__,
                            exc,
                        )
                        warnings.append("qualification publication")

            unique_warnings = list(dict.fromkeys(warnings))
            log.info(
                "Live Arena startup reconciliation finished • attempt=%s • sheet_reads=%s • reused_reads=%s • warnings=%s",
                attempt,
                sheet_reads,
                reused_reads,
                ", ".join(unique_warnings) or "none",
            )
        except Exception as exc:
            if is_rate_limited_error(exc):
                if attempt < panel._STARTUP_MAX_ATTEMPTS:
                    log.warning(
                        "Live Arena startup refresh hit Sheets quota; retrying after startup settles • attempt=%s/%s • delay=%ss • error=%s",
                        attempt,
                        panel._STARTUP_MAX_ATTEMPTS,
                        panel._STARTUP_RETRY_DELAY_SECONDS,
                        exc,
                    )
                    await asyncio.sleep(panel._STARTUP_RETRY_DELAY_SECONDS)
                    continue
                log.exception(
                    "Live Arena startup refresh exhausted Sheets quota retries • attempts=%s",
                    panel._STARTUP_MAX_ATTEMPTS,
                )
                return
            log.exception(
                "Live Arena startup refresh failed unexpectedly • error=%s: %s",
                type(exc).__name__,
                exc,
            )
            return

        if not warnings:
            return
        if attempt < panel._STARTUP_MAX_ATTEMPTS:
            log.warning(
                "Live Arena startup refresh incomplete; retrying • attempt=%s/%s • delay=%ss • items=%s",
                attempt,
                panel._STARTUP_MAX_ATTEMPTS,
                panel._STARTUP_RETRY_DELAY_SECONDS,
                ", ".join(dict.fromkeys(warnings)),
            )
            await asyncio.sleep(panel._STARTUP_RETRY_DELAY_SECONDS)
            continue
        return


def install() -> None:
    """Install after Victory Ledger workspace so this guard is the final reconciliation path."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import panel, runtime_hooks

    runtime_hooks._sync_round_discord = _sync_round_discord
    panel._run_startup_sync = _run_startup_sync
