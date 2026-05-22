"""Promo ticket watcher that logs lifecycle events to Sheets."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from time import monotonic
from typing import Dict, List, Optional, Tuple

import discord
from discord.ext import commands

from modules.common import feature_flags
from modules.common import runtime as rt
from modules.recruitment import availability
from modules.onboarding.constants import CLAN_TAG_PROMPT_HELPER
from modules.onboarding import logs as onboarding_logs
from modules.onboarding import thread_scopes
from modules.onboarding.controllers.welcome_controller import (
    extract_target_from_message,
    locate_welcome_message,
)
from modules.onboarding.sessions import ensure_session_for_thread
from modules.onboarding.watcher_welcome import (
    _NO_PLACEMENT_TAG,
    _channel_readable_label,
    _decision_visibility_line,
    _determine_reservation_decision,
    PanelOutcome,
    parse_promo_thread_name,
    persist_session_for_thread,
    post_open_questions_panel,
    resolve_subject_user_id,
)
from modules.onboarding.sheet_logging import log_sheet_write
from modules.onboarding.ui import panels
from shared.config import get_promo_channel_id, get_ticket_tool_bot_id
from shared.logs import log_lifecycle
from shared.sheets import onboarding as onboarding_sheets
from shared.sheets import onboarding_sessions
from shared.sheets import promo_tickets
from shared.sheets import recruitment as recruitment_sheets
from shared.sheets.onboarding import PROMO_HEADERS

UTC = dt.timezone.utc
log = logging.getLogger("c1c.onboarding.promo_watcher")
_CLOSED_MESSAGE_TOKEN = "ticket closed"
_PROMO_TRIGGER_MAP: Dict[str, str] = {
    "<!-- trigger:promo.r -->": "promo.r",
    "<!-- trigger:promo.m -->": "promo.m",
    "<!-- trigger:promo.l -->": "promo.l",
}
_PROMO_TRIGGER_LABELS: Dict[str, str] = {
    "promo.r": "Returning player",
    "promo.m": "Player move request",
    "promo.l": "Clan lead move request",
}


async def _send_runtime(message: str) -> None:
    try:
        await rt.send_log_message(message)
    except Exception:  # pragma: no cover - runtime notification best-effort
        log.warning("failed to send promo watcher log message", exc_info=True)


@dataclass(slots=True)
class PromoTicketContext:
    thread_id: int
    ticket_number: str
    username: str
    promo_type: str
    thread_created: str
    year: str
    month: str
    join_month: str = ""
    clan_tag: str = ""
    clan_name: str = ""
    progression: str = ""
    state: str = "open"
    prompt_message_id: Optional[int] = None
    close_detected: bool = False
    user_id: Optional[int] = None


class PromoClanSelect(discord.ui.Select):
    def __init__(self, parent_view: "PromoClanSelectView", tags: List[str]) -> None:
        options = [discord.SelectOption(label=tag, value=tag) for tag in tags[:25]]
        placeholder = "Select a clan tag"
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)
        self._parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # pragma: no cover - UI callback
        if not self.values:
            await interaction.response.defer()
            return
        await self._parent_view.handle_selection(interaction, self.values[0])


class PromoClanSelectView(discord.ui.View):
    def __init__(self, watcher: "PromoTicketWatcher", context: PromoTicketContext, tags: List[str]):
        super().__init__(timeout=300)
        self.watcher = watcher
        self.context = context
        self.message: Optional[discord.Message] = None
        self.select = PromoClanSelect(self, tags)
        self.add_item(self.select)

    async def handle_selection(self, interaction: discord.Interaction, tag: str) -> None:
        await interaction.response.defer()
        await self.watcher.finalize_from_interaction(self.context, tag, interaction, self)

    async def on_timeout(self) -> None:  # pragma: no cover - timeout path
        if self.message is None:
            return
        for child in self.children:
            child.disabled = True
        try:
            await self.message.edit(view=self)
        except Exception:
            log.debug("promo tag picker timeout edit failed", exc_info=True)


def _transitioned_to_closed(before: discord.Thread, after: discord.Thread) -> bool:
    before_archived = bool(getattr(before, "archived", False))
    after_archived = bool(getattr(after, "archived", False))
    before_locked = bool(getattr(before, "locked", False))
    after_locked = bool(getattr(after, "locked", False))

    reopened = (before_archived and not after_archived) or (before_locked and not after_locked)
    if reopened:
        return False
    just_archived = (not before_archived) and after_archived
    just_locked = (not before_locked) and after_locked
    return just_archived or just_locked


def _format_timestamp(value: dt.datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def _format_date(value: dt.datetime) -> str:
    return value.astimezone(UTC).date().isoformat()


def _promo_trigger_from_content(content: str | None) -> Tuple[str | None, str | None]:
    text = content or ""
    for marker, flow in _PROMO_TRIGGER_MAP.items():
        if marker in text:
            return marker, flow
    return None, None


class PromoTicketWatcher(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        channel_id = get_promo_channel_id()
        try:
            self.channel_id = int(channel_id) if channel_id is not None else None
        except (TypeError, ValueError):
            self.channel_id = None
        self.ticket_tool_id = get_ticket_tool_bot_id()
        self._tickets: Dict[int, PromoTicketContext] = {}
        self._clan_tags: List[str] = []
        self._announced = False
        self._auto_closed_threads: set[int] = set()

        if self.channel_id is None:
            log.warning("promo ticket watcher disabled — invalid PROMO_CHANNEL_ID")

    @staticmethod
    def _features_enabled() -> bool:
        return feature_flags.is_enabled("promo_enabled") and feature_flags.is_enabled("enable_promo_hook")

    def _is_ticket_thread(self, thread: discord.Thread | None) -> bool:
        if thread is None:
            return False
        return thread_scopes.is_promo_parent(thread)

    def _is_ticket_tool(self, user: discord.abc.User | None) -> bool:
        if user is None:
            return False
        if self.ticket_tool_id is not None:
            return getattr(user, "id", None) == self.ticket_tool_id
        return False

    def _log_panel_outcome(
        self,
        actor: discord.abc.User | None,
        thread: discord.Thread,
        *,
        outcome: PanelOutcome,
        trigger: str | None,
        flow: str | None,
    ) -> None:
        actor_handle = onboarding_logs.format_actor_handle(actor) or "<unknown>"
        thread_ref = getattr(thread, "name", None) or getattr(thread, "id", None) or "<unknown>"
        emoji = "📘" if outcome.result == "panel_created" else "⚠️"

        payload: dict[str, object] = {
            "actor": actor_handle,
            "thread": thread_ref,
            "trigger": trigger,
            "flow": flow,
            "result": outcome.result,
            "ms": outcome.elapsed_ms,
        }
        if outcome.reason:
            payload["reason"] = outcome.reason

        log_lifecycle(
            log,
            "promo",
            "triggered",
            scope_label="Promo panel",
            emoji=emoji,
            dedupe=False,
            **payload,
        )

    def _log_missing_trigger(
        self,
        actor: discord.abc.User | None,
        thread: discord.Thread,
        *,
        reason: str,
        trigger: str | None,
        start: float,
    ) -> None:
        actor_handle = onboarding_logs.format_actor_handle(actor) or "<unknown>"
        thread_ref = getattr(thread, "name", None) or getattr(thread, "id", None) or "<unknown>"
        elapsed_ms = int((monotonic() - start) * 1000)

        payload: dict[str, object] = {
            "actor": actor_handle,
            "thread": thread_ref,
            "result": "skipped",
            "reason": reason,
            "ms": elapsed_ms,
        }
        if trigger:
            payload["trigger"] = trigger

        log_lifecycle(
            log,
            "promo",
            "triggered",
            scope_label="Promo panel",
            emoji="⚠️",
            dedupe=False,
            **payload,
        )

    async def _load_clan_tags(self) -> List[str]:
        if self._clan_tags:
            return self._clan_tags
        try:
            tags = await asyncio.to_thread(onboarding_sheets.load_clan_tags)
        except Exception:
            log.exception("failed to load clan tags for promo watcher")
            return []
        normalized = []
        seen = set()
        for tag in tags:
            cleaned = str(tag or "").strip().upper()
            if cleaned and cleaned not in seen:
                normalized.append(cleaned)
                seen.add(cleaned)
        self._clan_tags = normalized
        return self._clan_tags

    async def _ensure_context(self, thread: discord.Thread) -> Optional[PromoTicketContext]:
        context = self._tickets.get(thread.id)
        if context is not None:
            return context

        parts = parse_promo_thread_name(thread.name)
        if parts is None:
            log.warning(
                "promo_watcher: unable to parse ticket name", extra={"thread_id": getattr(thread, "id", None)}
            )
            return None

        now = getattr(thread, "created_at", None) or dt.datetime.now(UTC)
        created_str = _format_timestamp(now)
        context = PromoTicketContext(
            thread_id=thread.id,
            ticket_number=parts.ticket_code,
            username=parts.username,
            promo_type=parts.promo_type,
            thread_created=created_str,
            year=str(now.year),
            month=now.strftime("%B"),
        )

        try:
            found = await asyncio.to_thread(onboarding_sheets.find_promo_row, parts.ticket_code)
        except Exception:
            log.exception("failed to read promo row during context ensure", extra={"ticket": parts.ticket_code})
            found = None

        if found:
            _, values = found
            context.clan_tag = values.get("clantag", "") or context.clan_tag
            context.clan_name = values.get("clan name", "") or context.clan_name
            context.progression = values.get("progression", "") or context.progression
            context.thread_created = values.get("thread created", "") or context.thread_created
            context.year = values.get("year", "") or context.year
            context.month = values.get("month", "") or context.month
            context.join_month = values.get("join_month", "") or context.join_month

        self._tickets[thread.id] = context
        return context

    async def _log_ticket_open(
        self,
        thread: discord.Thread,
        context: PromoTicketContext,
        *,
        user_id: int | str | None = None,
        created_at: dt.datetime | None = None,
    ) -> None:
        created = created_at or getattr(thread, "created_at", None) or dt.datetime.now(UTC)
        try:
            result = await log_sheet_write(
                flow="promo",
                phase="created",
                tab="Promo",
                logger=log,
                thread=thread,
                user=context.username,
                write_coro=lambda: asyncio.to_thread(
                    onboarding_sheets.append_promo_ticket_row,
                    context.ticket_number,
                    context.username,
                    context.clan_tag,
                    context.promo_type,
                    context.thread_created,
                    context.year,
                    context.month,
                    context.join_month,
                    context.clan_name,
                    context.progression,
                    thread_name=getattr(thread, "name", ""),
                    user_id=user_id,
                    thread_id=int(getattr(thread, "id", 0)),
                    panel_message_id=None,
                    status="open",
                    created_at=created,
                ),
            )
            log.info(
                "promo_ticket_open — ticket=%s • user=%s • result=row_%s",
                context.ticket_number,
                context.username,
                result,
            )
        except Exception as exc:
            log.error(
                "promo_ticket_open — ticket=%s • user=%s • result=error • reason=%s",
                context.ticket_number,
                context.username,
                exc,
            )

    async def _ensure_row_initialized(self, thread: discord.Thread, context: PromoTicketContext) -> None:
        try:
            found = await asyncio.to_thread(onboarding_sheets.find_promo_row, context.ticket_number)
        except Exception:
            log.exception(
                "failed to locate promo row for closure",
                extra={"thread_id": getattr(thread, "id", None), "ticket": context.ticket_number},
            )
            found = None

        if found:
            _, values = found
            context.clan_tag = values.get("clantag", "") or context.clan_tag
            context.clan_name = values.get("clan name", "") or context.clan_name
            context.progression = values.get("progression", "") or context.progression
            context.thread_created = values.get("thread created", "") or context.thread_created
            context.year = values.get("year", "") or context.year
            context.month = values.get("month", "") or context.month
            context.join_month = values.get("join_month", "") or context.join_month
            return

        log.warning(
            "promo watcher could not locate ticket row for closure; skipping append",
            extra={"thread_id": getattr(thread, "id", None), "ticket": context.ticket_number},
        )

    async def _send_invalid_tag_notice(self, thread: discord.Thread, actor: discord.abc.User | None, candidate: str) -> None:
        notice = (
            "⚠️ That clan tag was not recognized. Please pick a tag from the menu or reply with a valid tag"
            " (e.g. C1CE)."
        )
        if actor is not None:
            try:
                await actor.send(notice)
                return
            except Exception:
                log.debug("failed to send invalid promo tag DM", exc_info=True)
        try:
            await thread.send(notice, delete_after=30)
        except Exception:
            log.debug("failed to send invalid promo tag notice", exc_info=True)

    async def _begin_clan_prompt(self, thread: discord.Thread, context: PromoTicketContext) -> None:
        tags = await self._load_clan_tags()
        if not tags:
            log.warning("promo watcher unable to load clan tags for close prompt", extra={"ticket": context.ticket_number})
            return

        await self._ensure_row_initialized(thread, context)

        context.state = "awaiting_clan"
        content = f"Which clan tag applies to {context.username} (ticket {context.ticket_number})?\n{CLAN_TAG_PROMPT_HELPER}"
        view = PromoClanSelectView(self, context, tags)
        try:
            message = await thread.send(content, view=view)
        except Exception:
            context.state = "open"
            log.exception(
                "failed to post promo clan selection prompt",
                extra={"thread_id": getattr(thread, "id", None), "ticket": context.ticket_number},
            )
            return
        view.message = message
        context.prompt_message_id = message.id

        if context.user_id is None:
            log.warning(
                "promo watcher: skipping session persist; no resolved user id",
                extra={"thread_id": getattr(thread, "id", None), "ticket": context.ticket_number},
            )
            return

        ticket_username = None
        try:
            _, ticket_username = (getattr(thread, "name", "") or "").split("-", 1)
            ticket_username = ticket_username.strip()
        except ValueError:
            ticket_username = None

        created_at = getattr(message, "created_at", None) or dt.datetime.now(UTC)
        try:
            await persist_session_for_thread(
                flow="promo",
                ticket_number=context.ticket_number,
                thread=thread,
                user_id=context.user_id,
                username=context.username,
                created_at=created_at,
                panel_message_id=message.id,
            )
        except Exception:
            log.exception(
                "promo watcher: failed to persist onboarding session at panel creation",
                extra={"thread_id": getattr(thread, "id", None), "ticket": context.ticket_number},
            )
        else:
            if context.ticket_number and ticket_username:
                try:
                    await promo_tickets.save(context.ticket_number, ticket_username)
                except Exception:
                    log.exception(
                        "failed to persist promo ticket log",
                        extra={"thread_id": getattr(thread, "id", None), "ticket": context.ticket_number},
                    )

    async def finalize_from_interaction(
        self,
        context: PromoTicketContext,
        tag: str,
        interaction: discord.Interaction,
        view: PromoClanSelectView,
    ) -> None:
        thread = interaction.channel if isinstance(interaction.channel, discord.Thread) else None
        if thread is None:
            await interaction.followup.send(
                "⚠️ I lost track of the ticket thread. Please try again.", ephemeral=True
            )
            return
        await self._finalize_clan_tag(
            thread,
            context,
            tag,
            actor=getattr(interaction, "user", None),
            prompt_message=interaction.message,
            view=view,
        )

    async def _finalize_clan_tag(
        self,
        thread: discord.Thread,
        context: PromoTicketContext,
        final_tag: str,
        *,
        actor: discord.abc.User | None,
        prompt_message: Optional[discord.Message],
        view: Optional[PromoClanSelectView],
    ) -> None:
        if context.state != "awaiting_clan":
            return
        final_tag = (final_tag or "").strip().upper()
        if not final_tag:
            return

        tags = await self._load_clan_tags()
        if final_tag not in tags:
            await self._send_invalid_tag_notice(thread, actor, final_tag)
            return

        context.clan_tag = final_tag

        if view is not None:
            view.stop()
        if prompt_message is None and context.prompt_message_id:
            try:
                prompt_message = await thread.fetch_message(context.prompt_message_id)
            except Exception:
                prompt_message = None

        previous_final: str | None = ""
        try:
            existing_row = await asyncio.to_thread(
                onboarding_sheets.find_promo_row, context.ticket_number
            )
            if existing_row:
                row_values = existing_row[1]
                previous_final = (row_values.get("clantag", "") or "").strip()
        except Exception:
            previous_final = None
            log.exception(
                "promo reconcile: failed to fetch promo row before close write",
                extra={"ticket": context.ticket_number},
            )

        await self._complete_close(
            thread,
            context,
            progression="",
            clan_name="",
            previous_final=previous_final,
        )
        if context.state != "closed":
            return

        updated_name = getattr(thread, "name", "") or ""
        if updated_name and not updated_name.upper().endswith(f"-{final_tag}"):
            renamed = f"{updated_name}-{final_tag}"
            if len(renamed) > 100:
                renamed = renamed[:100]
            try:
                await thread.edit(name=renamed)
            except Exception:
                log.debug(
                    "promo watcher: failed to rename thread after clan finalization",
                    exc_info=True,
                    extra={"thread_id": getattr(thread, "id", None), "ticket": context.ticket_number},
                )

        followup = f"✅ Logged clan tag **{final_tag}** to Promo and closed this promo workflow."
        if prompt_message is not None:
            try:
                await prompt_message.edit(content=followup, view=None)
            except Exception:
                await thread.send(followup)
        else:
            await thread.send(followup)

    async def _complete_close(
        self,
        thread: discord.Thread,
        context: PromoTicketContext,
        progression: str,
        clan_name: str,
        *,
        phase: str | None = None,
        previous_final: str | None = "",
    ) -> None:
        timestamp = _format_date(dt.datetime.now(UTC))
        row = [
            context.ticket_number,
            context.username,
            context.clan_tag,
            timestamp,
            context.promo_type,
            context.thread_created,
            context.year,
            context.month,
            context.join_month,
            clan_name,
            progression,
        ]
        try:
            result = await log_sheet_write(
                flow="promo",
                phase=phase or "close",
                tab="Promo",
                logger=log,
                thread=thread,
                user=context.username,
                write_coro=lambda: asyncio.to_thread(
                    onboarding_sheets.upsert_promo, row, PROMO_HEADERS
                ),
            )
        except Exception:
            log.exception(
                "failed to finalize promo closure",
                extra={"thread_id": getattr(thread, "id", None), "ticket": context.ticket_number},
            )
            await thread.send("⚠️ I couldn't update the promo log. Please try again later.")
            return

        context.clan_name = clan_name
        context.progression = progression
        context.state = "closed"
        log.info(
            "promo_ticket_close — ticket=%s • user=%s • result=row_%s",
            context.ticket_number,
            context.username,
            result,
        )
        source = "promo"
        final_is_real = False
        open_deltas: Dict[str, int] = {}
        recompute_tags: List[str] = []
        if previous_final is None:
            decision_line = (
                "decision: "
                f"final_tag={(context.clan_tag or '').strip().upper() or '-'} • previous_final=- • "
                "reservation=not_applicable • consume_open_spot=False • final_is_real=False • "
                "decision_result=skipped_open_delta • skip_reason=previous_final_unavailable"
            )
            log.warning(
                "promo_open_spots_reconcile — ticket=%s • user=%s • final_tag=%s • source=%s • reason=previous_final_unavailable • action_required=manual_open_spots_review • %s",
                context.ticket_number,
                context.username,
                context.clan_tag or "-",
                source,
                decision_line,
            )
            try:
                onboarding_sessions.mark_completed(getattr(thread, "id", 0))
            except Exception:
                log.exception(
                    "promo watcher: failed to mark onboarding session complete",
                    extra={"thread_id": getattr(thread, "id", None)},
                )
            return

        try:
            final_entry = await asyncio.to_thread(recruitment_sheets.find_clan_row, context.clan_tag)
            final_is_real = final_entry is not None
        except Exception:
            log.exception(
                "promo reconcile: failed to check final clan tag",
                extra={"ticket": context.ticket_number, "clan_tag": context.clan_tag},
            )
            final_is_real = False

        normalized_previous = (previous_final or "").strip().upper()
        normalized_final = (context.clan_tag or "").strip().upper()
        consume_open_spot = bool(
            normalized_final
            and normalized_final != _NO_PLACEMENT_TAG
            and (
                not normalized_previous
                or normalized_previous == _NO_PLACEMENT_TAG
                or normalized_previous != normalized_final
            )
        )
        decision = _determine_reservation_decision(
            normalized_final,
            None,
            no_placement_tag=_NO_PLACEMENT_TAG,
            final_is_real=final_is_real,
            consume_open_spot=consume_open_spot,
            previous_final=previous_final or "",
        )
        open_deltas = dict(decision.open_deltas)
        recompute_tags = list(decision.recompute_tags)

        for tag, delta in open_deltas.items():
            try:
                await availability.adjust_manual_open_spots(tag, delta)
            except Exception:
                log.exception(
                    "promo reconcile: failed to adjust manual open spots",
                    extra={"ticket": context.ticket_number, "clan_tag": tag, "delta": delta},
                )
        for tag in recompute_tags:
            try:
                await availability.recompute_clan_availability(tag, guild=thread.guild)
            except Exception:
                log.exception(
                    "promo reconcile: failed to recompute clan availability",
                    extra={"ticket": context.ticket_number, "clan_tag": tag},
                )

        decision_line = _decision_visibility_line(
            final_tag=normalized_final,
            previous_final=previous_final,
            reservation_row=None,
            reservation_label="not_applicable",
            consume_open_spot=consume_open_spot,
            final_is_real=final_is_real,
            open_deltas=open_deltas,
            reservation_state_override="not_applicable",
        )
        log.info(
            "promo_open_spots_reconcile — ticket=%s • user=%s • clan=%s • source=%s • %s",
            context.ticket_number,
            context.username,
            context.clan_tag or "-",
            source,
            decision_line,
        )
        try:
            onboarding_sessions.mark_completed(getattr(thread, "id", 0))
        except Exception:
            log.exception(
                "promo watcher: failed to mark onboarding session complete",
                extra={"thread_id": getattr(thread, "id", None)},
            )

    async def _touch_promo_sheet_for_reminder(
        self,
        *,
        phase: str,
        thread: discord.Thread,
        context: PromoTicketContext,
        created_at: dt.datetime,
        user_ref: str,
    ) -> None:
        created_value = created_at.isoformat()
        updated_value = dt.datetime.now(UTC).isoformat()

        try:
            existing = await asyncio.to_thread(
                onboarding_sheets.find_promo_row, context.ticket_number
            )
        except Exception:
            log.debug(
                "promo reminder: failed to load promo row for sheet logging",
                exc_info=True,
                extra={"ticket": context.ticket_number, "thread_id": getattr(thread, "id", None)},
            )
            existing = None

        if existing:
            row_values = list(existing[1].values()) if isinstance(existing[1], dict) else list(existing[1])
        else:
            row_values = [
                context.ticket_number,
                context.username,
                context.clan_tag,
                "",
                context.promo_type,
                context.thread_created,
                context.year,
                context.month,
                context.join_month,
                context.clan_name,
                context.progression,
                getattr(thread, "name", ""),
                str(context.user_id or ""),
                str(getattr(thread, "id", 0)),
                "",
                context.state,
                created_value,
                "",
            ]

        if len(row_values) < len(PROMO_HEADERS):
            row_values.extend(["" for _ in range(len(PROMO_HEADERS) - len(row_values))])

        try:
            created_idx = PROMO_HEADERS.index("created_at")
            updated_idx = PROMO_HEADERS.index("updated_at")
        except ValueError:
            return

        if not row_values[created_idx]:
            row_values[created_idx] = created_value
        row_values[updated_idx] = updated_value

        try:
            await log_sheet_write(
                flow="promo",
                phase=phase,
                tab="Promo",
                logger=log,
                thread=thread,
                user=user_ref,
                write_coro=lambda: asyncio.to_thread(
                    onboarding_sheets.upsert_promo, row_values, PROMO_HEADERS
                ),
            )
        except Exception:
            log.debug(
                "promo reminder: sheet touch failed",
                exc_info=True,
                extra={"ticket": context.ticket_number, "thread_id": getattr(thread, "id", None)},
            )

    async def auto_close_ticket(
        self, thread: discord.Thread, context: PromoTicketContext
    ) -> None:
        thread_id = getattr(thread, "id", 0)
        if thread_id:
            self._auto_closed_threads.add(int(thread_id))
        context.state = "closed"
        await self._complete_close(
            thread,
            context,
            progression=context.progression,
            clan_name=context.clan_name,
            phase="auto_close",
        )

    async def auto_close_for_inactivity(
        self,
        thread: discord.Thread,
        context: PromoTicketContext,
        *,
        notice: str,
        closed_name: str | None,
    ) -> None:
        thread_id = getattr(thread, "id", 0)
        if thread_id:
            self._auto_closed_threads.add(int(thread_id))

        if closed_name:
            try:
                await thread.edit(name=closed_name)
            except Exception:
                log.warning(
                    "promo auto-close rename failed",
                    exc_info=True,
                    extra={"thread_id": getattr(thread, "id", None), "ticket": context.ticket_number},
                )

        try:
            await thread.send(notice)
        except Exception:
            log.warning(
                "promo auto-close notice failed",
                exc_info=True,
                extra={"thread_id": getattr(thread, "id", None)},
            )

        try:
            await thread.edit(archived=True, locked=True)
        except Exception:
            log.warning(
                "promo auto-close archive failed",
                exc_info=True,
                extra={"thread_id": getattr(thread, "id", None)},
            )

        await self.auto_close_ticket(thread, context)

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        if not self._features_enabled():
            return
        if not self._is_ticket_thread(thread):
            return
        context = await self._ensure_context(thread)
        if context is None:
            return

        created_at = getattr(thread, "created_at", None) or dt.datetime.now(UTC)

        starter: discord.Message | None = None
        try:
            starter = await locate_welcome_message(thread)
            applicant_id, _ = extract_target_from_message(starter)
        except Exception:
            applicant_id = None
            log.debug(
                "promo watcher: failed to resolve applicant on ticket open", exc_info=True, extra={"thread_id": getattr(thread, "id", None)}
            )

        bot_user_id = getattr(getattr(self.bot, "user", None), "id", None)
        subject_resolved = await resolve_subject_user_id(thread, bot_user_id=bot_user_id)
        if subject_resolved is None and applicant_id is not None:
            subject_resolved = applicant_id

        ticket_user = applicant_id if applicant_id is not None else subject_resolved

        # Persist the resolved subject so panel creation can write the onboarding session row later.
        context.user_id = ticket_user

        await self._log_ticket_open(
            thread,
            context,
            user_id=ticket_user,
            created_at=created_at,
        )

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread) -> None:
        if not self._features_enabled():
            return
        if not self._is_ticket_thread(after):
            return
        context = await self._ensure_context(after)
        if context is None:
            return
        if context.state in {"awaiting_clan", "closed"}:
            return
        if getattr(after, "id", None) in self._auto_closed_threads:
            context.state = "closed"
            return
        session_row = onboarding_sessions.get_by_thread_id(getattr(after, "id", None))
        if session_row and session_row.get("auto_closed_at"):
            return
        if not _transitioned_to_closed(before, after):
            return
        await self._begin_clan_prompt(after, context)

    def _parse_progression_payload(self, payload: str) -> tuple[str, str]:
        text = (payload or "").strip()
        if not text or text.lower() == "skip":
            return "", ""
        if "|" in text:
            parts = text.split("|", 1)
            return parts[0].strip(), parts[1].strip()
        return text, ""

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        channel_ref = (
            getattr(message.channel, "parent_id", None)
            if isinstance(message.channel, discord.Thread)
            else getattr(message.channel, "id", None)
        )
        if self.channel_id is None or channel_ref != self.channel_id:
            return
        if not self._features_enabled():
            return
        thread = message.channel if isinstance(message.channel, discord.Thread) else None
        if thread is None or not self._is_ticket_thread(thread):
            return

        start = monotonic()
        context = await self._ensure_context(thread)
        if context is None:
            return

        try:
            thread_id_int = int(thread.id)
        except (TypeError, ValueError):
            thread_id_int = None
        controller = panels.get_controller(thread_id_int) if thread_id_int is not None else None
        handler = getattr(controller, "handle_rolling_message", None) if controller else None
        if callable(handler):
            try:
                handled = await handler(message)
            except Exception:
                log.warning("promo rolling card handler raised", exc_info=True)
            else:
                if handled:
                    return

        if self._is_ticket_tool(message.author):
            content = (message.content or "").lower()
            if _CLOSED_MESSAGE_TOKEN in content:
                context.close_detected = True
                await self._begin_clan_prompt(thread, context)
                return

            trigger_key, flow_key = _promo_trigger_from_content(message.content)
            has_trigger_marker = "<!-- trigger:" in (message.content or "")
            if has_trigger_marker and not trigger_key:
                self._log_missing_trigger(
                    message.author,
                    thread,
                    reason="missing_trigger",
                    trigger=None,
                    start=start,
                )
                return
            if trigger_key and not flow_key:
                self._log_missing_trigger(
                    message.author,
                    thread,
                    reason="unknown_flow",
                    trigger=trigger_key,
                    start=start,
                )
                return
            if not trigger_key or not flow_key:
                return

            outcome = await post_open_questions_panel(
                self.bot,
                thread,
                actor=message.author,
                flow=flow_key,
                trigger_message=message,
            )
            self._log_panel_outcome(
                message.author,
                thread,
                outcome=outcome,
                trigger=flow_key,
                flow=flow_key,
            )
            return

        if getattr(message.author, "bot", False):
            return

        if context.state == "awaiting_clan":
            candidate = (message.content or "").strip().upper()
            if not candidate:
                return
            tags = await self._load_clan_tags()
            if candidate not in tags:
                await self._send_invalid_tag_notice(thread, message.author, candidate)
                return
            await self._finalize_clan_tag(
                thread,
                context,
                candidate,
                actor=message.author,
                prompt_message=None,
                view=None,
            )
            return

        trigger_key, flow_key = _promo_trigger_from_content(message.content)
        has_trigger_marker = "<!-- trigger:" in (message.content or "")
        if has_trigger_marker and not trigger_key:
            self._log_missing_trigger(
                message.author,
                thread,
                reason="missing_trigger",
                trigger=None,
                start=start,
            )
            return
        if trigger_key and not flow_key:
            self._log_missing_trigger(
                message.author,
                thread,
                reason="unknown_flow",
                trigger=trigger_key,
                start=start,
            )
            return
        if not trigger_key or not flow_key:
            return

        outcome = await post_open_questions_panel(
            self.bot,
            thread,
            actor=message.author,
            flow=flow_key,
            trigger_message=message,
        )
        self._log_panel_outcome(
            message.author,
            thread,
            outcome=outcome,
            trigger=flow_key,
            flow=flow_key,
        )


    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Guard against firing multiple times on reconnects
        if self._announced:
            return
        self._announced = True

        channel_id = get_promo_channel_id()
        if not channel_id:
            line = log_lifecycle(
                log,
                "promo",
                "enabled",
                scope_label="Promo watcher",
                emoji="📴",
                result="disabled",
                reason="missing_promo_channel",
                channel=None,
                channel_id=None,
            )
            return

        try:
            channel_id_int = int(channel_id)
        except (TypeError, ValueError):
            self.channel_id = None
            line = log_lifecycle(
                log,
                "promo",
                "enabled",
                scope_label="Promo watcher",
                emoji="⚠️",
                result="error",
                reason="invalid_promo_channel",
                channel_id=channel_id,
            )
            return

        self.channel_id = channel_id_int

        if not feature_flags.is_enabled("promo_enabled"):
            line = log_lifecycle(
                log,
                "promo",
                "enabled",
                scope_label="Promo watcher",
                emoji="📴",
                result="disabled",
                reason="feature_promo_enabled_off",
            )
            return

        if not feature_flags.is_enabled("enable_promo_hook"):
            line = log_lifecycle(
                log,
                "promo",
                "enabled",
                scope_label="Promo watcher",
                emoji="📴",
                result="disabled",
                reason="feature_enable_promo_hook_off",
            )
            return

        label = _channel_readable_label(self.bot, channel_id_int)
        log_lifecycle(
            log,
            "promo",
            "enabled",
            scope_label="Promo watcher",
            emoji="✅",
            channel=label,
            channel_id=channel_id_int,
            triggers=len(_PROMO_TRIGGER_MAP),
        )
        # Startup watcher status is included in the global startup summary.


async def setup(bot: commands.Bot) -> None:
    existing = bot.get_cog("PromoTicketWatcher")
    if existing is None:
        await bot.add_cog(PromoTicketWatcher(bot))
    elif not isinstance(existing, PromoTicketWatcher):
        raise RuntimeError("cog name collision for PromoTicketWatcher")
