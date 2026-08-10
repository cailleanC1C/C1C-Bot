"""Persistent public panel lifecycle for Live Arena."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

import discord

from shared.config import cfg
from shared.sheets.async_core import sheet_read_scope
from shared.sheets.core import is_rate_limited_error

from modules.community.live_arena.messages import (
    discord_timestamp,
    load_messages,
    load_pr3_config,
)
from modules.community.live_arena.repository import LiveArenaRepository
from modules.community.live_arena.service import _text, load_tournament_snapshot

log = logging.getLogger("c1c.community.live_arena.panel")
_sync_locks: dict[str, asyncio.Lock] = {}
_managers: dict[tuple[int, str], "LiveArenaPanelManager"] = {}
_startup_sync_tasks: dict[tuple[int, str], asyncio.Task] = {}

_STARTUP_SYNC_DELAY_SECONDS = 75
_STARTUP_RETRY_DELAY_SECONDS = 90
_STARTUP_MAX_ATTEMPTS = 3


@dataclass(frozen=True)
class PanelSyncResult:
    """Outcome of a panel sync, including failures deliberately handled here."""

    ok: bool
    operation: str = ""


def _now_utc() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class LiveArenaPanelManager:
    def __init__(self, bot, sheet_id: str, service_factory=None):
        self.bot = bot
        self.sheet_id = sheet_id
        self.service_factory = service_factory
        self._lock = _sync_locks.setdefault(sheet_id, asyncio.Lock())

    async def sync(self) -> PanelSyncResult:
        async with self._lock:
            config, _ = await load_pr3_config(self.sheet_id)
            tournament = await load_tournament_snapshot(self.sheet_id)
            if tournament.status in {"draft", "archived"}:
                return PanelSyncResult(True)
            if tournament.status not in {
                "signup_open",
                "signup_closed",
                "active",
                "completed",
            }:
                return PanelSyncResult(True)

            key = "signup_open" if tournament.status == "signup_open" else "signup_closed"
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

            resource = await repository.discord_resource(
                tournament.tournament_id, "signup_panel", "main"
            )
            if resource is not None and _text(resource["state"]) == "retired":
                return PanelSyncResult(True)
            registry_message_id = _text(resource["message_id"]) if resource else ""
            legacy_message_id = _text(config.get("PUBLIC_PANEL_MESSAGE_ID", ""))
            message_id = registry_message_id or legacy_message_id
            using_legacy = bool(message_id and not registry_message_id)

            from modules.community.live_arena.entry_views import RegistrationEntryView
            from modules.community.live_arena.views import ClosedTournamentView

            view = (
                RegistrationEntryView(self)
                if key == "signup_open"
                else ClosedTournamentView(self)
            )

            message = None
            if message_id:
                get_partial_message = getattr(channel, "get_partial_message", None)
                if callable(get_partial_message):
                    try:
                        message = get_partial_message(int(message_id))
                    except Exception as exc:
                        log.exception(
                            "❌ Live Arena panel — direct edit target failed • message_id=%s • error=%s: %s",
                            message_id,
                            type(exc).__name__,
                            exc,
                        )
                        return PanelSyncResult(False, "edit")
                else:
                    try:
                        message = await channel.fetch_message(int(message_id))
                    except discord.NotFound:
                        message = None
                    except Exception as exc:
                        log.exception(
                            "❌ Live Arena panel — fetch failed • message_id=%s • error=%s: %s",
                            message_id,
                            type(exc).__name__,
                            exc,
                        )
                        return PanelSyncResult(False, "fetch")

            if message is not None:
                try:
                    await message.edit(embed=embed, view=view)
                except discord.NotFound:
                    log.warning(
                        "⚠️ Live Arena panel — saved message missing; recreating • tournament=%s • message_id=%s",
                        tournament.tournament_id,
                        message_id,
                    )
                except Exception as exc:
                    log.exception(
                        "❌ Live Arena panel — edit failed • message_id=%s • error=%s: %s",
                        message_id,
                        type(exc).__name__,
                        exc,
                    )
                    return PanelSyncResult(False, "edit")
                else:
                    if using_legacy or resource is None:
                        now = _now_utc()
                        await repository.upsert_discord_resource(
                            tournament_id=tournament.tournament_id,
                            resource_type="signup_panel",
                            resource_key="main",
                            channel_id=str(channel.id),
                            message_id=message_id,
                            created_at_utc=(
                                _text(resource["created_at_utc"]) if resource else now
                            ),
                            updated_at_utc=now,
                            state="active",
                            notes="Migrated from legacy PUBLIC_PANEL_MESSAGE_ID."
                            if using_legacy
                            else _text(resource["notes"]) if resource else "",
                        )
                    return PanelSyncResult(True)

            created = await channel.send(embed=embed, view=view)
            try:
                now = _now_utc()
                await repository.upsert_discord_resource(
                    tournament_id=tournament.tournament_id,
                    resource_type="signup_panel",
                    resource_key="main",
                    channel_id=str(channel.id),
                    message_id=str(created.id),
                    created_at_utc=(
                        _text(resource["created_at_utc"]) if resource else now
                    ),
                    updated_at_utc=now,
                    state="active",
                    notes=_text(resource["notes"]) if resource else "",
                )
            except Exception:
                log.exception("❌ Live Arena panel — resource persistence failed")
                try:
                    await created.delete()
                except Exception:
                    log.exception("⚠️ Live Arena panel — untracked message cleanup failed")
                raise
            return PanelSyncResult(True)


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

                log.info(
                    "Live Arena startup panel refresh finished • attempt=%s • sheet_reads=%s • reused_reads=%s • warnings=%s",
                    attempt,
                    reads.misses,
                    reads.hits,
                    ", ".join(dict.fromkeys(warnings)) or "none",
                )

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
