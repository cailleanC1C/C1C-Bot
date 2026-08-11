"""Runtime wiring for the complete Live Arena PR 6B-1 competition flow."""

from __future__ import annotations

import asyncio
import logging
from types import MethodType

import discord

from shared.theme import colors

from modules.community.live_arena.competition import calculate_qualification_standings
from modules.community.live_arena.competition_admin import (
    ReopenClosedRoundButton,
    ReviewResultIssuesButton,
)
from modules.community.live_arena.competition_resolution import CompetitionResolutionService
from modules.community.live_arena.organizer_panel import OrganizerView
from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.result_views import (
    MatchResultView,
    restore_pending_result_finalizers,
    set_post_mutation_sync,
)
from modules.community.live_arena.service import _text, load_config
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.runtime_hooks")
_installed = False
_restore_tasks: dict[str, asyncio.Task] = {}

_ROUND_PUBLIC_STATUSES = {
    "active",
    "published",
    "open",
    "published/open",
    "ready_to_close",
    "closed",
    "correction_in_progress",
}


def install() -> None:
    """Install result, review, closure, correction, and Discord repair wiring."""
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

        async def competition_sync():
            return await _sync_manager_competition(manager)

        manager._competition_sync = competition_sync
        set_post_mutation_sync(manager.sheet_id, competition_sync)

        add_view = getattr(manager.bot, "add_view", None)
        if callable(add_view):
            add_view(MatchResultView(manager.sheet_id))
            _schedule_restore(manager.sheet_id)

        base_view = manager.view

        def view(status=None):
            result = base_view(status)
            add_item = getattr(result, "add_item", None)
            if not callable(add_item):
                return result
            competition_disabled = status is not None and status != "active"
            add_item(
                CloseCurrentRoundButton(
                    manager,
                    disabled=competition_disabled,
                )
            )
            add_item(
                ReviewResultIssuesButton(
                    manager,
                    disabled=competition_disabled,
                )
            )
            add_item(
                ReopenClosedRoundButton(
                    manager,
                    disabled=competition_disabled,
                )
            )
            return result

        manager.view = view
        return True

    async def reconcile_with_result_controls(publisher):
        sheet_id = str(getattr(publisher.service, "sheet_id", "") or "").strip()
        if not sheet_id:
            return list(await original_reconcile(publisher))

        warnings: list[str] = []
        try:
            snapshot = await publisher.service.snapshot()
            if snapshot.round_row is None:
                return warnings
            if snapshot.status == "active":
                warnings.extend(await original_reconcile(publisher))
                snapshot = await publisher.service.snapshot()
            if snapshot.status not in _ROUND_PUBLIC_STATUSES:
                return list(dict.fromkeys(warnings))
            warnings.extend(
                await _sync_round_discord(publisher.bot, publisher.service, snapshot)
            )
        except Exception:
            log.exception("Live Arena competition Discord reconciliation failed")
            warnings.append("competition Discord state")
        return list(dict.fromkeys(warnings))

    qualification_panel.install_qualification = install_qualification_with_results
    qualification_panel.QualificationPublisher.reconcile = reconcile_with_result_controls


