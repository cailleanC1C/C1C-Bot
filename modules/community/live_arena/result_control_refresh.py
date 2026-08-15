"""Low-read result-control refresh and quota hardening for Live Arena.

Result/dispute mutations already return the authoritative updated MATCHES row.
Keep that row in task-local context so the affected Duelling Deck controls can be
updated immediately without reading Google Sheets again. Expensive whole-
tournament reconciliation is coalesced into a short background debounce window,
so a burst of result reports produces one broad sync instead of one per click.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextvars import Context, ContextVar
from dataclasses import dataclass

import discord

from shared.config import cfg
from shared.sheets.async_core import afetch_values, sheet_read_scope
from shared.sheets.core import is_rate_limited_error

from modules.community.live_arena.competition_resolution import CompetitionResolutionService
from modules.community.live_arena.messages import MESSAGE_HEADERS, load_pr5_config
from modules.community.live_arena.service import _enabled, _rows, _text

log = logging.getLogger("c1c.community.live_arena.result_control_refresh")
_installed = False
_mutation_channel: ContextVar[object | None] = ContextVar(
    "live_arena_result_mutation_channel", default=None
)
_mutation_match: ContextVar[dict[str, object] | None] = ContextVar(
    "live_arena_result_mutation_match", default=None
)
_original_post_mutation_sync = None
_base_error_embed = None
_broad_sync_tasks: dict[str, asyncio.Task] = {}
_broad_sync_dirty: dict[str, bool] = {}
_BROAD_SYNC_DEBOUNCE_SECONDS = 5.0


@dataclass(frozen=True)
class _QuotaCopy:
    title: str
    description: str
    color: int

    def embed(self) -> discord.Embed:
        return discord.Embed(
            title=self.title,
            description=self.description,
            color=self.color,
        )


_quota_copy: dict[str, _QuotaCopy] = {}
_active_sheet_id = ""


def _control_state(match_status: str) -> tuple[bool, bool]:
    """Return report_disabled, dispute_disabled for one persisted match state."""
    status = str(match_status or "").strip().lower()
    report_disabled = status not in {"published", "open"}
    dispute_disabled = status != "pending_confirmation"
    return report_disabled, dispute_disabled


async def _refresh_channel_controls(
    channel,
    sheet_id: str,
    match: dict[str, object] | None = None,
) -> None:
    """Rewrite the starter controls, using mutation state without another read."""
    from modules.community.live_arena import result_views

    channel_id = _text(getattr(channel, "id", ""))
    if not channel_id:
        raise RuntimeError("Live Arena result-control refresh requires a thread ID")

    # Normal result/dispute paths pass the row returned by the successful Sheet
    # mutation. Keep a read fallback for repair/tests and any future caller that
    # has only a thread reference.
    current = dict(match) if match is not None else None
    if current is None:
        service = CompetitionResolutionService(str(sheet_id))
        await service.initialize()
        current = await service.match_for_thread(channel_id)

    report_disabled, dispute_disabled = _control_state(
        _text(current.get("status"))
    )

    starter = None
    get_partial = getattr(channel, "get_partial_message", None)
    if callable(get_partial):
        starter = get_partial(int(channel_id))
    if starter is None:
        fetch_message = getattr(channel, "fetch_message", None)
        if callable(fetch_message):
            starter = await fetch_message(int(channel_id))
    if starter is None:
        raise RuntimeError("Duelling Deck starter message could not be resolved")

    await starter.edit(
        view=result_views.MatchResultView(
            str(sheet_id),
            report_disabled=report_disabled,
            dispute_disabled=dispute_disabled,
        )
    )


async def _sync_with_targeted_control_refresh(sheet_id: str) -> None:
    """Refresh one mutated thread immediately, then schedule one broad sync."""
    channel = _mutation_channel.get()
    match = _mutation_match.get()
    _mutation_channel.set(None)
    _mutation_match.set(None)

    if channel is not None:
        try:
            await _refresh_channel_controls(channel, str(sheet_id), match)
        except Exception:
            log.exception(
                "Live Arena immediate result-control refresh failed • thread=%s",
                _text(getattr(channel, "id", "")),
            )

    _schedule_broad_sync(str(sheet_id))


def _schedule_broad_sync(sheet_id: str) -> None:
    """Coalesce rapid result mutations into one whole-tournament reconciliation."""
    sid = str(sheet_id)
    if not callable(_original_post_mutation_sync):
        return

    _broad_sync_dirty[sid] = True
    existing = _broad_sync_tasks.get(sid)
    if existing is not None and not existing.done():
        return

    # Result reporting normally runs inside sheet_read_scope(). Start the delayed
    # worker in a fresh Context so it cannot inherit and keep using that completed
    # interaction's short-lived read cache.
    task = Context().run(
        asyncio.create_task,
        _broad_sync_worker(sid),
        name=f"live-arena-post-result-sync:{sid}",
    )
    _broad_sync_tasks[sid] = task


async def _broad_sync_worker(sheet_id: str) -> None:
    sid = str(sheet_id)
    try:
        while True:
            await asyncio.sleep(_BROAD_SYNC_DEBOUNCE_SECONDS)
            # Mutations that happened during the debounce are already represented
            # by the Sheet state this sync is about to read. Only a mutation that
            # happens *during* the sync needs a trailing pass.
            _broad_sync_dirty[sid] = False
            try:
                with sheet_read_scope():
                    await _original_post_mutation_sync(sid)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception(
                    "Live Arena coalesced post-result reconciliation failed • sheet=%s",
                    sid,
                )
            if not _broad_sync_dirty.get(sid, False):
                break
    finally:
        current = asyncio.current_task()
        if _broad_sync_tasks.get(sid) is current:
            _broad_sync_tasks.pop(sid, None)
        _broad_sync_dirty.pop(sid, None)


def _wrap_notice(original):
    async def notice_with_mutation_channel(channel, *args, **kwargs):
        _mutation_channel.set(channel)
        return await original(channel, *args, **kwargs)

    return notice_with_mutation_channel


def _wrap_service_mutation(original):
    async def mutation_with_result_capture(self, *args, **kwargs):
        updated = await original(self, *args, **kwargs)
        if updated is not None:
            _mutation_match.set(dict(updated))
        return updated

    return mutation_with_result_capture


async def _load_quota_copy(sheet_id: str) -> None:
    """Preload the quota message so a 429 never requires another Sheet read."""
    global _active_sheet_id

    sid = str(sheet_id or "").strip()
    if not sid:
        return
    config, _ = await load_pr5_config(sid)
    tab = config["MESSAGES_TAB"]
    rows = _rows(await afetch_values(sid, tab) or [], MESSAGE_HEADERS, tab)
    matches = [
        row
        for row in rows
        if _text(row["message_key"]) == "sheets_quota_retry"
        and _enabled(row["active"])
    ]
    if len(matches) != 1:
        raise RuntimeError(
            "MESSAGES: required active row missing or duplicated: sheets_quota_retry"
        )
    row = matches[0]
    color_text = _text(row["color_hex"])
    if len(color_text) != 7 or not color_text.startswith("#"):
        raise RuntimeError("MESSAGES.sheets_quota_retry: color_hex must be #RRGGBB")
    try:
        color = int(color_text[1:], 16)
    except ValueError as exc:
        raise RuntimeError(
            "MESSAGES.sheets_quota_retry: color_hex must be #RRGGBB"
        ) from exc

    title = _text(row["title"])
    description = _text(row["description"])
    if "{" in title + description or "}" in title + description:
        raise RuntimeError(
            "MESSAGES.sheets_quota_retry must not contain placeholders"
        )
    _quota_copy[sid] = _QuotaCopy(title, description, color)
    _active_sheet_id = sid


def _safe_error_embed(message: object) -> discord.Embed:
    """Never expose raw Google quota payloads to a Live Arena user."""
    if isinstance(message, BaseException) and is_rate_limited_error(message):
        copy = _quota_copy.get(_active_sheet_id)
        if copy is not None:
            return copy.embed()
        # Emergency fallback only when startup could not preload MESSAGES. This
        # reuses the existing generic Live Arena wording rather than the provider
        # exception payload.
        return _base_error_embed("Something went wrong. Please try again later.")
    return _base_error_embed(message)


def _patch_live_arena_error_embeds() -> None:
    """Replace imported aliases of views.error_embed across loaded Live Arena modules."""
    global _base_error_embed

    from modules.community.live_arena import views

    _base_error_embed = views.error_embed
    views.error_embed = _safe_error_embed
    for module_name, module in list(sys.modules.items()):
        if not module_name.startswith("modules.community.live_arena"):
            continue
        if getattr(module, "error_embed", None) is _base_error_embed:
            setattr(module, "error_embed", _safe_error_embed)


def install() -> None:
    """Install after all reporting/rendering wrappers so every result flow uses it."""
    global _installed
    global _original_post_mutation_sync
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import (
        panel,
        result_views,
        simulation_ux_hardening as ux,
    )

    # Preload the friendly quota copy inside the same short-lived startup read
    # scope as the existing matchup-copy loader. CONFIG/MESSAGES reads therefore
    # deduplicate instead of adding another startup API request.
    original_register = panel.register_live_arena

    async def register_with_quota_copy(bot):
        sid = str(cfg.get("LIVE_ARENA_TOURNAMENT_SHEET_ID", "") or "").strip()
        with sheet_read_scope():
            if sid:
                await _load_quota_copy(sid)
            return await original_register(bot)

    panel.register_live_arena = register_with_quota_copy

    # Capture the authoritative row returned by the Sheet mutation. This removes
    # the extra initialize()+match_for_thread() read from the immediate refresh.
    CompetitionResolutionService.report_result = _wrap_service_mutation(
        CompetitionResolutionService.report_result
    )
    CompetitionResolutionService.dispute_result = _wrap_service_mutation(
        CompetitionResolutionService.dispute_result
    )

    # Both player/organizer reporting and disputes post a thread notice immediately
    # before calling result_views._run_post_mutation_sync. Capture that exact thread
    # so the returned mutation row can update its starter controls in place.
    ux_notice = ux._post_thread_notice
    result_notice = result_views._post_thread_notice
    wrapped_ux_notice = _wrap_notice(ux_notice)
    ux._post_thread_notice = wrapped_ux_notice
    if result_notice is ux_notice:
        result_views._post_thread_notice = wrapped_ux_notice
    else:
        result_views._post_thread_notice = _wrap_notice(result_notice)

    _original_post_mutation_sync = result_views._run_post_mutation_sync
    result_views._run_post_mutation_sync = _sync_with_targeted_control_refresh

    _patch_live_arena_error_embeds()
