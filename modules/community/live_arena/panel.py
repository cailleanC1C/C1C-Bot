"""Persistent public panel lifecycle for Live Arena PR3."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import discord

from shared.config import cfg
from shared.sheets.async_core import (
    acall_with_backoff,
    aget_worksheet,
    sheet_read_scope,
)
from shared.sheets.core import is_rate_limited_error

from modules.community.live_arena.messages import (
    discord_timestamp,
    load_messages,
    load_pr3_config,
)
from modules.community.live_arena.repository import LiveArenaRepository
from modules.community.live_arena.service import load_tournament_snapshot

log = logging.getLogger("c1c.community.live_arena.panel")
_sync_locks: dict[str, asyncio.Lock] = {}
_managers: dict[tuple[int, str], "LiveArenaPanelManager"] = {}
_startup_sync_tasks: dict[tuple[int, str], asyncio.Task] = {}

# Live Arena panel refresh is not required for Discord login. Keep it out of the
# first-minute Sheets stampede and let the persistent controls wire immediately.
_STARTUP_SYNC_DELAY_SECONDS = 75
_STARTUP_RETRY_DELAY_SECONDS = 90
_STARTUP_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class PanelSyncResult:
    """Outcome of a panel sync, including failures deliberately handled here."""

    ok: bool
    operation: str = ""


class LiveArenaPanelManager:
    def __init__(self, bot, sheet_id: str, service_factory=None):
        self.bot = bot
        self.sheet_id = sheet_id
        self.service_factory = service_factory
        # Registration hooks can be invoked more than once (including overlapping
        # on_ready dispatches).  The workbook, not a transient manager, owns sync.
        self._lock = _sync_locks.setdefault(sheet_id, asyncio.Lock())

    async def sync(self) -> PanelSyncResult:
        async with self._lock:
            config, matrix = await load_pr3_config(self.sheet_id)
            tournament = await load_tournament_snapshot(self.sheet_id)
            if tournament.status == "draft":
                return PanelSyncResult(True)
            key = (
                "signup_open" if tournament.status == "signup_open" else "signup_closed"
            )
            if tournament.status not in {"signup_open", "signup_closed"}:
                return PanelSyncResult(True)
            messages = await load_messages(self.sheet_id, config["MESSAGES_TAB"], {key})
            repository = LiveArenaRepository(self.sheet_id)
            await repository.initialize()
            participants = await repository.participants()
            count = sum(
                str(row["tournament_id"]).strip() == tournament.tournament_id
                and str(row["status"]).strip() == "confirmed"
                for row in participants
            )
            values = dict(
                tournament_name=tournament.tournament_name, confirmed_count=count
            )
            if key == "signup_open":
                values.update(
                    signup_deadline=discord_timestamp(tournament.signup_closes_at_utc),
                    max_participants=tournament.max_participants,
                )
            embed = messages[key].embed(**values)
            channel = self.bot.get_channel(int(config["SIGNUP_CHANNEL_ID"]))
            if channel is None:
                channel = await self.bot.fetch_channel(int(config["SIGNUP_CHANNEL_ID"]))
            message = None
            if config["PUBLIC_PANEL_MESSAGE_ID"]:
                try:
                    message = await channel.fetch_message(
                        int(config["PUBLIC_PANEL_MESSAGE_ID"])
                    )
                except discord.NotFound:
                    message = None
                except Exception:
                    log.exception("❌ Live Arena panel — fetch failed")
                    return PanelSyncResult(False, "fetch")
            from modules.community.live_arena.entry_views import RegistrationEntryView
            from modules.community.live_arena.views import ClosedTournamentView

            view = (
                RegistrationEntryView(self)
                if key == "signup_open"
                else ClosedTournamentView(self)
            )
            if message is not None:
                try:
                    await message.edit(embed=embed, view=view)
                except Exception:
                    log.exception("❌ Live Arena panel — edit failed")
                    return PanelSyncResult(False, "edit")
                return PanelSyncResult(True)
            created = await channel.send(embed=embed, view=view)
            try:
                await self._persist_message_id(matrix, str(created.id))
            except Exception:
                log.exception("❌ Live Arena panel — message ID persistence failed")
                try:
                    await created.delete()
                except Exception:
                    log.exception(
                        "⚠️ Live Arena panel — untracked message cleanup failed"
                    )
                raise
            return PanelSyncResult(True)

    async def _persist_message_id(self, matrix, message_id: str) -> None:
        headers = [str(value).strip() for value in matrix[0]]
        key_col, value_col = headers.index("Key"), headers.index("Value")
        rows = [
            index
            for index, row in enumerate(matrix[1:], 2)
            if key_col < len(row)
            and str(row[key_col]).strip() == "PUBLIC_PANEL_MESSAGE_ID"
        ]
        if len(rows) != 1:
            raise RuntimeError(
                "CONFIG: key PUBLIC_PANEL_MESSAGE_ID must occur exactly once"
            )
        worksheet = await aget_worksheet(self.sheet_id, "CONFIG")
        await acall_with_backoff(
            worksheet.update_cell, rows[0], value_col + 1, message_id
        )


def _schedule_startup_sync(
    bot,
    sheet_id: str,
    manager: LiveArenaPanelManager,
    organizer,
    qualification_installed: bool,
    refresh_qualification_state,
    reconcile_qualification_publication,
) -> None:
    key = (id(bot), sheet_id)
    existing = _startup_sync_tasks.get(key)
    if existing is not None and not existing.done():
        log.info("Live Arena startup refresh already scheduled; keeping existing task")
        return

    task = asyncio.create_task(
        _run_startup_sync(
            manager,
            organizer,
            qualification_installed,
            refresh_qualification_state,
            reconcile_qualification_publication,
        ),
        name=f"live-arena-startup-sync:{sheet_id[-6:]}",
    )
    _startup_sync_tasks[key] = task

    def _done(done_task: asyncio.Task) -> None:
        if _startup_sync_tasks.get(key) is done_task:
            _startup_sync_tasks.pop(key, None)
        if done_task.cancelled():
            return
        try:
            done_task.result()
        except Exception:
            # The worker is meant to contain its own failures. Keep a final guard so
            # an unexpected bug still leaves a useful production traceback.
            log.exception("❌ Live Arena deferred startup refresh task crashed")

    task.add_done_callback(_done)


async def _run_startup_sync(
    manager: LiveArenaPanelManager,
    organizer,
    qualification_installed: bool,
    refresh_qualification_state,
    reconcile_qualification_publication,
) -> None:
    await asyncio.sleep(_STARTUP_SYNC_DELAY_SECONDS)

    for attempt in range(1, _STARTUP_MAX_ATTEMPTS + 1):
        warnings: list[str] = []
        try:
            with sheet_read_scope() as reads:
                if qualification_installed:
                    try:
                        await refresh_qualification_state(organizer)
                    except Exception as exc:
                        if is_rate_limited_error(exc):
                            raise
                        log.exception("⚠️ Live Arena qualification startup state refresh failed")
                        warnings.append("qualification state")

                try:
                    result = await manager.sync()
                    if isinstance(result, PanelSyncResult) and not result.ok:
                        warnings.append("public panel")
                except Exception as exc:
                    if is_rate_limited_error(exc):
                        raise
                    log.exception("⚠️ Live Arena public panel startup refresh failed")
                    warnings.append("public panel")

                try:
                    result = await organizer.sync()
                    if isinstance(result, PanelSyncResult) and not result.ok:
                        warnings.append("organizer panel")
                except Exception as exc:
                    if is_rate_limited_error(exc):
                        raise
                    log.exception("⚠️ Live Arena organizer panel startup refresh failed")
                    warnings.append("organizer panel")

                if qualification_installed:
                    try:
                        publication_warnings = await reconcile_qualification_publication(
                            organizer
                        )
                        warnings.extend(publication_warnings)
                    except Exception as exc:
                        if is_rate_limited_error(exc):
                            raise
                        log.exception(
                            "⚠️ Live Arena qualification startup publication retry failed"
                        )
                        warnings.append("qualification publication")

                log.info(
                    "Live Arena startup refresh finished • attempt=%s • sheet_reads=%s • reused_reads=%s • warnings=%s",
                    attempt,
                    reads.misses,
                    reads.hits,
                    ", ".join(dict.fromkeys(warnings)) or "none",
                )
        except Exception as exc:
            if is_rate_limited_error(exc):
                if attempt < _STARTUP_MAX_ATTEMPTS:
                    log.warning(
                        "⚠️ Live Arena startup refresh hit Sheets quota; retrying after startup settles • attempt=%s/%s • delay=%ss • error=%s",
                        attempt,
                        _STARTUP_MAX_ATTEMPTS,
                        _STARTUP_RETRY_DELAY_SECONDS,
                        exc,
                    )
                    await asyncio.sleep(_STARTUP_RETRY_DELAY_SECONDS)
                    continue
                log.exception(
                    "❌ Live Arena startup refresh exhausted Sheets quota retries • attempts=%s",
                    _STARTUP_MAX_ATTEMPTS,
                )
                return
            log.exception("❌ Live Arena startup refresh failed unexpectedly")
            return

        if not warnings:
            return
        if attempt < _STARTUP_MAX_ATTEMPTS:
            log.warning(
                "⚠️ Live Arena startup refresh incomplete; retrying • attempt=%s/%s • delay=%ss • items=%s",
                attempt,
                _STARTUP_MAX_ATTEMPTS,
                _STARTUP_RETRY_DELAY_SECONDS,
                ", ".join(dict.fromkeys(warnings)),
            )
            await asyncio.sleep(_STARTUP_RETRY_DELAY_SECONDS)
            continue
        return


async def register_live_arena(bot):
    sheet_id = str(cfg.get("LIVE_ARENA_TOURNAMENT_SHEET_ID", "") or "").strip()
    if not sheet_id:
        return None
    manager = _managers.setdefault(
        (id(bot), sheet_id), LiveArenaPanelManager(bot, sheet_id)
    )
    from modules.community.live_arena.entry_views import RegistrationEntryView
    from modules.community.live_arena.organizer_panel import OrganizerPanelManager
    from modules.community.live_arena.qualification_lock import (
        install_qualification_roster_lock,
    )
    from modules.community.live_arena.qualification_panel import (
        install_qualification,
        reconcile_qualification_publication,
        refresh_qualification_state,
    )
    from modules.community.live_arena.views import ClosedTournamentView

    organizer = OrganizerPanelManager(bot, sheet_id, manager)
    qualification_installed = install_qualification(organizer)
    if qualification_installed:
        install_qualification_roster_lock(organizer)

    # Wire every persistent interaction immediately. Sheet reads and panel refreshes
    # are deliberately deferred so Live Arena cannot add to the first-minute startup
    # quota spike or prevent unrelated ready wiring from completing.
    manager.organizer_manager = organizer
    bot.add_view(RegistrationEntryView(manager))
    bot.add_view(ClosedTournamentView(manager))
    bot.add_view(organizer.view())

    _schedule_startup_sync(
        bot,
        sheet_id,
        manager,
        organizer,
        qualification_installed,
        refresh_qualification_state,
        reconcile_qualification_publication,
    )
    log.info(
        "Live Arena persistent controls registered; Sheet refresh deferred by %ss",
        _STARTUP_SYNC_DELAY_SECONDS,
    )
    return manager
