"""Immediate, thread-local result-control refresh for Live Arena.

Result reporting and disputes already persist the correct Sheet state first. This
layer makes the affected Duelling Deck starter view reflect that new state before
the broader tournament reconciliation runs, so a successful mutation never leaves
stale Report/Dispute/Scheduling controls visible to players.
"""

from __future__ import annotations

import logging
from contextvars import ContextVar

from modules.community.live_arena.competition_resolution import CompetitionResolutionService
from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.result_control_refresh")
_installed = False
_mutation_channel: ContextVar[object | None] = ContextVar(
    "live_arena_result_mutation_channel", default=None
)
_original_post_mutation_sync = None


def _control_state(match_status: str) -> tuple[bool, bool]:
    """Return report_disabled, dispute_disabled for one persisted match state."""
    status = str(match_status or "").strip().lower()
    report_disabled = status not in {"published", "open"}
    dispute_disabled = status != "pending_confirmation"
    return report_disabled, dispute_disabled


async def _refresh_channel_controls(channel, sheet_id: str) -> None:
    """Re-read the affected match and rewrite only its starter-message controls."""
    from modules.community.live_arena import result_views

    channel_id = _text(getattr(channel, "id", ""))
    if not channel_id:
        raise RuntimeError("Live Arena result-control refresh requires a thread ID")

    service = CompetitionResolutionService(str(sheet_id))
    await service.initialize()
    match = await service.match_for_thread(channel_id)
    report_disabled, dispute_disabled = _control_state(_text(match.get("status")))

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
    """Refresh the mutated thread first, then keep the broad sync as a safety net."""
    channel = _mutation_channel.get()
    _mutation_channel.set(None)
    if channel is not None:
        try:
            await _refresh_channel_controls(channel, str(sheet_id))
        except Exception:
            log.exception(
                "Live Arena immediate result-control refresh failed • thread=%s",
                _text(getattr(channel, "id", "")),
            )

    if callable(_original_post_mutation_sync):
        await _original_post_mutation_sync(str(sheet_id))


def _wrap_notice(original):
    async def notice_with_mutation_channel(channel, *args, **kwargs):
        _mutation_channel.set(channel)
        return await original(channel, *args, **kwargs)

    return notice_with_mutation_channel


def install() -> None:
    """Install after all result/reporting wrappers so every mutation uses it."""
    global _installed
    global _original_post_mutation_sync
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import result_views, simulation_ux_hardening as ux

    # Both player/organizer reporting and disputes post a thread notice immediately
    # before calling result_views._run_post_mutation_sync. Capture that exact thread
    # in the current task so the sync can repair it first without changing any
    # reporting/dispute business logic.
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
