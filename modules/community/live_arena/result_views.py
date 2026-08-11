"""Persistent match-result controls and low-read finalization scheduling."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime

import discord

from shared.theme import colors

from modules.community.live_arena.competition_resolution import CompetitionResolutionService
from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena.service import _text
from modules.community.live_arena.views import error_embed

log = logging.getLogger("c1c.community.live_arena.result_views")
_finalizer_tasks: dict[tuple[str, str], asyncio.Task] = {}
_post_mutation_syncs: dict[str, object] = {}


def set_post_mutation_sync(sheet_id: str, callback) -> None:
    """Register the runtime's best-effort Discord reconciliation callback."""
    _post_mutation_syncs[str(sheet_id)] = callback


class MatchResultView(discord.ui.View):
    """Persistent controls; the current thread resolves the exact MATCHES row."""

    def __init__(
        self,
        sheet_id: str,
        *,
        report_disabled: bool = False,
        dispute_disabled: bool = False,
    ):
        super().__init__(timeout=None)
        self.sheet_id = sheet_id
        for item in self.children:
            custom_id = getattr(item, "custom_id", "")
            if custom_id == "live_arena:match:report_result":
                item.disabled = report_disabled
            elif custom_id == "live_arena:match:dispute_result":
                item.disabled = dispute_disabled

    @discord.ui.button(
        label="Report Result",
        style=discord.ButtonStyle.success,
        custom_id="live_arena:match:report_result",
    )
    async def report_result(self, interaction: discord.Interaction, _button):
        try:
            service = CompetitionResolutionService(self.sheet_id)
            await service.initialize()
            match = await service.match_for_thread(str(interaction.channel_id))
            actor = str(interaction.user.id)
            if actor not in {
                _text(match["player_a_discord_user_id"]),
                _text(match["player_b_discord_user_id"]),
            }:
                raise RegistrationError(
                    "Only the two players in this matchup can report its result"
                )
            await interaction.response.send_modal(
                ReportResultModal(self.sheet_id, _text(match["match_id"]))
            )
        except Exception as exc:
            log.exception("Live Arena result report preflight failed")
            if interaction.response.is_done():
                await interaction.followup.send(embed=error_embed(exc), ephemeral=True)
            else:
                await interaction.response.send_message(
                    embed=error_embed(exc), ephemeral=True
                )

    @discord.ui.button(
        label="Dispute Result",
        style=discord.ButtonStyle.danger,
        custom_id="live_arena:match:dispute_result",
    )
    async def dispute_result(self, interaction: discord.Interaction, _button):
        await interaction.response.defer(ephemeral=True)
        try:
            service = CompetitionResolutionService(self.sheet_id)
            await service.initialize()
            match = await service.match_for_thread(str(interaction.channel_id))
            updated = await service.dispute_result(
                str(interaction.user.id), _text(match["match_id"])
            )
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Result disputed",
                    description=(
                        "The reported result is frozen and will not affect standings "
                        "until an organizer resolves it."
                    ),
                    color=colors.c1c_blue,
                ),
                ephemeral=True,
            )
            await _post_thread_notice(
                interaction.channel,
                (
                    f"⚠️ Result disputed by <@{interaction.user.id}>. "
                    "This matchup is frozen pending organizer review."
                ),
            )
            cancel_match_finalizer(self.sheet_id, _text(updated["match_id"]))
            await _run_post_mutation_sync(self.sheet_id)
        except Exception as exc:
            log.exception("Live Arena result dispute failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


class ReportResultModal(discord.ui.Modal, title="Report Match Result"):
    score = discord.ui.TextInput(
        label="Final series score",
        placeholder="2-0 or 2-1",
        min_length=3,
        max_length=5,
    )

    def __init__(self, sheet_id: str, match_id: str):
        super().__init__(timeout=300)
        self.sheet_id = sheet_id
        self.match_id = match_id

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            match_service = CompetitionResolutionService(self.sheet_id)
            await match_service.initialize()
            match = await match_service.match_for_thread(str(interaction.channel_id))
            if _text(match["match_id"]) != self.match_id:
                raise RegistrationError(
                    "This match thread no longer matches the result form"
                )
            score_a, score_b = _score_for_sheet_sides(
                str(self.score.value),
                str(interaction.user.id),
                match,
            )
            screenshot_present = await _thread_has_result_screenshot(
                interaction.channel,
                {
                    _text(match["player_a_discord_user_id"]),
                    _text(match["player_b_discord_user_id"]),
                },
            )
            updated = await match_service.report_result(
                str(interaction.user.id),
                self.match_id,
                score_a,
                score_b,
                screenshot_present=screenshot_present,
            )
            status = _text(updated["status"])
            if status == "pending_confirmation":
                schedule_match_finalization(
                    self.sheet_id,
                    self.match_id,
                    _text(updated["confirm_due_at_utc"]),
                )
                description = (
                    f"Recorded **{_text(updated['reported_score_a'])}-"
                    f"{_text(updated['reported_score_b'])}**. Your opponent can "
                    "dispute it until the objection window closes."
                )
                notice = (
                    f"📜 Result reported by <@{interaction.user.id}>: "
                    f"**{_text(updated['reported_score_a'])}-"
                    f"{_text(updated['reported_score_b'])}**. The opponent may "
                    "dispute before the confirmation deadline."
                )
            else:
                description = (
                    f"Recorded **{_text(updated['reported_score_a'])}-"
                    f"{_text(updated['reported_score_b'])}** after the round deadline. "
                    "It is waiting for organizer review and does not affect standings yet."
                )
                notice = (
                    f"⏰ Late result reported by <@{interaction.user.id}>: "
                    f"**{_text(updated['reported_score_a'])}-"
                    f"{_text(updated['reported_score_b'])}**. Organizer review is required."
                )
            await interaction.followup.send(
                embed=discord.Embed(
                    title="Result reported",
                    description=description,
                    color=colors.c1c_blue,
                ),
                ephemeral=True,
            )
            await _post_thread_notice(interaction.channel, notice)
            await _run_post_mutation_sync(self.sheet_id)
        except Exception as exc:
            log.exception("Live Arena result report failed")
            await interaction.followup.send(embed=error_embed(exc), ephemeral=True)


def schedule_match_finalization(
    sheet_id: str, match_id: str, confirm_due_at_utc: str
) -> None:
    key = (str(sheet_id), str(match_id))
    old = _finalizer_tasks.get(key)
    if old is not None and not old.done():
        old.cancel()
    task = asyncio.create_task(
        _finalize_when_due(str(sheet_id), str(match_id), str(confirm_due_at_utc)),
        name=f"live-arena-result-finalize:{match_id}",
    )
    _finalizer_tasks[key] = task

    def _done(done: asyncio.Task) -> None:
        if _finalizer_tasks.get(key) is done:
            _finalizer_tasks.pop(key, None)
        if done.cancelled():
            return
        try:
            done.result()
        except Exception:
            log.exception("Live Arena result finalizer crashed • match=%s", match_id)

    task.add_done_callback(_done)


def cancel_match_finalizer(sheet_id: str, match_id: str) -> None:
    task = _finalizer_tasks.pop((str(sheet_id), str(match_id)), None)
    if task is not None and not task.done():
        task.cancel()


async def restore_pending_result_finalizers(sheet_id: str) -> int:
    """Restore exact per-match timers after restart without a recurring Sheets poll."""
    service = CompetitionResolutionService(sheet_id)
    await service.initialize()
    rows = await service.repository.matches()
    count = 0
    for row in rows:
        if _text(row.get("status")) != "pending_confirmation":
            continue
        match_id = _text(row.get("match_id"))
        due = _text(row.get("confirm_due_at_utc"))
        if not match_id or not due:
            continue
        schedule_match_finalization(sheet_id, match_id, due)
        count += 1
    return count


async def _finalize_when_due(sheet_id: str, match_id: str, due_text: str) -> None:
    due = _parse_utc(due_text)
    delay = max(0.0, (due - datetime.now(UTC)).total_seconds())
    if delay:
        await asyncio.sleep(delay)
    service = CompetitionResolutionService(sheet_id)
    await service.initialize()
    result = await service.finalize_match_if_due(match_id)
    if result is not None:
        log.info("Live Arena result auto-finalized • match=%s", match_id)
        await _run_post_mutation_sync(sheet_id)


async def _thread_has_result_screenshot(channel, player_ids: set[str]) -> bool:
    history = getattr(channel, "history", None)
    if not callable(history):
        return False
    async for message in history(limit=100):
        author_id = str(getattr(getattr(message, "author", None), "id", ""))
        if author_id not in player_ids:
            continue
        for attachment in getattr(message, "attachments", ()):
            content_type = str(
                getattr(attachment, "content_type", "") or ""
            ).lower()
            filename = str(getattr(attachment, "filename", "") or "").lower()
            if content_type.startswith("image/") or filename.endswith(
                (".png", ".jpg", ".jpeg", ".webp")
            ):
                return True
    return False


def _score_for_sheet_sides(raw: str, reporter_id: str, match) -> tuple[int, int]:
    cleaned = str(raw or "").strip().replace("–", "-").replace("—", "-")
    parts = [part.strip() for part in cleaned.split("-")]
    if len(parts) != 2:
        raise RegistrationError("Enter the final score like 2-0 or 2-1")
    try:
        reporter_score, opponent_score = int(parts[0]), int(parts[1])
    except ValueError as exc:
        raise RegistrationError("Enter the final score like 2-0 or 2-1") from exc
    a = _text(match["player_a_discord_user_id"])
    b = _text(match["player_b_discord_user_id"])
    if reporter_id == a:
        return reporter_score, opponent_score
    if reporter_id == b:
        return opponent_score, reporter_score
    raise RegistrationError("Only a player in this matchup can report its result")


def _parse_utc(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise RegistrationError("Match confirmation deadline is invalid") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


async def _post_thread_notice(channel, content: str) -> None:
    try:
        await channel.send(
            content,
            allowed_mentions=discord.AllowedMentions(
                users=True, roles=False, everyone=False
            ),
        )
    except Exception:
        log.exception("Live Arena matchup thread notice failed")


async def _run_post_mutation_sync(sheet_id: str) -> None:
    callback = _post_mutation_syncs.get(str(sheet_id))
    if not callable(callback):
        return
    try:
        await callback()
    except Exception:
        log.exception("Live Arena post-result Discord reconciliation failed")
