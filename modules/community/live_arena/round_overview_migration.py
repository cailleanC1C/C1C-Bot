"""Final migration guard for existing Victory Ledger round overview messages.

PR #1123 introduced the canonical multi-embed renderer, but the live migration
case needs an explicit final boundary: an already-persisted overview message must
be checked after the full decorated competition sync and rewritten in place if
it still has the legacy single-embed shape.

This module also preloads the Sheet-backed overview copy during Live Arena
registration so the delayed startup reconciliation is not the first consumer of
those message rows.
"""

from __future__ import annotations

import logging

from shared.config import cfg

from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.round_overview_migration")
_installed = False


async def ensure_existing_overview_payload(bot, qualification_service, snapshot) -> bool:
    """Ensure an already-persisted round overview uses the canonical embed shape.

    Returns True when an existing overview message was successfully verified or
    migrated. Returns False when the snapshot does not yet point at a persisted
    overview message (new-round creation remains owned by the normal sync path).
    """
    round_row = getattr(snapshot, "round_row", None)
    if round_row is None:
        return False
    overview_id = _text(round_row.get("overview_message_id"))
    if not overview_id:
        return False

    from modules.community.live_arena import qualification_panel
    from modules.community.live_arena.competition_resolution import (
        CompetitionResolutionService,
    )
    from modules.community.live_arena.round_overview import (
        render_round_overview_embeds,
    )

    config = qualification_service.repository.config
    overview_channel = await qualification_panel._resolve_channel(
        bot, int(config["ROUND_OVERVIEW_CHANNEL_ID"])
    )
    message = await overview_channel.fetch_message(int(overview_id))

    stage = _text(round_row.get("round_stage")).lower()
    expected_embed_count = 3 if stage == "qualification" else 2
    current_embeds = list(getattr(message, "embeds", ()) or ())
    if len(current_embeds) == expected_embed_count:
        return True

    guild_id = _text(
        getattr(getattr(overview_channel, "guild", None), "id", "")
    )
    _, (_, tournament), _, _ = await qualification_service.context()
    standings = []
    if stage == "qualification":
        competition_service = CompetitionResolutionService(
            qualification_service.sheet_id
        )
        await competition_service.initialize()
        standings = await competition_service.standings()

    embeds = await render_round_overview_embeds(
        sheet_id=qualification_service.sheet_id,
        tournament=tournament,
        round_row=round_row,
        matches=[dict(row) for row in snapshot.matches],
        standings=standings,
        guild_id=guild_id,
    )
    await message.edit(embeds=embeds)
    log.info(
        "Live Arena migrated existing Victory Ledger overview • round=%s • message=%s • embeds=%s",
        _text(round_row.get("round_id")),
        overview_id,
        len(embeds),
    )
    return True


def install() -> None:
    """Install the final existing-message migration and copy preload boundary."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import panel, round_overview, runtime_hooks

    # Preload the Sheet-backed round overview copy before the delayed startup
    # reconciliation. This makes startup migration use the already-validated
    # MESSAGES payload instead of depending on a first-use config/messages read.
    original_register = panel.register_live_arena

    async def register_with_round_overview_preload(bot):
        sheet_id = str(
            cfg.get("LIVE_ARENA_TOURNAMENT_SHEET_ID", "") or ""
        ).strip()
        if sheet_id:
            try:
                await round_overview._templates(sheet_id)
            except Exception:
                # Do not prevent persistent view registration. The normal startup
                # reconciliation still retries and will surface its own warning.
                log.exception("Live Arena round overview Sheet-copy preload failed")
        return await original_register(bot)

    panel.register_live_arena = register_with_round_overview_preload

    # This is deliberately installed last. All older wrappers get to perform
    # their normal result/view/thread maintenance first; then the canonical
    # overview shape wins as the final presentation boundary.
    original_sync = runtime_hooks._sync_round_discord

    async def sync_with_existing_overview_migration(
        bot, qualification_service, snapshot
    ):
        warnings = list(
            await original_sync(bot, qualification_service, snapshot)
        )
        try:
            verified = await ensure_existing_overview_payload(
                bot, qualification_service, snapshot
            )
            if verified:
                warnings = [
                    item
                    for item in warnings
                    if item != "Victory Ledger overview"
                ]
        except Exception:
            log.exception(
                "Live Arena existing Victory Ledger overview migration failed"
            )
            warnings.append("Victory Ledger overview")
        return list(dict.fromkeys(warnings))

    runtime_hooks._sync_round_discord = sync_with_existing_overview_migration