async def _sync_manager_competition(manager) -> list[str]:
    from modules.community.live_arena import qualification_panel

    factory = getattr(manager, "qualification_service_factory", None)
    service = factory(manager.sheet_id) if factory is not None else qualification_panel.QualificationService(manager.sheet_id)
    await service.initialize()
    return await qualification_panel.QualificationPublisher(manager.bot, service).reconcile()


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
            service = CompetitionResolutionService(self.manager.sheet_id)
            await service.initialize()
            rounds = await service.repository.rounds()
            base = await load_config(self.manager.sheet_id)
            tid = base["ACTIVE_TOURNAMENT_ID"]
            closable = [
                row
                for row in rounds
                if _text(row.get("tournament_id")) == tid
                and _text(row.get("status")) in {"ready_to_close", "correction_in_progress"}
            ]
            if len(closable) != 1:
                raise RegistrationError(
                    "Exactly one round must be ready to close or in correction before using this action"
                )
            closed = await service.close_round(
                str(interaction.user.id), _text(closable[0]["round_id"])
            )
            warnings = await _best_effort_competition_sync(self.manager)
            try:
                await self.manager.sync()
            except Exception:
                log.exception("Live Arena organizer panel refresh after round close failed")
                warnings.append("organizer panel")
            embed = discord.Embed(
                title="Round closed",
                description=(
                    f"**{_text(closed['round_name'])}** is now closed. "
                    "Its finalized results are locked for normal operations."
                ),
                color=colors.c1c_blue,
            )
            if warnings:
                embed.add_field(
                    name="Sync warning",
                    value=(
                        "The Sheet state is saved, but these Discord items need repair:\n"
                        + "\n".join(f"• {item}" for item in dict.fromkeys(warnings))
                    )[:1024],
                    inline=False,
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            log.exception("Live Arena round close action failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


async def _best_effort_competition_sync(manager) -> list[str]:
    sync = getattr(manager, "_competition_sync", None)
    if not callable(sync):
        return []
    try:
        return list(await sync())
    except Exception:
        log.exception("Live Arena competition sync failed")
        return ["competition Discord state"]


async def _sync_round_discord(bot, qualification_service, snapshot) -> list[str]:
    """Repair the round forward from Sheet truth and reflect current result state."""
    from modules.community.live_arena import qualification_panel

    warnings: list[str] = []
    sheet_id = qualification_service.sheet_id
    round_status = snapshot.status
    matches = [dict(row) for row in snapshot.matches]

    for match in matches:
        thread_id = _text(match.get("thread_id"))
        if not thread_id:
            continue
        try:
            thread = bot.get_channel(int(thread_id))
            if thread is None:
                thread = await bot.fetch_channel(int(thread_id))
            report_disabled = not (
                round_status in {"active", "published", "open", "published/open", "correction_in_progress"}
                and _text(match.get("status")) in {"published", "open"}
            )
            dispute_disabled = not (
                round_status in {"active", "published", "open", "published/open", "correction_in_progress"}
                and _text(match.get("status")) == "pending_confirmation"
            )
            await _ensure_match_result_view(
                thread,
                sheet_id,
                report_disabled=report_disabled,
                dispute_disabled=dispute_disabled,
            )
        except Exception:
            log.exception(
                "Live Arena result-control reconciliation failed • match=%s",
                _text(match.get("match_id")),
            )
            warnings.append(f"Match {_text(match.get('match_number'))} result controls")

    try:
        config = qualification_service.repository.config
        overview_channel = await qualification_panel._resolve_channel(
            bot, int(config["ROUND_OVERVIEW_CHANNEL_ID"])
        )
        _, (_, tournament), _, _ = await qualification_service.context()
        standings = []
        if _text(snapshot.round_row.get("round_stage")).lower() == "qualification":
            competition_service = CompetitionResolutionService(sheet_id)
            await competition_service.initialize()
            standings = await competition_service.standings()
        embed = _competition_overview_embed(
            tournament,
            snapshot.round_row,
            matches,
            standings,
        )
        overview_id = _text(snapshot.round_row.get("overview_message_id"))
        message = None
        if overview_id:
            try:
                message = await overview_channel.fetch_message(int(overview_id))
            except discord.NotFound:
                message = None
        if message is not None:
            await message.edit(embed=embed)
        else:
            created = await overview_channel.send(embed=embed)
            try:
                await qualification_service.record_overview_message_id(
                    _text(snapshot.round_row["round_id"]), str(created.id)
                )
            except Exception:
                try:
                    await created.delete()
                except Exception:
                    log.exception("Live Arena untracked overview cleanup failed")
                raise
    except Exception:
        log.exception("Live Arena competition overview synchronization failed")
        warnings.append("Victory Ledger overview")
    return warnings


def _competition_overview_embed(tournament, round_row, matches, standings):
    from modules.community.live_arena import qualification_panel

    deadline = qualification_panel._format_timestamp(
        _text(round_row["deadline_at_utc"]), "F"
    )
    terminal = {"finalized", "forfeit", "double_forfeit", "bye"}
    completed = sum(_text(row.get("status")) in terminal for row in matches)
    status = _text(round_row.get("status"))
    state_line = {
        "active": "Round is open",
        "published": "Round is open",
        "open": "Round is open",
        "published/open": "Round is open",
        "ready_to_close": "All matchups are final · ready for organizer closure",
        "closed": "Round closed",
        "correction_in_progress": "⚠️ Correction in progress · next-round publication blocked",
    }.get(status, status.replace("_", " ").title())
    embed = discord.Embed(
        title=_text(round_row["round_name"]),
        description=(
            f"**{_text(tournament['tournament_name'])}**\n"
            f"**State:** {state_line}\n"
            f"Round deadline: {deadline}\n"
            f"Completed: **{completed} / {len(matches)}**"
        ),
        color=colors.c1c_blue,
    )
    for match in sorted(matches, key=lambda row: int(_text(row["match_number"]))):
        thread_id = _text(match.get("thread_id"))
        location = f"<#{thread_id}>" if thread_id else "Forum post pending"
        result = _public_match_result(match)
        embed.add_field(
            name=f"Match {_text(match['match_number'])}",
            value=(
                f"<@{_text(match['player_a_discord_user_id'])}> vs "
                f"<@{_text(match['player_b_discord_user_id'])}>\n"
                f"{result}\n{location}"
            ),
            inline=False,
        )
    if standings:
        lines = [
            f"**#{entry.rank}** <@{entry.discord_user_id}> · **{entry.match_record}**"
            for entry in standings
        ]
        embed.add_field(
            name="Qualification standings",
            value="\n".join(lines)[:1024] or "No finalized results yet.",
            inline=False,
        )
    return embed


def _public_match_result(match) -> str:
    status = _text(match.get("status"))
    if status == "finalized":
        return f"Final: **{_text(match['final_score_a'])}-{_text(match['final_score_b'])}**"
    if status == "forfeit":
        winner = _text(match.get("final_winner_discord_user_id"))
        return f"Final: **forfeit** · winner <@{winner}>" if winner else "Final: **forfeit**"
    if status == "double_forfeit":
        return "Final: **double forfeit**"
    if status == "bye":
        return "Final: **bye**"
    if status == "pending_confirmation":
        return "Result reported · objection window open"
    if status == "disputed":
        return "⚠️ Result disputed · organizer review"
    if status == "late_review":
        return "⏰ Late result · organizer review"
    return "Result pending"


def _schedule_restore(sheet_id: str) -> None:
    existing = _restore_tasks.get(sheet_id)
    if existing is not None and not existing.done():
        return

    async def runner():
        await asyncio.sleep(105)
        try:
            count = await restore_pending_result_finalizers(sheet_id)
            if count:
                log.info("Live Arena restored pending result timers • count=%s", count)
        except Exception:
            log.exception("Live Arena pending-result timer restore failed")

    task = asyncio.create_task(
        runner(), name=f"live-arena-result-restore:{sheet_id[-6:]}"
    )
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


async def _ensure_match_result_view(
    thread,
    sheet_id: str,
    *,
    report_disabled: bool = False,
    dispute_disabled: bool = False,
) -> None:
    """Attach state-aware persistent controls to the forum starter message."""
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
    await starter.edit(
        view=MatchResultView(
            sheet_id,
            report_disabled=report_disabled,
            dispute_disabled=dispute_disabled,
        )
    )
