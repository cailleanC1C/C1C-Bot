"""Keep Live Arena organizer actions inside a sane Google Sheets read budget."""

from __future__ import annotations

import asyncio
import logging
import time

import discord

from shared.sheets.async_core import sheet_read_scope
from shared.sheets.core import is_rate_limited_error
from shared.theme import colors

log = logging.getLogger("c1c.community.live_arena.sheets_read_hardening")
_installed = False

_HALL_OF_FAME_STARTUP_DELAY_SECONDS = 240
_HALL_OF_FAME_RETRY_DELAY_SECONDS = 90
_HALL_OF_FAME_MAX_ATTEMPTS = 3
_AUTH_SOFT_REFRESH_SECONDS = 120
_AUTH_MAX_AGE_SECONDS = 900
_auth_cache: dict[str, tuple[str, float]] = {}
_auth_refresh_tasks: dict[str, asyncio.Task] = {}


def _log_scope(label: str, reads) -> None:
    log.info(
        "Live Arena Sheets read budget • operation=%s • reads=%s • reused=%s",
        label,
        reads.misses,
        reads.hits,
    )


def _remember_organizer_role(manager, config) -> None:
    role_id = str(config.get("ORGANIZER_ROLE_ID", "") or "").strip()
    if not role_id:
        return
    _auth_cache[str(manager.sheet_id)] = (role_id, time.monotonic())


async def _refresh_organizer_role(manager) -> None:
    from modules.community.live_arena.messages import load_pr5_config

    sheet_id = str(manager.sheet_id)
    try:
        with sheet_read_scope() as reads:
            config, _ = await load_pr5_config(sheet_id)
        _remember_organizer_role(manager, config)
        _log_scope("organizer:auth-refresh", reads)
    except asyncio.CancelledError:
        raise
    except Exception:
        log.exception("Live Arena organizer authorization cache refresh failed")


def _schedule_organizer_role_refresh(manager) -> None:
    sheet_id = str(manager.sheet_id)
    existing = _auth_refresh_tasks.get(sheet_id)
    if existing is not None and not existing.done():
        return
    try:
        task = asyncio.create_task(
            _refresh_organizer_role(manager),
            name=f"live-arena-auth-refresh:{sheet_id[-6:]}",
        )
    except RuntimeError:
        return
    _auth_refresh_tasks[sheet_id] = task

    def _done(done_task: asyncio.Task) -> None:
        if _auth_refresh_tasks.get(sheet_id) is done_task:
            _auth_refresh_tasks.pop(sheet_id, None)
        if done_task.cancelled():
            return
        try:
            done_task.result()
        except Exception:
            log.exception("Live Arena organizer authorization refresh task crashed")

    task.add_done_callback(_done)


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
    """Install read-budget and interaction guards before managers are constructed."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import (
        hall_of_fame,
        next_tournament,
        organizer_panel,
        organizer_registration_hardening,
        panel,
    )

    # Organizer authorization used to read CONFIG on every component click. That
    # made every organizer control depend on Sheets completing inside Discord's
    # acknowledgement window, including controls that must open a modal. Keep the
    # last known organizer role in memory instead. If no safe cache exists yet,
    # fail closed immediately and refresh it in the background.
    organizer_data = organizer_panel.OrganizerPanelManager.data

    async def cached_organizer_data(self, guild=None):
        result = await organizer_data(self, guild)
        _remember_organizer_role(self, result[0])
        return result

    organizer_panel.OrganizerPanelManager.data = cached_organizer_data

    async def cached_authorized(self, interaction):
        sheet_id = str(self.manager.sheet_id)
        cached = _auth_cache.get(sheet_id)
        now = time.monotonic()
        if cached is None or (now - cached[1]) > _AUTH_MAX_AGE_SECONDS:
            _schedule_organizer_role_refresh(self.manager)
            await organizer_panel._send_ephemeral(
                interaction,
                embed=organizer_panel.error_embed(
                    "Organizer controls are still initializing. Try again in a moment."
                ),
            )
            return False

        role_id, loaded_at = cached
        if (now - loaded_at) > _AUTH_SOFT_REFRESH_SECONDS:
            _schedule_organizer_role_refresh(self.manager)
        allowed = any(
            str(role.id) == role_id
            for role in getattr(interaction.user, "roles", [])
        )
        if not allowed:
            await organizer_panel._send_ephemeral(
                interaction,
                embed=organizer_panel.error_embed(
                    "You need the configured organizer role to use this control."
                ),
            )
        return allowed

    organizer_panel.OrganizerView.authorized = cached_authorized

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

    # Refresh roster is a component edit backed by several Sheet reads. Acknowledge
    # before loading the roster and then edit the deferred original response.
    async def safe_refresh_roster(self, interaction):
        await organizer_panel._defer_ephemeral(interaction)
        if not await organizer_panel.OrganizerView(self.manager).authorized(interaction):
            return
        try:
            embed = await organizer_panel.roster_embed(self.manager, interaction.guild)
            await interaction.edit_original_response(
                embed=embed,
                view=organizer_panel.RosterActions(self.manager),
            )
        except Exception as exc:
            log.exception("Live Arena organizer roster refresh failed")
            await interaction.followup.send(
                embed=organizer_panel.error_embed(exc), ephemeral=True
            )

    organizer_panel.RefreshRoster.callback = safe_refresh_roster

    # The first Create Next Tournament click loads Sheet-backed copy before opening
    # the ephemeral wizard. Acknowledge first; the later modal handoffs are already
    # protected by next_tournament_modal_boundary / next_tournament_wizard_ux.
    async def safe_create_next_tournament(self, interaction):
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        if not await organizer_panel.OrganizerView(self.manager).authorized(interaction):
            return
        try:
            messages = await next_tournament._load_next_messages(
                self.manager.sheet_id, {"next_tournament_intro"}
            )
            await interaction.followup.send(
                embed=messages["next_tournament_intro"].embed(),
                view=next_tournament.NextTournamentStartView(self.manager),
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Live Arena Create Next Tournament start failed")
            await interaction.followup.send(
                embed=next_tournament.error_embed(exc), ephemeral=True
            )

    next_tournament.CreateNextTournamentButton.callback = safe_create_next_tournament

    # The historical panel used to wake 90 seconds after deploy, almost directly on
    # top of the 75-second core startup reconciliation. Move it well clear of that
    # window and give quota failures a bounded retry instead of competing with user
    # interactions during deploy recovery.
    hall_of_fame._schedule_sync = _budgeted_hall_of_fame_schedule
