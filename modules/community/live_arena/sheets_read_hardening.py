"""Keep Live Arena organizer actions inside a sane Google Sheets read budget."""

from __future__ import annotations

import asyncio
import logging

import discord

from shared.sheets.async_core import sheet_read_scope
from shared.sheets.core import is_rate_limited_error
from shared.theme import colors

log = logging.getLogger("c1c.community.live_arena.sheets_read_hardening")
_installed = False

_HALL_OF_FAME_STARTUP_DELAY_SECONDS = 240
_HALL_OF_FAME_RETRY_DELAY_SECONDS = 90
_HALL_OF_FAME_MAX_ATTEMPTS = 3


def _log_scope(label: str, reads) -> None:
    log.info(
        "Live Arena Sheets read budget • operation=%s • reads=%s • reused=%s",
        label,
        reads.misses,
        reads.hits,
    )


async def _budgeted_execute_transition(interaction, manager, action) -> None:
    """Run one organizer mutation without re-reading identical tabs repeatedly.

    The mutation and post-write refresh deliberately use separate read scopes. Reusing
    the pre-write TOURNAMENTS read after changing registration state would make the
    refreshed panels stale.
    """
    from modules.community.live_arena import organizer_panel

    try:
        with sheet_read_scope() as mutation_reads:
            service = organizer_panel.OrganizerService(manager.sheet_id)
            await service.initialize()
            await service.transition(action, str(interaction.user.id))
        _log_scope(f"organizer:{action}:mutation", mutation_reads)

        with sheet_read_scope() as refresh_reads:
            warnings = await manager.secondary_sync()
        _log_scope(f"organizer:{action}:refresh", refresh_reads)

        past_tense = {"open": "opened", "close": "closed", "reopen": "reopened"}
        embed = discord.Embed(
            title="Registration updated",
            description=f"Registration was successfully {past_tense[action]}.",
            color=colors.c1c_blue,
        )
        if warnings:
            embed.add_field(
                name="Sync warning",
                value="Core Sheet state was saved, but "
                + ", ".join(warnings)
                + " could not be refreshed.",
                inline=False,
            )
    except Exception as exc:
        log.exception("❌ Live Arena organizer transition failed")
        embed = organizer_panel.error_embed(exc)
    await interaction.followup.send(embed=embed, ephemeral=True)


async def _run_hall_of_fame_startup_sync(manager) -> None:
    """Stagger the expensive historical refresh away from core startup reconciliation."""
    from modules.community.live_arena import hall_of_fame

    await asyncio.sleep(_HALL_OF_FAME_STARTUP_DELAY_SECONDS)
    for attempt in range(1, _HALL_OF_FAME_MAX_ATTEMPTS + 1):
        try:
            with sheet_read_scope() as reads:
                await hall_of_fame.sync_hall_of_fame(manager)
            _log_scope("hall-of-fame:startup", reads)
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if is_rate_limited_error(exc) and attempt < _HALL_OF_FAME_MAX_ATTEMPTS:
                log.warning(
                    "⚠️ Live Arena Hall of Fame startup refresh hit Sheets quota; retrying • attempt=%s/%s • delay=%ss",
                    attempt,
                    _HALL_OF_FAME_MAX_ATTEMPTS,
                    _HALL_OF_FAME_RETRY_DELAY_SECONDS,
                )
                await asyncio.sleep(_HALL_OF_FAME_RETRY_DELAY_SECONDS)
                continue
            log.exception(
                "❌ Live Arena Hall of Fame startup refresh failed • attempt=%s/%s",
                attempt,
                _HALL_OF_FAME_MAX_ATTEMPTS,
            )
            return


def _budgeted_hall_of_fame_schedule(manager) -> None:
    from modules.community.live_arena import hall_of_fame

    sheet_id = str(manager.sheet_id)
    existing = hall_of_fame._sync_tasks.get(sheet_id)
    if existing is not None and not existing.done():
        return

    async def run() -> None:
        try:
            await _run_hall_of_fame_startup_sync(manager)
        finally:
            if hall_of_fame._sync_tasks.get(sheet_id) is asyncio.current_task():
                hall_of_fame._sync_tasks.pop(sheet_id, None)

    try:
        hall_of_fame._sync_tasks[sheet_id] = asyncio.create_task(
            run(), name=f"live-arena-hall-of-fame:{sheet_id[-6:]}"
        )
    except RuntimeError:
        pass


def install() -> None:
    """Install read-budget guards before any Live Arena manager is constructed."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import (
        hall_of_fame,
        organizer_panel,
        organizer_registration_hardening,
        panel,
    )

    # Close-registration preflight is read-only, so all authorization/config/roster
    # reads can safely share one scope.
    original_close_prompt = organizer_registration_hardening._send_close_prompt

    async def budgeted_close_prompt(interaction, manager) -> None:
        with sheet_read_scope() as reads:
            await original_close_prompt(interaction, manager)
        _log_scope("organizer:close:preflight", reads)

    organizer_registration_hardening._send_close_prompt = budgeted_close_prompt

    # Mutations need a fresh post-write scope; replacing this single runtime symbol
    # covers normal transition buttons and both confirmation-view implementations.
    organizer_panel.execute_transition = _budgeted_execute_transition

    # Standalone panel refreshes should also deduplicate their own repeated reads.
    organizer_sync = organizer_panel.OrganizerPanelManager.sync

    async def budgeted_organizer_sync(self):
        with sheet_read_scope() as reads:
            result = await organizer_sync(self)
        _log_scope("organizer:panel-sync", reads)
        return result

    organizer_panel.OrganizerPanelManager.sync = budgeted_organizer_sync

    public_sync = panel.LiveArenaPanelManager.sync

    async def budgeted_public_sync(self):
        with sheet_read_scope() as reads:
            result = await public_sync(self)
        _log_scope("public:panel-sync", reads)
        return result

    panel.LiveArenaPanelManager.sync = budgeted_public_sync

    # The historical panel used to wake 90 seconds after deploy, almost directly on
    # top of the 75-second core startup reconciliation. Move it well clear of that
    # window and give quota failures a bounded retry instead of competing with user
    # interactions during deploy recovery.
    hall_of_fame._schedule_sync = _budgeted_hall_of_fame_schedule
