"""Runtime wiring for Live Arena competition controls without duplicating panel code."""

from __future__ import annotations

import asyncio
import logging

import discord

from modules.community.live_arena.competition import LiveArenaCompetitionService
from modules.community.live_arena.organizer_panel import OrganizerView
from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.result_views import (
    MatchResultView,
    restore_pending_result_finalizers,
)
from modules.community.live_arena.service import _text
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.runtime_hooks")
_installed = False
_restore_tasks: dict[str, asyncio.Task] = {}


def install() -> None:
    """Install narrow wrappers around the existing organizer/Q1 runtime."""
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import qualification_panel

    original_install = qualification_panel.install_qualification
    original_reconcile = qualification_panel.QualificationPublisher.reconcile

    def install_qualification_with_results(manager) -> bool:
        installed = original_install(manager)
        if not installed:
            return False
        if getattr(manager, "_competition_results_installed", False):
            return True
        manager._competition_results_installed = True

        manager.bot.add_view(MatchResultView(manager.sheet_id))
        _schedule_restore(manager.sheet_id)

        base_view = manager.view

        def view(status=None):
            result = base_view(status)
            result.add_item(
                CloseCurrentRoundButton(
                    manager,
                    disabled=status is not None and status != "active",
                )
            )
            return result

        manager.view = view
        return True

    async def reconcile_with_result_controls(publisher):
        warnings = list(await original_reconcile(publisher))
        try:
            snapshot = await publisher.service.snapshot()
            if snapshot.round_row is None or snapshot.status != "active":
                return warnings
            for match in snapshot.matches:
                thread_id = _text(match.get("thread_id"))
                if not thread_id:
                    continue
                try:
                    thread = publisher.bot.get_channel(int(thread_id))
                    if thread is None:
                        thread = await publisher.bot.fetch_channel(int(thread_id))
                    await _ensure_match_result_view(thread, publisher.service.sheet_id)
                except Exception:
                    log.exception(
                        "Live Arena result control reconciliation failed • match=%s",
                        _text(match.get("match_id")),
                    )
                    warnings.append(
                        f"Match {_text(match.get('match_number'))} result controls"
                    )
        except Exception:
            log.exception("Live Arena result-control reconciliation failed")
            warnings.append("match result controls")
        return list(dict.fromkeys(warnings))

    qualification_panel.install_qualification = install_qualification_with_results
    qualification_panel.QualificationPublisher.reconcile = reconcile_with_result_controls


class CloseCurrentRoundButton(discord.ui.Button):
    def __init__(self, manager, *, disabled=False):
        super().__init__(
            label="Close Current Round",
            custom_id="live_arena:organizer:round:close_current",
            style=discord.ButtonStyle.primary,
            disabled=disabled,
        )
        self.manager = manager

    async def callback(self, interaction):
        if not await OrganizerView(self.manager).authorized(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            service = LiveArenaCompetitionService(self.manager.sheet_id)
            await service.initialize()
            rounds = await service.repository.rounds()
            base = await __import__(
                "modules.community.live_arena.service", fromlist=["load_config"]
            ).load_config(self.manager.sheet_id)
            tid = base["ACTIVE_TOURNAMENT_ID"]
            ready = [
                row
                for row in rounds
                if _text(row.get("tournament_id")) == tid
                and _text(row.get("status")) == "ready_to_close"
            ]
            if len(ready) != 1:
                raise RegistrationError(
                    "Exactly one round must be ready to close before using this action"
                )
            closed = await service.close_round(
                str(interaction.user.id), _text(ready[0]["round_id"])
            )
            try:
                await self.manager.sync()
            except Exception:
                log.exception("Live Arena organizer panel refresh after round close failed")
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Round closed",
                    description=(
                        f"**{_text(closed['round_name'])}** is now closed. "
                        "Its finalized results are locked for normal operations."
                    ),
                    color=0x3498DB,
                ),
                ephemeral=True,
            )
        except Exception as exc:
            log.exception("Live Arena round close action failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


def _schedule_restore(sheet_id: str) -> None:
    existing = _restore_tasks.get(sheet_id)
    if existing is not None and not existing.done():
        return

    async def runner():
        # Keep the existing Live Arena startup read budget intact.
        await asyncio.sleep(105)
        try:
            count = await restore_pending_result_finalizers(sheet_id)
            if count:
                log.info("Live Arena restored pending result timers • count=%s", count)
        except Exception:
            log.exception("Live Arena pending-result timer restore failed")

    task = asyncio.create_task(runner(), name=f"live-arena-result-restore:{sheet_id[-6:]}")
    _restore_tasks[sheet_id] = task

    def _done(done: asyncio.Task) -> None:
        if _restore_tasks.get(sheet_id) is done:
            _restore_tasks.pop(sheet_id, None)
        if done.cancelled():
            return
        try:
            done.result()
        except Exception:
            log.exception("Live Arena result restore task crashed")

    task.add_done_callback(_done)


async def _ensure_match_result_view(thread, sheet_id: str) -> None:
    """Attach the persistent controls to the forum starter message idempotently."""
    starter = None
    get_partial = getattr(thread, "get_partial_message", None)
    if callable(get_partial):
        starter = get_partial(int(thread.id))
    else:
        fetch_message = getattr(thread, "fetch_message", None)
        if callable(fetch_message):
            starter = await fetch_message(int(thread.id))
    if starter is None:
        raise RuntimeError("Duelling Deck starter message could not be resolved")
    await starter.edit(view=MatchResultView(sheet_id))
