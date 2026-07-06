"""Promo ticket watcher that logs lifecycle events to Sheets."""
from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from types import SimpleNamespace
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
from modules.onboarding.watcher_welcome import (
    _build_clan_math_row_lines,
    _capture_clan_snapshots,
    _channel_readable_label,
    _clan_math_column_indices,
    _NO_PLACEMENT_TAG,
    _log_clan_math_event,
    _normalize_clan_math_targets,
    _send_placement_log_line,
    build_closed_thread_name,
    cleanup_reservation_for_ticket_close,
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
from shared.cache import telemetry as cache_telemetry
from shared.sheets import onboarding as onboarding_sheets
from shared.sheets import onboarding_sessions
from shared.sheets import promo_tickets
from shared.sheets import recruitment as recruitment_sheets
from shared.sheets import reservations as reservations_sheets

UTC = dt.timezone.utc
log = logging.getLogger("c1c.onboarding.promo_watcher")


async def _log_promo_failure_row(
    *,
    reason: str,
    source: str,
    thread: object | None = None,
    actor: object | None = None,
    target_user_id: int | str | None = None,
    target_message_id: int | str | None = None,
    message_id: int | str | None = None,
    promo_flow: str | None = None,
    ticket_code: str | None = None,
) -> None:
    guild = getattr(thread, "guild", None)
    parent = getattr(thread, "parent", None)
    thread_id = getattr(thread, "id", None)
    note = {
        "promo_ticket_parse_failed": "promo_ticket_parse_failed before dialog start",
        "close_context_unresolved": "close_context_unresolved before clan prompt",
        "context_not_found": "context_not_found during close/startup backfill",
    }.get(reason, str(reason or "promo failure"))
    payload = {
        "flow": "promo",
        "source": source,
        "result": "failure",
        "reason": reason,
        "guild_id": getattr(guild, "id", None),
        "parent_channel_id": getattr(parent, "id", None),
        "thread_id": thread_id,
        "thread_name": getattr(thread, "name", None),
        "actor_id": getattr(actor, "id", None),
        "actor_name": onboarding_logs.format_actor_handle(actor) if actor is not None else None,
        "target_user_id": target_user_id,
        "target_message_id": target_message_id,
        "message_id": message_id,
        "promo_flow": promo_flow,
        "ticket_code": ticket_code,
    }
    try:
        result = await asyncio.to_thread(
            onboarding_sheets.update_ticket_finalization_state,
            "promo",
            ticket=ticket_code,
            thread_id=thread_id,
            finalization_status="skipped_unresolved",
            finalization_note=note,
        )
    except Exception:
        log.exception("promo failure promo-row update failed", extra=payload)
    else:
        log.warning("promo failure promo-row updated", extra={**payload, "sheet_result": result})


def _promo_headers_for_write(*, ticket: str | None = None) -> list[str]:
    try:
        return onboarding_sheets.get_live_promo_headers()
    except Exception:
        log.exception(
            "promo source clan header mapping unavailable; cannot write Promo row",
            extra={"ticket": ticket or "-"},
        )
        raise


def _source_clan_from_promo_values(values: dict[str, str], *, ticket: str | None = None) -> str:
    try:
        source_header = onboarding_sheets.get_promo_source_clan_tag_header()
    except Exception:
        log.exception(
            "promo source clan header mapping unavailable; cannot read source clan",
            extra={"ticket": ticket or "-"},
        )
        return ""
    if source_header not in values:
        log.warning(
            "promo row missing configured source clan header",
            extra={"ticket": ticket or "-", "source_header": source_header},
        )
        return ""
    return (values.get(source_header, "") or "").strip()



def _promo_finalization_state(row_values: dict[str, str] | list[str] | None) -> dict[str, str]:
    if not row_values:
        return {}
    try:
        return onboarding_sheets.get_ticket_finalization_state("promo", row_values)
    except Exception:
        log.exception("promo finalization state unavailable")
        if isinstance(row_values, dict):
            return {
                "finalization_status": str(row_values.get("finalization_status", "") or "").strip(),
                "reservation_status": str(row_values.get("reservation_status", "") or "").strip(),
                "clan_update_status": str(row_values.get("clan_update_status", "") or "").strip(),
                "finalization_note": str(row_values.get("finalization_note", "") or "").strip(),
            }
        return {}


def _is_closed_thread(thread: discord.Thread) -> bool:
    name = (getattr(thread, "name", "") or "").lower()
    return bool(getattr(thread, "archived", False)) or bool(getattr(thread, "locked", False)) or name.startswith("closed-")


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
PROMO_CLOSE_BACKFILL_LOOKBACK_HOURS = 48
_PROMO_BACKFILL_TIMESTAMP_HEADERS = (
    "date closed",
    "updated_at",
    "created_at",
    "thread created",
)


async def _ensure_fresh_clans_for_placement(*, actor: str, ticket: str, user: str) -> bool:
    try:
        await cache_telemetry.refresh_now("clans", actor=actor)
    except Exception:
        log.exception("promo reconcile: failed to refresh clans cache", extra={"ticket": ticket, "user": user})
        return False
    snapshot = cache_telemetry.get_snapshot("clans")
    if (not snapshot.available) or snapshot.last_result not in {"ok", "retry_ok"}:
        log.warning(
            "promo reconcile: clans data not fresh; skipping seat math",
            extra={"ticket": ticket, "user": user, "last_result": snapshot.last_result, "last_error": snapshot.last_error},
        )
        return False
    return True


def _find_promo_clan_row(clan_tag: str, *, force: bool = False) -> tuple[int, List[str]] | None:
    """Resolve promo clans with the same configured bot_info tag column used by availability writes."""

    try:
        headers = availability._resolve_availability_headers()  # type: ignore[attr-defined]
        return availability._find_availability_clan_row(clan_tag, headers)  # type: ignore[attr-defined]
    except Exception:
        log.debug(
            "promo clan lookup falling back to recruitment.find_clan_row",
            exc_info=True,
            extra={"clan_tag": clan_tag},
        )
    try:
        return recruitment_sheets.find_clan_row(clan_tag, force=force)
    except TypeError:
        return recruitment_sheets.find_clan_row(clan_tag)


_find_promo_clan_row.lookup_mode = "availability_configured_clan_tag_header_with_recruitment_fallback"  # type: ignore[attr-defined]


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
    source_clan_tag: str = ""
    clan_name: str = ""
    progression: str = ""
    state: str = "open"
    prompt_message_id: Optional[int] = None
    close_detected: bool = False
    user_id: Optional[int] = None
    close_trigger: str = "ticket_tool"


class PromoClanSelect(discord.ui.Select):
    def __init__(self, parent_view: "PromoClanSelectView", tags: List[str], *, role: str) -> None:
        options = [discord.SelectOption(label=tag, value=tag) for tag in tags[:25]]
        placeholder = "Where did they come from?" if role == "source" else "Where are they going?"
        super().__init__(placeholder=placeholder, min_values=1, max_values=1, options=options)
        self._parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:  # pragma: no cover - UI callback
        if not self.values:
            await interaction.response.defer()
            return
        await self._parent_view.handle_selection(interaction, self.values[0])


class PromoClanSelectView(discord.ui.View):
    def __init__(self, watcher: "PromoTicketWatcher", context: PromoTicketContext, tags: List[str], *, role: str):
        super().__init__(timeout=300)
        self.watcher = watcher
        self.context = context
        self.role = role
        self.message: Optional[discord.Message] = None
        self.select = PromoClanSelect(self, tags, role=role)
        self.add_item(self.select)

    async def handle_selection(self, interaction: discord.Interaction, tag: str) -> None:
        await interaction.response.defer()
        if self.role == "source":
            await self.watcher.source_from_interaction(self.context, tag, interaction, self)
            return
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


def _parse_backfill_timestamp(value: object) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    candidates = [text]
    if " " in text and "T" not in text:
        candidates.append(text.replace(" ", "T", 1))
    if len(text) >= 10:
        candidates.append(text[:10])
    for candidate in candidates:
        try:
            if len(candidate) == 10 and candidate[4] == "-" and candidate[7] == "-":
                parsed = dt.datetime.combine(dt.date.fromisoformat(candidate), dt.time.min)
            else:
                parsed = dt.datetime.fromisoformat(candidate.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    return None


def _backfill_row_timestamp(values: dict[str, str]) -> tuple[dt.datetime | None, str | None]:
    for header in _PROMO_BACKFILL_TIMESTAMP_HEADERS:
        parsed = _parse_backfill_timestamp(values.get(header))
        if parsed is not None:
            return parsed, header
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

        now = getattr(thread, "created_at", None) or dt.datetime.now(UTC)
        created_str = _format_timestamp(now)
        found = None
        found_source = None
        parts = None
        session_row = None
        try:
            session_row = onboarding_sessions.get_by_thread_id(getattr(thread, "id", None))
        except Exception:
            session_row = None
        if session_row:
            parts = parse_promo_thread_name(session_row.get("thread_name") or getattr(thread, "name", ""))
        if parts is None:
            parts = parse_promo_thread_name(thread.name)

        if parts is not None:
            try:
                found = await asyncio.to_thread(onboarding_sheets.find_promo_row, parts.ticket_code)
                if found is not None:
                    found_source = "sheet_ticket"
            except Exception:
                log.exception("failed to read promo row during context ensure", extra={"ticket": parts.ticket_code})
                found = None
        try:
            if found is None:
                found = await asyncio.to_thread(onboarding_sheets.find_promo_row_by_thread_id, getattr(thread, "id", None))
                if found is not None:
                    found_source = "sheet_thread_id"
        except Exception:
            log.exception("close_context_resolved failed reading promo row by thread_id", extra={"thread_id": getattr(thread, "id", None)})
        if found and found_source == "sheet_thread_id":
            _, values = found
            ticket = (values.get("ticket number") or values.get("ticket_number") or values.get("ticket") or "").strip()
            username = (values.get("username") or values.get("thread_name") or getattr(thread, "name", "") or "unknown").strip(" -_")
            ptype = (values.get("type") or "move").strip()
            context = PromoTicketContext(
                thread_id=thread.id,
                ticket_number=ticket,
                username=username or "unknown",
                promo_type=ptype,
                thread_created=values.get("thread created", "") or created_str,
                year=values.get("year", "") or str(now.year),
                month=values.get("month", "") or now.strftime("%B"),
            )
            context.clan_tag = values.get("clantag", "") or context.clan_tag
            context.source_clan_tag = _source_clan_from_promo_values(values, ticket=ticket) or context.source_clan_tag
            context.clan_name = values.get("clan name", "") or context.clan_name
            context.progression = values.get("progression", "") or context.progression
            context.join_month = values.get("join_month", "") or context.join_month
            try:
                uid = str(values.get("user_id") or "").strip()
                context.user_id = int(uid) if uid else None
            except (TypeError, ValueError):
                context.user_id = None
            self._tickets[thread.id] = context
            log.info("close_context_resolved", extra={"flow": "promo", "thread_id": thread.id, "ticket": context.ticket_number, "source": found_source})
            return context
        if parts is None:
            target_user_id = None
            target_message_id = None
            try:
                welcome_message = await locate_welcome_message(thread)
                target_user_id, target_message_id = extract_target_from_message(welcome_message)
            except Exception:
                pass
            log.warning(
                "close_context_unresolved",
                extra={
                    "flow": "promo",
                    "thread_id": getattr(thread, "id", None),
                    "thread_name": getattr(thread, "name", None),
                    "target_user_id": target_user_id,
                },
            )
            await _log_promo_failure_row(
                reason="close_context_unresolved",
                source="message",
                thread=thread,
                target_user_id=target_user_id,
                target_message_id=target_message_id,
            )
            return None

        context = PromoTicketContext(
            thread_id=thread.id,
            ticket_number=parts.ticket_code,
            username=parts.username,
            promo_type=parts.promo_type,
            thread_created=created_str,
            year=str(now.year),
            month=now.strftime("%B"),
        )

        if found:
            _, values = found
            context.clan_tag = values.get("clantag", "") or context.clan_tag
            context.source_clan_tag = _source_clan_from_promo_values(values, ticket=parts.ticket_code) or context.source_clan_tag
            context.clan_name = values.get("clan name", "") or context.clan_name
            context.progression = values.get("progression", "") or context.progression
            context.thread_created = values.get("thread created", "") or context.thread_created
            context.year = values.get("year", "") or context.year
            context.month = values.get("month", "") or context.month
            context.join_month = values.get("join_month", "") or context.join_month

        if context.user_id is None and session_row:
            try:
                uid = str(session_row.get("user_id") or "").strip()
                context.user_id = int(uid) if uid else None
            except (TypeError, ValueError):
                context.user_id = None
        if context.user_id is None and found:
            try:
                uid = str(found[1].get("user_id") or "").strip()
                context.user_id = int(uid) if uid else None
            except (TypeError, ValueError):
                context.user_id = None
        if context.user_id is None:
            try:
                welcome_message = await locate_welcome_message(thread)
                context.user_id, _ = extract_target_from_message(welcome_message)
            except Exception:
                context.user_id = None

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
                    context.source_clan_tag,
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
        await self._patch_ticket_metadata(
            phase="created_metadata",
            thread=thread,
            context=context,
            user_id=user_id,
            status="open",
            created_at=created,
        )

    async def _patch_ticket_metadata(
        self,
        *,
        phase: str,
        thread: discord.Thread,
        context: PromoTicketContext,
        user_id: int | str | None = None,
        panel_message_id: int | str | None = None,
        status: str | None = None,
        review_reason: str | None = None,
        created_at: dt.datetime | None = None,
        updated_at: dt.datetime | None = None,
    ) -> str | None:
        resolved_user_id = user_id if user_id is not None else context.user_id
        reason = review_reason
        if resolved_user_id is None and not reason:
            reason = "missing_user_id"
            log.warning(
                "promo metadata user_id missing",
                extra={"ticket": context.ticket_number, "thread_id": getattr(thread, "id", None)},
            )
        try:
            result = await asyncio.to_thread(
                onboarding_sheets.patch_promo_ticket_metadata,
                ticket=context.ticket_number,
                thread_id=getattr(thread, "id", None),
                thread_name=getattr(thread, "name", ""),
                username=context.username,
                user_id=resolved_user_id,
                panel_message_id=panel_message_id,
                status=status or context.state or "open",
                review_reason=reason,
                created_at=created_at or getattr(thread, "created_at", None),
                updated_at=updated_at,
            )
        except Exception:
            log.exception(
                "promo metadata write failed",
                extra={"phase": phase, "ticket": context.ticket_number, "thread_id": getattr(thread, "id", None)},
            )
            return None
        log.info(
            "promo metadata write %s",
            result,
            extra={"phase": phase, "ticket": context.ticket_number, "thread_id": getattr(thread, "id", None)},
        )
        return result

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
            context.source_clan_tag = _source_clan_from_promo_values(values, ticket=context.ticket_number) or context.source_clan_tag
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

    async def _begin_clan_prompt(self, thread: discord.Thread, context: PromoTicketContext, *, trigger: str | None = None) -> None:
        trigger = trigger or getattr(context, "close_trigger", "ticket_tool")
        try:
            found = await asyncio.to_thread(onboarding_sheets.find_promo_row, context.ticket_number)
            if found and (_promo_finalization_state(found[1]).get("finalization_status") or "").lower() == "done":
                log.info("close_already_finalized", extra={"flow": "promo", "trigger": trigger, "thread_id": getattr(thread, "id", None), "ticket": context.ticket_number})
                await _send_placement_log_line(flow="promo", outcome="already_done", ticket=context.ticket_number, player=context.username, trigger=trigger, action="skipped")
                return
        except Exception:
            log.exception("promo finalization prompt preflight failed", extra={"ticket": context.ticket_number, "thread_id": getattr(thread, "id", None)})
        tags = await self._load_clan_tags()
        if not tags:
            log.warning("promo watcher unable to load clan tags for close prompt", extra={"ticket": context.ticket_number})
            await _send_placement_log_line(flow="promo", outcome="failed", ticket=context.ticket_number, player=context.username, trigger=trigger, reason="clan_tags_unavailable", action="manual_check")
            return

        await self._ensure_row_initialized(thread, context)
        if context.source_clan_tag and context.clan_tag:
            context.state = "awaiting_destination_clan"
            await self._complete_close(thread, context, progression=context.progression, clan_name=context.clan_name, phase="close", previous_final=context.source_clan_tag, trigger=trigger)
            return
        try:
            await asyncio.to_thread(onboarding_sheets.update_ticket_finalization_state, "promo", ticket=context.ticket_number, thread_id=getattr(thread, "id", None), finalization_status="prompt_required", finalization_note="missing source/destination, prompted staff")
        except Exception:
            log.exception("promo prompt finalization state update failed", extra={"ticket": context.ticket_number, "thread_id": getattr(thread, "id", None)})

        context.state = "awaiting_source_clan"
        content = (
            f"Where did the member come from?\n"
            f"Where is the member going?\n"
            f"First select the source clan for {context.username} (ticket {context.ticket_number}).\n"
            f"Use **{_NO_PLACEMENT_TAG}** only when there was no source placement.\n{CLAN_TAG_PROMPT_HELPER}"
        )
        source_tags = [tag for tag in tags if tag != _NO_PLACEMENT_TAG]
        source_tags.insert(0, _NO_PLACEMENT_TAG)
        view = PromoClanSelectView(self, context, source_tags, role="source")
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
        await self._patch_ticket_metadata(
            phase="close_prompt_metadata",
            thread=thread,
            context=context,
            panel_message_id=message.id,
            status="prompt_required",
            updated_at=getattr(message, "created_at", None),
        )
        log.info("close_prompt_started", extra={"flow": "promo", "trigger": trigger, "thread_id": getattr(thread, "id", None), "ticket": context.ticket_number})
        await _send_placement_log_line(flow="promo", outcome="prompt", ticket=context.ticket_number, player=context.username, trigger=trigger, finalization_status="prompt_required", reason="missing_source_destination", action="prompted_staff")

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


    async def source_from_interaction(
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
        await self._set_source_clan_tag(
            thread,
            context,
            tag,
            actor=getattr(interaction, "user", None),
            prompt_message=interaction.message,
            view=view,
        )

    async def _set_source_clan_tag(
        self,
        thread: discord.Thread,
        context: PromoTicketContext,
        source_tag: str,
        *,
        actor: discord.abc.User | None,
        prompt_message: Optional[discord.Message],
        view: Optional[PromoClanSelectView],
    ) -> None:
        if context.state != "awaiting_source_clan":
            return
        source_tag = (source_tag or "").strip().upper()
        if not source_tag:
            return

        tags = await self._load_clan_tags()
        valid_sources = set(tags) | {_NO_PLACEMENT_TAG}
        if source_tag not in valid_sources:
            await self._send_invalid_tag_notice(thread, actor, source_tag)
            return

        context.source_clan_tag = source_tag
        context.state = "awaiting_destination_clan"
        if view is not None:
            view.stop()

        destination_content = (
            f"Source recorded as **{source_tag}**. Where is the member going?\n"
            f"Select the destination clan for {context.username} (ticket {context.ticket_number}).\n"
            f"{CLAN_TAG_PROMPT_HELPER}"
        )
        destination_view = PromoClanSelectView(self, context, tags, role="destination")
        if prompt_message is not None:
            try:
                await prompt_message.edit(content=destination_content, view=destination_view)
                destination_view.message = prompt_message
                return
            except Exception:
                log.debug("failed to edit promo source prompt into destination prompt", exc_info=True)
        try:
            message = await thread.send(destination_content, view=destination_view)
        except Exception:
            context.state = "awaiting_source_clan"
            log.exception(
                "failed to post promo destination clan selection prompt",
                extra={"thread_id": getattr(thread, "id", None), "ticket": context.ticket_number},
            )
            return
        destination_view.message = message
        context.prompt_message_id = message.id

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
        if context.state not in {"awaiting_clan", "awaiting_destination_clan"}:
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

        previous_final: str | None = (context.source_clan_tag or "").strip().upper()

        await self._complete_close(
            thread,
            context,
            progression="",
            clan_name="",
            previous_final=previous_final,
        )
        if context.state != "closed":
            return

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
        trigger: str = "ticket_tool",
    ) -> None:
        timestamp = _format_date(dt.datetime.now(UTC))
        found_state = None
        try:
            found_state = await asyncio.to_thread(onboarding_sheets.find_promo_row, context.ticket_number)
            if not found_state:
                raise RuntimeError(f"promo finalization row not found for ticket={context.ticket_number}")
            finalization_state = onboarding_sheets.get_ticket_finalization_state("promo", found_state[1])
            if (finalization_state.get("finalization_status") or "").lower() == "done":
                log.info("close_already_finalized", extra={"flow": "promo", "trigger": trigger, "thread_id": getattr(thread, "id", None), "ticket": context.ticket_number})
                await _send_placement_log_line(flow="promo", outcome="already_done", ticket=context.ticket_number, player=context.username, source=context.source_clan_tag, destination=context.clan_tag, trigger=trigger, action="skipped")
                context.state = "closed"
                return
            await asyncio.to_thread(onboarding_sheets.update_ticket_finalization_state, "promo", ticket=context.ticket_number, thread_id=getattr(thread, "id", None), finalization_status="in_progress", finalization_note=f"finalization started by {trigger}")
        except Exception:
            log.exception("promo finalization state preflight failed", extra={"ticket": context.ticket_number, "thread_id": getattr(thread, "id", None)})
            try:
                await asyncio.to_thread(onboarding_sheets.update_ticket_finalization_state, "promo", ticket=context.ticket_number, thread_id=getattr(thread, "id", None), finalization_status="failed", finalization_note="finalization state preflight failed")
            except Exception:
                log.exception("promo finalization state failed marker update failed", extra={"ticket": context.ticket_number, "thread_id": getattr(thread, "id", None)})
            await _send_placement_log_line(flow="promo", outcome="failed", ticket=context.ticket_number, reason="finalization_state_preflight_failed", action="manual_check")
            return
        log.info("close_finalization_started", extra={"flow": "promo", "trigger": trigger, "thread_id": getattr(thread, "id", None), "ticket": context.ticket_number})
        try:
            promo_headers = _promo_headers_for_write(ticket=context.ticket_number)
        except Exception:
            await thread.send("⚠️ Promo source clan header mapping is missing. Please fix Config before closing this promo ticket.")
            return

        row_map = {
            "ticketnumber": context.ticket_number,
            "username": context.username,
            "clantag": context.clan_tag,
            "sourceclantag": context.source_clan_tag,
            "dateclosed": timestamp,
            "type": context.promo_type,
            "threadcreated": context.thread_created,
            "year": context.year,
            "month": context.month,
            "joinmonth": context.join_month,
            "clanname": clan_name,
            "progression": progression,
        }
        row = [
            row_map.get(
                "".join(ch for ch in str(header or "").lower() if ch.isalnum()),
                "",
            )
            for header in promo_headers
        ]
        try:
            await self._patch_ticket_metadata(
                phase="finalization_metadata",
                thread=thread,
                context=context,
                panel_message_id=context.prompt_message_id,
                status="closed",
            )
            result = await log_sheet_write(
                flow="promo",
                phase=phase or "close",
                tab="Promo",
                logger=log,
                thread=thread,
                user=context.username,
                write_coro=lambda: asyncio.to_thread(
                    onboarding_sheets.upsert_promo, row, promo_headers
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
        ticket_id_raw = context.ticket_number
        ticket_id_final = (context.ticket_number or "").strip()
        final_tag = (context.clan_tag or "").strip().upper()
        source_tag = (context.source_clan_tag or "").strip().upper()
        existing_clan = str(found_state[1].get("clantag", "") or "").strip().upper() if found_state else ""
        if not source_tag:
            source_tag = existing_clan or _NO_PLACEMENT_TAG
        if source_tag == _NO_PLACEMENT_TAG and existing_clan:
            source_tag = existing_clan
        channel_name_before = getattr(thread, "name", "") or ""
        log.info(
            "promo_ticket_close — workflow_type=promo/move • ticket_id_raw=%s • ticket_id_final=%s • player_name=%s • source_clan_tag=%s • destination_clan_tag=%s • result=row_%s",
            ticket_id_raw,
            ticket_id_final,
            context.username,
            source_tag or "-",
            final_tag or "-",
            result,
        )

        row_targets = None
        column_map = None
        before_snapshots = {}
        open_spots_before = "-"
        math_tags = {
            tag
            for tag in {final_tag, source_tag}
            if tag and tag != _NO_PLACEMENT_TAG
        }
        if math_tags:
            try:
                row_targets = _normalize_clan_math_targets(math_tags)
                column_map = _clan_math_column_indices()
                before_snapshots = _capture_clan_snapshots(row_targets, column_map, force=True)
                if row_targets:
                    first_key = next(iter(row_targets))
                    snapshot = before_snapshots.get(first_key)
                    if snapshot is not None:
                        open_spots_before = snapshot.values.get("open_spots", "-") or "-"
            except Exception:
                log.exception(
                    "promo_close clan math before-state unavailable; continuing with safe sheet update path",
                    extra={"ticket": context.ticket_number, "clan_tag": final_tag},
                )
                row_targets = None
                column_map = None
                before_snapshots = {}

        cleanup = await cleanup_reservation_for_ticket_close(
            scope="promo",
            ticket=ticket_id_final,
            user=context.username,
            user_id=context.user_id,
            final_tag=final_tag,
            previous_final=source_tag,
            require_source_for_open_spot_math=True,
            guild=getattr(thread, "guild", None),
            require_active_reservation=False,
            logger=log,
            ensure_fresh_fn=_ensure_fresh_clans_for_placement,
            find_active_reservations_fn=reservations_sheets.find_active_reservations_for_recruit,
            find_clan_row_fn=_find_promo_clan_row,
            update_reservation_status_fn=reservations_sheets.update_reservation_status,
            adjust_manual_open_spots_fn=availability.adjust_manual_open_spots,
            recompute_clan_availability_fn=availability.recompute_clan_availability,
        )
        if cleanup.skipped:
            log.info(
                "promo_reservation_cleanup — scope=promo • ticket=%s • user=%s • user_id=%s • clan_tag=%s • reservation=none • result=skip • reason=%s",
                context.ticket_number,
                context.username,
                context.user_id or "-",
                context.clan_tag or "-",
                cleanup.reason or "no_cleanup_needed",
            )
        else:
            log.info(
                "promo_reservation_cleanup — scope=promo • ticket=%s • user=%s • user_id=%s • clan_tag=%s • reservation=%s • old_status=%s • new_status=%s • recalculation=%s • result=%s%s",
                context.ticket_number,
                context.username,
                context.user_id or "-",
                context.clan_tag or "-",
                f"row{cleanup.reservation_row.row_number}" if cleanup.reservation_row is not None else "none",
                cleanup.old_status or "-",
                cleanup.new_status or "-",
                f"recomputed:{','.join(cleanup.recomputed_tags) if cleanup.recomputed_tags else 'none'}",
                "ok" if cleanup.ok else "partial",
                f" • reason={cleanup.reason}" if cleanup.reason else "",
            )
        log.info(
            "promo_open_spots_reconcile — ticket=%s • user=%s • clan=%s • source=promo • %s",
            ticket_id_final,
            context.username,
            final_tag or "-",
            cleanup.decision_line,
        )

        open_spots_after = "-"
        after_snapshots = {}
        row_change_lines = [cleanup.decision_line]
        if row_targets is not None and column_map is not None:
            try:
                after_snapshots = _capture_clan_snapshots(row_targets, column_map, force=True)
                row_change_lines.extend(
                    _build_clan_math_row_lines(row_targets, before_snapshots, after_snapshots)
                )
                if row_targets:
                    first_key = next(iter(row_targets))
                    snapshot = after_snapshots.get(first_key)
                    if snapshot is not None:
                        open_spots_after = snapshot.values.get("open_spots", "-") or "-"
            except Exception:
                log.exception(
                    "promo_close clan math after-state unavailable",
                    extra={"ticket": ticket_id_final, "clan_tag": final_tag},
                )

        logging_channel_result = "skip"
        reservation_status = "released" if cleanup.reservation_row is not None and cleanup.ok else ("failed" if not cleanup.ok else "none")
        clan_update_status = "done" if cleanup.ok else "partial"
        finalization_status = "done" if cleanup.ok else "partial"
        finalization_note = "finalized by close handler" if cleanup.ok else (cleanup.reason or "reservation/clan update partially failed")
        try:
            await asyncio.to_thread(onboarding_sheets.update_ticket_finalization_state, "promo", ticket=ticket_id_final, thread_id=getattr(thread, "id", None), finalization_status=finalization_status, reservation_status=reservation_status, clan_update_status=clan_update_status, finalization_note=finalization_note)
        except Exception:
            finalization_status = "partial"
            logging_channel_result = "state_error"
            log.exception("promo finalization state completion update failed", extra={"ticket": ticket_id_final, "thread_id": getattr(thread, "id", None)})
        outcome = "success" if finalization_status == "done" else "partial"
        await _send_placement_log_line(flow="promo", outcome=outcome, ticket=ticket_id_final, player=context.username, source=source_tag or None, destination=final_tag or _NO_PLACEMENT_TAG, trigger=trigger, reservation=reservation_status, clan_update=clan_update_status, finalization_status=finalization_status, action=(None if outcome == "success" else "manual_check"))
        try:
            await _log_clan_math_event(
                SimpleNamespace(
                    ticket_number=ticket_id_final,
                    username=context.username,
                    close_source=context.close_trigger or trigger or "ticket_tool",
                ),
                final_display=final_tag if final_tag else _NO_PLACEMENT_TAG,
                reservation_label=cleanup.reservation_label or "none",
                reservation_row=cleanup.reservation_row,
                result=("ok" if outcome == "success" else "error"),
                reason=(None if outcome == "success" else cleanup.reason or "promo_finalization_partial"),
                row_change_lines=row_change_lines,
            )
        except Exception:
            log.exception(
                "failed to emit promo clan math log",
                extra={"ticket": ticket_id_final, "result": outcome},
            )
        logging_channel_result = "ok" if logging_channel_result == "skip" else logging_channel_result

        channel_name_after = channel_name_before
        if final_tag:
            new_name = build_closed_thread_name(ticket_id_final, context.username, final_tag)
            try:
                await thread.edit(name=new_name)
                channel_name_after = new_name
            except Exception:
                log.exception(
                    "failed to rename promo thread",
                    extra={"thread_id": getattr(thread, "id", None), "ticket": ticket_id_final},
                )

        release_source_open_spot = cleanup.applied_open_deltas.get(source_tag, 0) > 0
        consume_destination_open_spot = cleanup.applied_open_deltas.get(final_tag, 0) < 0
        log.info(
            "promo_close_debug — workflow_type=promo/move • ticket_id_raw=%s • ticket_id_final=%s • player_name=%s • source_clan_tag=%s • source_clan_lookup_key=%s • source_clan_row_found=%s • source_clan_row_number=%s • previous_is_real=%s • reason_if_not_real=%s • source_clan_lookup_mode=%s • destination_clan_tag=%s • reservation_status=%s • consume_destination_open_spot=%s • release_source_open_spot=%s • open_spots_before=%s • open_spots_after=%s • open_spots_before_by_clan=%s • open_spots_after_by_clan=%s • channel_name_before=%s • channel_name_after=%s • logging_channel_result=%s",
            ticket_id_raw,
            ticket_id_final,
            context.username,
            source_tag or "-",
            cleanup.source_clan_lookup_key or "-",
            cleanup.source_clan_row_found,
            cleanup.source_clan_row_number if cleanup.source_clan_row_number is not None else "-",
            cleanup.previous_is_real,
            cleanup.source_clan_not_real_reason or "-",
            cleanup.source_clan_lookup_mode or "-",
            final_tag or "-",
            cleanup.reservation_label or "none",
            consume_destination_open_spot,
            release_source_open_spot,
            open_spots_before,
            open_spots_after,
            {tag: (before_snapshots.get(key).values.get("open_spots", "-") if before_snapshots.get(key) else "-") for key, tag in (row_targets or {}).items()},
            {tag: (after_snapshots.get(key).values.get("open_spots", "-") if after_snapshots.get(key) else "-") for key, tag in (row_targets or {}).items()} if 'after_snapshots' in locals() else {},
            channel_name_before or "-",
            channel_name_after or "-",
            logging_channel_result,
        )
        log.info("close_finalization_completed", extra={"flow": "promo", "trigger": trigger, "thread_id": getattr(thread, "id", None), "ticket": ticket_id_final, "finalization_status": finalization_status, "reservation_status": reservation_status, "clan_update_status": clan_update_status})
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
        _ = user_ref
        await self._patch_ticket_metadata(
            phase=phase,
            thread=thread,
            context=context,
            panel_message_id=context.prompt_message_id,
            status=context.state or "open",
            created_at=created_at,
            updated_at=dt.datetime.now(UTC),
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
        if context.state in {"awaiting_clan", "awaiting_source_clan", "awaiting_destination_clan", "closed"}:
            return
        if getattr(after, "id", None) in self._auto_closed_threads:
            context.state = "closed"
            return
        session_row = onboarding_sessions.get_by_thread_id(getattr(after, "id", None))
        if session_row and session_row.get("auto_closed_at"):
            return
        trigger = ""
        if bool(getattr(after, "archived", False)) and not bool(getattr(before, "archived", False)):
            trigger = "manual_archive"
        elif bool(getattr(after, "locked", False)) and not bool(getattr(before, "locked", False)):
            trigger = "manual_lock"
        elif (getattr(after, "name", "") or "").lower().startswith("closed-"):
            trigger = "closed_rename"
        elif not _transitioned_to_closed(before, after):
            return
        else:
            trigger = "manual_archive"
        log.info("close_signal_detected", extra={"flow": "promo", "trigger": trigger, "thread_id": getattr(after, "id", None), "ticket": context.ticket_number})
        context.close_trigger = trigger  # type: ignore[attr-defined]
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
                log.info("close_signal_detected", extra={"flow": "promo", "trigger": "ticket_tool", "thread_id": getattr(thread, "id", None), "ticket": context.ticket_number})
                context.close_trigger = "ticket_tool"  # type: ignore[attr-defined]
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
            if getattr(outcome, "panel_message_id", None):
                await self._patch_ticket_metadata(
                    phase="panel_metadata",
                    thread=thread,
                    context=context,
                    panel_message_id=outcome.panel_message_id,
                    status="open",
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

        if context.state in {"awaiting_clan", "awaiting_source_clan", "awaiting_destination_clan"}:
            candidate = (message.content or "").strip().upper()
            if not candidate:
                return
            tags = await self._load_clan_tags()
            valid = set(tags) | ({_NO_PLACEMENT_TAG} if context.state == "awaiting_source_clan" else set())
            if candidate not in valid:
                await self._send_invalid_tag_notice(thread, message.author, candidate)
                return
            if context.state == "awaiting_source_clan":
                await self._set_source_clan_tag(
                    thread,
                    context,
                    candidate,
                    actor=message.author,
                    prompt_message=None,
                    view=None,
                )
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
        if getattr(outcome, "panel_message_id", None):
            await self._patch_ticket_metadata(
                phase="panel_metadata",
                thread=thread,
                context=context,
                panel_message_id=outcome.panel_message_id,
                status="open",
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
            log_lifecycle(
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
            log_lifecycle(
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
            log_lifecycle(
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
            log_lifecycle(
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
        await self.run_close_backfill()
        # Startup watcher status is included in the global startup summary.

    async def run_close_backfill(self) -> dict[str, int]:
        summary = {
            "scanned": 0,
            "finalized": 0,
            "prompt_required": 0,
            "already_done": 0,
            "unresolved": 0,
            "error": 0,
            "skipped_old": 0,
            "skipped_no_timestamp": 0,
        }
        cutoff = dt.datetime.now(UTC) - dt.timedelta(hours=PROMO_CLOSE_BACKFILL_LOOKBACK_HOURS)
        try:
            rows = await asyncio.to_thread(onboarding_sheets.list_ticket_rows_for_finalization_backfill, "promo")
        except Exception as exc:
            reason = f"{type(exc).__name__}: {exc}"
            log.exception("close_backfill_summary", extra={"flow": "promo", **summary, "result": "error", "reason": reason})
            return summary
        for _, values in rows:
            state = _promo_finalization_state(values)
            if (state.get("finalization_status") or "").lower() == "done":
                summary["already_done"] += 1
                continue
            status = str(values.get("status") or "").strip().lower()
            thread_id = str(values.get("thread_id") or values.get("thread") or "").strip()
            if status != "closed" and not thread_id:
                continue
            stamp, stamp_source = _backfill_row_timestamp(values)
            ticket = values.get("ticket number") or values.get("ticket_number") or values.get("ticket")
            if stamp is None:
                summary["skipped_no_timestamp"] += 1
                log.info(
                    "close_backfill_skip_no_timestamp",
                    extra={"flow": "promo", "thread_id": thread_id, "ticket": ticket},
                )
                continue
            if stamp < cutoff:
                summary["skipped_old"] += 1
                log.debug(
                    "close_backfill_skip_old",
                    extra={
                        "flow": "promo",
                        "thread_id": thread_id,
                        "ticket": ticket,
                        "timestamp_source": stamp_source,
                        "lookback_hours": PROMO_CLOSE_BACKFILL_LOOKBACK_HOURS,
                    },
                )
                continue
            summary["scanned"] += 1
            thread = None
            if thread_id:
                try:
                    tid = int(thread_id)
                    thread = self.bot.get_channel(tid) if hasattr(self.bot, "get_channel") else None
                    if thread is None and hasattr(self.bot, "fetch_channel"):
                        thread = await self.bot.fetch_channel(tid)
                except Exception:
                    thread = None
            player = values.get("username") or "unknown"
            sheet_closed = status == "closed"
            thread_closed = bool(thread is not None and _is_closed_thread(thread))
            if thread is None:
                if not sheet_closed:
                    log.info(
                        "close_backfill_skip_open_unfetchable",
                        extra={"flow": "promo", "thread_id": thread_id, "ticket": ticket, "status": status or "-"},
                    )
                    continue
                summary["unresolved"] += 1
                try:
                    await asyncio.to_thread(onboarding_sheets.update_ticket_finalization_state, "promo", ticket=ticket, thread_id=thread_id, finalization_status="skipped_unresolved", finalization_note="context unresolved during startup/backfill")
                except Exception:
                    summary["error"] += 1
                log.warning("close_context_unresolved", extra={"flow": "promo", "trigger": "startup_backfill", "thread_id": thread_id, "ticket": ticket})
                await _log_promo_failure_row(
                    reason="close_context_unresolved",
                    source="message",
                    thread=SimpleNamespace(id=thread_id, name=values.get("thread_name")),
                    ticket_code=ticket,
                )
                await _send_placement_log_line(flow="promo", outcome="unresolved", ticket=ticket, player=player, trigger="startup_backfill", reason="context_not_found", action="skipped", thread=values.get("thread_name"))
                continue
            if not sheet_closed and not thread_closed:
                log.debug(
                    "close_backfill_skip_open_thread",
                    extra={"flow": "promo", "thread_id": thread_id, "ticket": ticket, "status": status or "-"},
                )
                continue
            context = await self._ensure_context(thread)
            if context is None:
                summary["unresolved"] += 1
                try:
                    await asyncio.to_thread(onboarding_sheets.update_ticket_finalization_state, "promo", ticket=ticket, thread_id=thread_id, finalization_status="skipped_unresolved", finalization_note="context unresolved during startup/backfill")
                except Exception:
                    summary["error"] += 1
                await _log_promo_failure_row(
                    reason="close_context_unresolved",
                    source="message",
                    thread=thread,
                    ticket_code=ticket,
                )
                await _send_placement_log_line(flow="promo", outcome="unresolved", ticket=ticket, player=player, trigger="startup_backfill", reason="context_not_found", action="skipped", thread=getattr(thread, "name", None))
                continue
            try:
                await self._begin_clan_prompt(thread, context, trigger="startup_backfill")
                if context.state == "closed":
                    summary["finalized"] += 1
                else:
                    summary["prompt_required"] += 1
            except Exception:
                summary["error"] += 1
                log.exception("close finalization backfill failed", extra={"flow": "promo", "thread_id": thread_id, "ticket": ticket})
        log.info("close_backfill_summary", extra={"flow": "promo", **summary})
        return summary


async def setup(bot: commands.Bot) -> None:
    existing = bot.get_cog("PromoTicketWatcher")
    if existing is None:
        await bot.add_cog(PromoTicketWatcher(bot))
    elif not isinstance(existing, PromoTicketWatcher):
        raise RuntimeError("cog name collision for PromoTicketWatcher")
