"""Staff-only fallback command for manually finishing onboarding placements."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

import discord
from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only, is_admin_member, is_recruiter, is_staff_member
from modules.onboarding import thread_scopes
from modules.onboarding.watcher_welcome import (
    TicketContext,
    WelcomeTicketWatcher,
    _NO_PLACEMENT_TAG,
    parse_promo_thread_name,
    parse_welcome_thread_name,
)
from modules.onboarding.watcher_promo import PromoTicketContext, PromoTicketWatcher
from shared.sheets import onboarding as onboarding_sheets
from shared.sheets import onboarding_sessions

log = logging.getLogger("c1c.onboarding.finishplacement")
_TICKET_BACKFILL_LOCK: asyncio.Lock | None = None


def _ticket_backfill_lock() -> asyncio.Lock:
    global _TICKET_BACKFILL_LOCK
    if _TICKET_BACKFILL_LOCK is None:
        _TICKET_BACKFILL_LOCK = asyncio.Lock()
    return _TICKET_BACKFILL_LOCK


def _parse_backfill_window(value: str | None) -> tuple[int | None, str | None]:
    text = str(value or "").strip().lower()
    if not text:
        return None, "Missing window. Usage: `!ticketbackfill <welcome|promo|all> <1h|6h|24h|3d|7d>` (max 7d)."
    match = re.fullmatch(r"(\d+)([hd])", text)
    if not match:
        return None, "Invalid window. Use hours or days such as `1h`, `6h`, `24h`, `3d`, or `7d` (max 7d)."
    amount = int(match.group(1))
    unit = match.group(2)
    hours = amount if unit == "h" else amount * 24
    if hours <= 0:
        return None, "Invalid window. Window must be greater than zero."
    if hours > 7 * 24:
        return None, "Window too large. Normal ticket backfill is capped at 7d; rerun with a smaller explicit window."
    return hours, None


def _format_backfill_summary(flow: str, window: str, summary: dict[str, int]) -> str:
    keys = (
        "scanned",
        "finalized",
        "prompt_required",
        "already_done",
        "unresolved",
        "skipped_old",
        "skipped_no_timestamp",
        "error",
    )
    parts = [f"flow={flow}", f"window={window}"]
    parts.extend(f"{key}={int(summary.get(key, 0))}" for key in keys)
    return " • ".join(parts)


Flow = Literal["welcome", "promo"]


@dataclass(slots=True)
class FinishPlacementContext:
    flow: Flow
    ticket_id: str
    username: str
    user_id: int | None = None
    prompt_message_id: int | None = None
    promo_type: str = ""
    thread_created: str = ""
    year: str = ""
    month: str = ""
    join_month: str = ""
    clan_name: str = ""
    progression: str = ""
    existing_clan_tag: str = ""
    existing_source_clan_tag: str = ""
    status: str = ""
    date_closed: str = ""
    source: str = ""
    lookup_keys: list[str] | None = None


def _upper_tag(value: str | None) -> str:
    return str(value or "").strip().upper()


def _is_none_source(value: str | None) -> bool:
    return _upper_tag(value) in {"", _NO_PLACEMENT_TAG}


def _safe_int(value: Any) -> int | None:
    try:
        if value is None or str(value).strip() == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _merge_finalized_state_from_ticket(
    ctx: FinishPlacementContext,
) -> FinishPlacementContext:
    """Best-effort idempotency refresh by full ticket id before finalization."""

    try:
        if ctx.flow == "welcome":
            found = onboarding_sheets.find_welcome_row(ctx.ticket_id)
            if found:
                _row, values = found
                header = list(getattr(onboarding_sheets, "WELCOME_HEADERS", []))
                mapped = {
                    header[idx]: values[idx] if idx < len(values) else ""
                    for idx in range(len(header))
                }
                ctx.existing_clan_tag = (
                    ctx.existing_clan_tag or str(mapped.get("clantag") or "").strip()
                )
                ctx.status = ctx.status or str(mapped.get("status") or "").strip()
                ctx.date_closed = (
                    ctx.date_closed or str(mapped.get("date_closed") or "").strip()
                )
        else:
            found = onboarding_sheets.find_promo_row(ctx.ticket_id)
            if found:
                _row, mapped = found
                ctx.existing_clan_tag = (
                    ctx.existing_clan_tag or str(mapped.get("clantag") or "").strip()
                )
                ctx.status = ctx.status or str(mapped.get("status") or "").strip()
                ctx.date_closed = (
                    ctx.date_closed
                    or str(
                        mapped.get("date closed") or mapped.get("date_closed") or ""
                    ).strip()
                )
    except Exception:
        log.exception(
            "finishplacement idempotency ticket refresh failed",
            extra={"ticket_id": ctx.ticket_id, "flow": ctx.flow},
        )
    return ctx


def _is_finalized(ctx: FinishPlacementContext) -> bool:
    if str(ctx.status or "").strip().lower() in {
        "closed",
        "complete",
        "completed",
        "finalized",
    }:
        return True
    if str(ctx.date_closed or "").strip() and _upper_tag(ctx.existing_clan_tag):
        return True
    return False


def _welcome_row_context(thread_id: int) -> FinishPlacementContext | None:
    found = onboarding_sheets.find_welcome_row_by_thread_id(thread_id)
    if not found:
        return None
    _row_number, values = found
    return FinishPlacementContext(
        flow="welcome",
        ticket_id=str(
            values.get("ticket_number") or values.get("ticket") or ""
        ).strip(),
        username=str(values.get("username") or "").strip(),
        user_id=_safe_int(values.get("user_id")),
        prompt_message_id=_safe_int(values.get("panel_message_id")),
        existing_clan_tag=str(values.get("clantag") or "").strip(),
        status=str(values.get("status") or "").strip(),
        date_closed=str(
            values.get("date_closed") or values.get("dateclosed") or ""
        ).strip(),
        source="sheet_thread_id",
        lookup_keys=[f"welcome.thread_id={thread_id}"],
    )


def _promo_row_context(thread_id: int) -> FinishPlacementContext | None:
    found = onboarding_sheets.find_promo_row_by_thread_id(thread_id)
    if not found:
        return None
    _row_number, values = found
    source_header = ""
    try:
        source_header = onboarding_sheets.get_promo_source_clan_tag_header()
    except Exception:
        source_header = "source_clan_tag"
    return FinishPlacementContext(
        flow="promo",
        ticket_id=str(
            values.get("ticket number")
            or values.get("ticket_number")
            or values.get("ticket")
            or ""
        ).strip(),
        username=str(values.get("username") or "").strip(),
        user_id=_safe_int(values.get("user_id")),
        prompt_message_id=_safe_int(values.get("panel_message_id")),
        promo_type=str(values.get("type") or "").strip(),
        thread_created=str(
            values.get("thread created") or values.get("thread_created") or ""
        ).strip(),
        year=str(values.get("year") or "").strip(),
        month=str(values.get("month") or "").strip(),
        join_month=str(values.get("join_month") or "").strip(),
        clan_name=str(values.get("clan name") or values.get("clan_name") or "").strip(),
        progression=str(values.get("progression") or "").strip(),
        existing_clan_tag=str(values.get("clantag") or "").strip(),
        existing_source_clan_tag=str(
            values.get(source_header) or values.get("source_clan_tag") or ""
        ).strip(),
        status=str(values.get("status") or "").strip(),
        date_closed=str(
            values.get("date closed") or values.get("date_closed") or ""
        ).strip(),
        source="sheet_thread_id",
        lookup_keys=[f"promo.thread_id={thread_id}"],
    )


def _session_context(
    thread: discord.Thread, flow: Flow
) -> FinishPlacementContext | None:
    row = onboarding_sessions.get_by_thread_id(getattr(thread, "id", None))
    if not row:
        return None
    thread_name = str(row.get("thread_name") or getattr(thread, "name", "") or "")
    if flow == "promo":
        parts = parse_promo_thread_name(thread_name)
        if not parts:
            return None
        return FinishPlacementContext(
            flow="promo",
            ticket_id=parts.ticket_code,
            username=parts.username,
            user_id=_safe_int(row.get("user_id")),
            prompt_message_id=_safe_int(row.get("panel_message_id")),
            promo_type=parts.promo_type,
            source="onboarding_session_thread_id",
            lookup_keys=[f"sessions.thread_id={getattr(thread, 'id', None)}"],
        )
    parts = parse_welcome_thread_name(thread_name)
    if not parts:
        return None
    return FinishPlacementContext(
        flow="welcome",
        ticket_id=parts.ticket_code,
        username=parts.username,
        user_id=_safe_int(row.get("user_id")),
        prompt_message_id=_safe_int(row.get("panel_message_id")),
        source="onboarding_session_thread_id",
        lookup_keys=[f"sessions.thread_id={getattr(thread, 'id', None)}"],
    )


def _thread_name_context(
    thread: discord.Thread, flow: Flow
) -> FinishPlacementContext | None:
    now = getattr(thread, "created_at", None) or dt.datetime.now(dt.timezone.utc)
    if flow == "promo":
        parts = parse_promo_thread_name(getattr(thread, "name", None))
        if not parts:
            return None
        return FinishPlacementContext(
            flow="promo",
            ticket_id=parts.ticket_code,
            username=parts.username,
            promo_type=parts.promo_type,
            thread_created=now.isoformat(),
            year=str(now.year),
            month=now.strftime("%B"),
            existing_clan_tag=parts.clan_tag or "",
            source="thread_name",
            lookup_keys=[f"thread.name={getattr(thread, 'name', '')}"],
        )
    parts = parse_welcome_thread_name(getattr(thread, "name", None))
    if not parts:
        return None
    return FinishPlacementContext(
        flow="welcome",
        ticket_id=parts.ticket_code,
        username=parts.username,
        existing_clan_tag=parts.clan_tag or "",
        source="thread_name",
        lookup_keys=[f"thread.name={getattr(thread, 'name', '')}"],
    )


class FinishPlacementCog(commands.Cog):
    """Staff fallback command that delegates to the normal close finalizers."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _authorized(self, member: Any) -> bool:
        return bool(
            is_staff_member(member) or is_admin_member(member) or is_recruiter(member)
        )

    async def _valid_tags(self) -> set[str]:
        tags = await asyncio.to_thread(onboarding_sheets.load_clan_tags)
        return {
            str(tag or "").strip().upper() for tag in tags if str(tag or "").strip()
        }

    async def _resolve_context(
        self, thread: discord.Thread, flow: Flow
    ) -> FinishPlacementContext | None:
        lookup_keys = [f"thread_id={getattr(thread, 'id', None)}"]
        if flow == "welcome":
            watcher = self.bot.get_cog("WelcomeTicketWatcher")
            if isinstance(watcher, WelcomeTicketWatcher):
                existing = watcher._tickets.get(thread.id)
                if existing is not None:
                    return FinishPlacementContext(
                        flow="welcome",
                        ticket_id=existing.ticket_number,
                        username=existing.username,
                        user_id=existing.recruit_id,
                        prompt_message_id=existing.prompt_message_id,
                        existing_clan_tag=existing.final_clan or "",
                        status=existing.state,
                        source="memory_thread_id",
                        lookup_keys=lookup_keys,
                    )
            for resolver in (_welcome_row_context,):
                ctx = await asyncio.to_thread(resolver, thread.id)
                if ctx:
                    return ctx
        else:
            watcher = self.bot.get_cog("PromoTicketWatcher")
            if isinstance(watcher, PromoTicketWatcher):
                existing = watcher._tickets.get(thread.id)
                if existing is not None:
                    return FinishPlacementContext(
                        flow="promo",
                        ticket_id=existing.ticket_number,
                        username=existing.username,
                        user_id=existing.user_id,
                        prompt_message_id=existing.prompt_message_id,
                        promo_type=existing.promo_type,
                        thread_created=existing.thread_created,
                        year=existing.year,
                        month=existing.month,
                        join_month=existing.join_month,
                        clan_name=existing.clan_name,
                        progression=existing.progression,
                        existing_clan_tag=existing.clan_tag,
                        existing_source_clan_tag=existing.source_clan_tag,
                        status=existing.state,
                        source="memory_thread_id",
                        lookup_keys=lookup_keys,
                    )
            ctx = await asyncio.to_thread(_promo_row_context, thread.id)
            if ctx:
                return ctx

        ctx = await asyncio.to_thread(_session_context, thread, flow)
        if ctx:
            return ctx
        # Ticket helper/log table currently shares the Welcome sheet in this codebase;
        # the sheet-by-thread lookup above covers it when thread_id is present.
        return _thread_name_context(thread, flow)

    @tier("staff")
    @help_metadata(
        function_group="recruitment", section="onboarding", access_tier="staff"
    )
    @commands.command(
        name="finishplacement",
        usage="<source_clan_tag|NONE> <destination_clan_tag>",
        help="Staff fallback: manually finish a welcome or promo placement in the current ticket thread.",
        brief="Manually finish placement for the current onboarding ticket.",
    )
    async def finishplacement(
        self,
        ctx: commands.Context,
        source_clan_tag: str | None = None,
        destination_clan_tag: str | None = None,
    ) -> None:
        thread = ctx.channel if isinstance(ctx.channel, discord.Thread) else None
        actor_id = getattr(getattr(ctx, "author", None), "id", None)
        log.info(
            "finishplacement_invoked",
            extra={"thread_id": getattr(thread, "id", None), "actor_id": actor_id},
        )

        if not self._authorized(getattr(ctx, "author", None)):
            log.info(
                "finishplacement_unauthorized",
                extra={"thread_id": getattr(thread, "id", None), "actor_id": actor_id},
            )
            await ctx.reply("Staff only.", mention_author=False)
            return

        if source_clan_tag is None or destination_clan_tag is None:
            await ctx.reply(
                "Usage: `!finishplacement <source_clan_tag|NONE> <destination_clan_tag>`",
                mention_author=False,
            )
            return

        if thread is None:
            log.info(
                "finishplacement_wrong_thread_type",
                extra={
                    "channel_id": getattr(ctx.channel, "id", None),
                    "actor_id": actor_id,
                },
            )
            await ctx.reply(
                "Use this inside a welcome or promo ticket thread.",
                mention_author=False,
            )
            return

        if thread_scopes.is_welcome_parent(thread):
            flow: Flow = "welcome"
        elif thread_scopes.is_promo_parent(thread):
            flow = "promo"
        else:
            log.info(
                "finishplacement_wrong_thread_type",
                extra={
                    "thread_id": thread.id,
                    "parent_channel_id": getattr(thread, "parent_id", None),
                    "actor_id": actor_id,
                },
            )
            await ctx.reply(
                "Use this inside a welcome or promo ticket thread.",
                mention_author=False,
            )
            return

        source_tag = _upper_tag(source_clan_tag)
        destination_tag = _upper_tag(destination_clan_tag)
        if not destination_tag or destination_tag == _NO_PLACEMENT_TAG:
            await ctx.reply("Destination clan tag is required.", mention_author=False)
            return

        valid_tags = await self._valid_tags()
        if destination_tag not in valid_tags or (
            not _is_none_source(source_tag) and source_tag not in valid_tags
        ):
            await ctx.reply(
                "Unknown clan tag. Please use configured clan tags or `NONE` for source.",
                mention_author=False,
            )
            return

        if flow == "welcome" and not _is_none_source(source_tag):
            log.warning(
                "finishplacement_wrong_thread_type welcome_source_rejected",
                extra={
                    "thread_id": thread.id,
                    "source_clan_tag": source_tag,
                    "destination_clan_tag": destination_tag,
                    "actor_id": actor_id,
                },
            )
            await ctx.reply(
                "Welcome tickets only support `NONE` as the source clan.",
                mention_author=False,
            )
            return

        context = await self._resolve_context(thread, flow)
        if context is None or not context.ticket_id or not context.username:
            attempted = [
                f"thread_id={thread.id}",
                f"channel_name={getattr(thread, 'name', '')}",
                "memory",
                "sheet_thread_id",
                "onboarding_session",
                "ticket_helper",
                "thread_name",
            ]
            log.error(
                "finishplacement_context_unresolved",
                extra={
                    "thread_id": thread.id,
                    "channel_name": getattr(thread, "name", ""),
                    "parent_channel_id": getattr(thread, "parent_id", None),
                    "actor_id": actor_id,
                    "attempted_lookup_keys": attempted,
                    "reason": "missing_ticket_or_username",
                },
            )
            await ctx.reply(
                "I couldn't resolve this ticket context. Please verify the thread name or sheet row and try again.",
                mention_author=False,
            )
            return

        context = await asyncio.to_thread(_merge_finalized_state_from_ticket, context)
        log.info(
            "finishplacement_context_resolved",
            extra={
                "thread_id": thread.id,
                "ticket_id": context.ticket_id,
                "flow": flow,
                "source": context.source,
            },
        )
        if _is_finalized(context):
            log.info(
                "finishplacement_already_finalized",
                extra={
                    "thread_id": thread.id,
                    "ticket_id": context.ticket_id,
                    "flow": flow,
                },
            )
            await ctx.reply(
                "This ticket already appears finalized.", mention_author=False
            )
            return

        log.info(
            "finishplacement_started",
            extra={
                "thread_id": thread.id,
                "ticket_id": context.ticket_id,
                "flow": flow,
                "source_clan_tag": source_tag,
                "destination_clan_tag": destination_tag,
            },
        )
        if flow == "welcome":
            watcher = self.bot.get_cog("WelcomeTicketWatcher")
            if not isinstance(watcher, WelcomeTicketWatcher):
                await ctx.reply(
                    "Welcome ticket finalizer is unavailable.", mention_author=False
                )
                return
            ticket_context = watcher._tickets.get(thread.id) or TicketContext(
                thread_id=thread.id,
                ticket_number=context.ticket_id,
                username=context.username,
                recruit_id=context.user_id,
                recruit_display=context.username,
                prompt_message_id=context.prompt_message_id,
            )
            ticket_context.close_source = "finishplacement"
            watcher._tickets[thread.id] = ticket_context
            await watcher._finalize_clan_tag(
                thread,
                ticket_context,
                destination_tag,
                actor=getattr(ctx, "author", None),
                source="finishplacement",
                prompt_message=None,
                view=None,
                notify=False,
                rename_thread=True,
                sheet_phase="finishplacement",
            )
            if ticket_context.state == "closed":
                await ctx.reply(
                    f"Placement finalized: **{destination_tag}**.", mention_author=False
                )
                log.info(
                    "finishplacement_completed",
                    extra={
                        "thread_id": thread.id,
                        "ticket_id": context.ticket_id,
                        "flow": flow,
                    },
                )
            return

        watcher = self.bot.get_cog("PromoTicketWatcher")
        if not isinstance(watcher, PromoTicketWatcher):
            await ctx.reply(
                "Promo ticket finalizer is unavailable.", mention_author=False
            )
            return
        now = getattr(thread, "created_at", None) or dt.datetime.now(dt.timezone.utc)
        promo_context = watcher._tickets.get(thread.id) or PromoTicketContext(
            thread_id=thread.id,
            ticket_number=context.ticket_id,
            username=context.username,
            promo_type=context.promo_type or "promo.m",
            thread_created=context.thread_created or now.isoformat(),
            year=context.year or str(now.year),
            month=context.month or now.strftime("%B"),
            join_month=context.join_month,
            clan_name=context.clan_name,
            progression=context.progression,
            user_id=context.user_id,
            prompt_message_id=context.prompt_message_id,
        )
        promo_context.ticket_number = context.ticket_id
        promo_context.username = context.username
        promo_context.source_clan_tag = (
            _NO_PLACEMENT_TAG if _is_none_source(source_tag) else source_tag
        )
        promo_context.clan_tag = destination_tag
        promo_context.state = "awaiting_destination_clan"
        watcher._tickets[thread.id] = promo_context
        await watcher._complete_close(
            thread,
            promo_context,
            progression=promo_context.progression,
            clan_name=promo_context.clan_name,
            phase="finishplacement",
            previous_final=promo_context.source_clan_tag,
        )
        if promo_context.state == "closed":
            await ctx.reply(
                f"Placement finalized: **{source_tag or _NO_PLACEMENT_TAG}** → **{destination_tag}**.",
                mention_author=False,
            )
            log.info(
                "finishplacement_completed",
                extra={
                    "thread_id": thread.id,
                    "ticket_id": context.ticket_id,
                    "flow": flow,
                },
            )

    @tier("admin")
    @help_metadata(function_group="operational", section="onboarding", access_tier="admin")
    @commands.command(
        name="ticketbackfill",
        usage="<welcome|promo|all> <1h|6h|24h|3d|7d>",
        help="Admin repair: manually run welcome/promo ticket close backfill for an explicit bounded window.",
        brief="Manually run bounded ticket close backfill.",
    )
    @admin_only()
    async def ticketbackfill(
        self, ctx: commands.Context, flow: str | None = None, window: str | None = None
    ) -> None:
        requested_flow = str(flow or "").strip().lower()
        if requested_flow not in {"welcome", "promo", "all"}:
            await ctx.reply(
                "Invalid flow. Usage: `!ticketbackfill <welcome|promo|all> <1h|6h|24h|3d|7d>`.",
                mention_author=False,
            )
            return
        window_hours, error = _parse_backfill_window(window)
        if error or window_hours is None:
            await ctx.reply(error, mention_author=False)
            return

        lock = _ticket_backfill_lock()
        if lock.locked():
            await ctx.reply(
                "A ticket close backfill is already running. Wait for it to finish before starting another.",
                mention_author=False,
            )
            return

        flows = ["welcome", "promo"] if requested_flow == "all" else [requested_flow]
        summaries: list[str] = []
        async with lock:
            for item in flows:
                watcher_name = "WelcomeTicketWatcher" if item == "welcome" else "PromoTicketWatcher"
                watcher = self.bot.get_cog(watcher_name)
                if watcher is None or not hasattr(watcher, "run_close_backfill"):
                    summaries.append(
                        _format_backfill_summary(item, str(window), {"error": 1}) + " • reason=watcher_not_loaded"
                    )
                    continue
                try:
                    summary = await watcher.run_close_backfill(window_hours=window_hours)
                except Exception as exc:
                    log.exception("manual_ticket_backfill_failed", extra={"flow": item, "window_hours": window_hours})
                    summaries.append(
                        _format_backfill_summary(item, str(window), {"error": 1})
                        + f" • reason={type(exc).__name__}"
                    )
                    continue
                summaries.append(_format_backfill_summary(item, str(window), summary))

        await ctx.reply(
            "Ticket close backfill complete:\n" + "\n".join(f"• {line}" for line in summaries),
            mention_author=False,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FinishPlacementCog(bot))
