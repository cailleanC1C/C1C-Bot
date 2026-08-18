"""Fail-safe wrapper for the Victory Ledger workspace.

The workspace is presentation-only. If its thread/index layer cannot initialize, live
round/result processing must keep working and the canonical overview falls back to
the legacy parent-channel destination instead of returning a broken Discord state.
"""

from __future__ import annotations

import logging

import discord

from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.victory_ledger_workspace_fallback")
_installed = False


async def _legacy_overview_sync(bot, qualification_service, snapshot) -> None:
    """Render only the pre-workspace Victory Ledger overview destination."""
    from modules.community.live_arena import qualification_panel, runtime_hooks
    from modules.community.live_arena.round_overview import render_round_overview_embeds

    sheet_id = qualification_service.sheet_id
    config = qualification_service.repository.config
    overview_channel = await qualification_panel._resolve_channel(
        bot, int(config["ROUND_OVERVIEW_CHANNEL_ID"])
    )
    guild_id = _text(getattr(getattr(overview_channel, "guild", None), "id", ""))
    _, (_, tournament), _, _ = await qualification_service.context()
    standings = []
    if _text(snapshot.round_row.get("round_stage")).lower() == "qualification":
        competition_service = runtime_hooks.CompetitionResolutionService(sheet_id)
        await competition_service.initialize()
        standings = await competition_service.standings()

    embeds = await render_round_overview_embeds(
        sheet_id=sheet_id,
        tournament=tournament,
        round_row=snapshot.round_row,
        matches=[dict(row) for row in snapshot.matches],
        standings=standings,
        guild_id=guild_id,
    )
    overview_id = _text(snapshot.round_row.get("overview_message_id"))
    message = None
    if overview_id:
        try:
            message = await overview_channel.fetch_message(int(overview_id))
        except discord.NotFound:
            message = None
    if message is not None:
        await message.edit(embeds=embeds)
        return

    created = await overview_channel.send(embeds=embeds)
    recorder = getattr(qualification_service, "record_overview_message_id", None)
    if not callable(recorder):
        return
    try:
        await recorder(_text(snapshot.round_row["round_id"]), str(created.id))
    except Exception:
        try:
            await created.delete()
        except Exception:
            log.exception("Live Arena untracked fallback overview cleanup failed")
        raise


async def _sync_with_fallback(workspace_sync, bot, qualification_service, snapshot):
    # Older integration callers and lightweight tests intentionally do not expose
    # the registration repository. They use the pre-workspace destination directly.
    if getattr(qualification_service, "registration_repository", None) is None:
        try:
            await _legacy_overview_sync(bot, qualification_service, snapshot)
            return []
        except Exception:
            log.exception("Live Arena legacy Victory Ledger compatibility sync failed")
            return ["Victory Ledger overview"]

    warnings = list(await workspace_sync(bot, qualification_service, snapshot))
    if "Victory Ledger overview" not in warnings:
        return warnings

    # The workspace is optional presentation infrastructure. Thread permissions,
    # missing copy, or Discord thread creation must never block the tournament.
    try:
        await _legacy_overview_sync(bot, qualification_service, snapshot)
    except Exception:
        log.exception("Live Arena Victory Ledger fallback after workspace failure failed")
        return warnings

    return [warning for warning in warnings if warning != "Victory Ledger overview"]


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import hall_of_fame, knockout_runtime, runtime_hooks
    from modules.community.live_arena import victory_ledger_workspace as workspace

    workspace_sync = runtime_hooks._sync_round_discord

    async def sync(bot, qualification_service, snapshot):
        return await _sync_with_fallback(
            workspace_sync, bot, qualification_service, snapshot
        )

    runtime_hooks._sync_round_discord = sync

    # Final recap and Hall of Fame are also presentation-only. If workspace routing
    # fails, preserve their established parent-channel behavior instead of failing
    # tournament completion/history refresh.
    routed_final_recap = knockout_runtime._sync_final_recap
    original_final_recap = workspace._original_final_recap

    async def final_recap(manager, service, summary):
        try:
            await routed_final_recap(manager, service, summary)
        except Exception:
            log.exception(
                "Live Arena Tournament Results routing failed; using legacy final recap"
            )
            await original_final_recap(manager, service, summary)

    knockout_runtime._sync_final_recap = final_recap

    routed_hall = hall_of_fame.sync_hall_of_fame

    async def hall(manager):
        try:
            await routed_hall(manager)
        except Exception:
            log.exception(
                "Live Arena Hall of Fame thread routing failed; keeping history sync alive"
            )
            # The routed final recap wrapper may already include the original Hall of
            # Fame hook. Do not recurse into a replaced hall sync here; logging the
            # failure is safer than duplicating a global history message.
            return None

    hall_of_fame.sync_hall_of_fame = hall
