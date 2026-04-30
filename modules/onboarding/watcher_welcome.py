"""Welcome-thread watcher that posts the onboarding questionnaire panel."""

from __future__ import annotations

import logging
import re
import asyncio
from time import monotonic
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set

import discord
from discord import RawReactionActionEvent
from discord.ext import commands

from modules.common import feature_flags
from modules.common import runtime as rt
from modules.common.logs import log as human_log
from modules.onboarding import logs, thread_membership, thread_scopes, welcome_flow
from modules.onboarding.constants import CLAN_TAG_PROMPT_HELPER
from modules.onboarding.ui import panels
from modules.onboarding.controllers.welcome_controller import (
    extract_target_from_message,
    locate_welcome_message,
)
from modules.onboarding.sheet_logging import log_sheet_write
from modules.onboarding.sessions import Session, ensure_session_for_thread
from shared.config import (
    get_admin_role_ids,
    get_guardian_knight_role_ids,
    get_onboarding_log_channel_id,
    get_recruiter_role_ids,
    get_promo_channel_id,
    get_recruitment_coordinator_role_ids,
    get_welcome_channel_id,
    get_ticket_tool_bot_id,
)
from shared.logfmt import channel_label
from shared.logs import log_lifecycle
from modules.recruitment import availability
from shared.sheets import onboarding as onboarding_sheets
from shared.sheets import onboarding_sessions
from shared.sheets import welcome_tickets
from shared.sheets import reservations as reservations_sheets
from shared.sheets import recruitment as recruitment_sheets
from shared.sheets.cache_service import cache as sheets_cache

log = logging.getLogger("c1c.onboarding.welcome_watcher")

_REMINDER_JOB_NAME = "welcome_incomplete_scan"
_REMINDER_INTERVAL_SECONDS = 900
_FIRST_REMINDER_AFTER = timedelta(hours=3)
_WARNING_AFTER = timedelta(hours=24)
_AUTO_CLOSE_AFTER = timedelta(hours=36)

_REMINDER_TASK: asyncio.Task | None = None
_TARGET_CACHE: dict[int, int | None] = {}
_ONBOARDING_LOG_CHANNEL: discord.abc.Messageable | None = None
_ONBOARDING_LOG_CHANNEL_FETCHED = False
_AUTO_CLOSED_THREADS: set[int] = set()

_TRIGGER_PHRASE = "awake by reacting with"
_TICKET_EMOJI = "🎫"

_TICKET_CODE_RE = re.compile(r"(W?\d{4})", re.IGNORECASE)
_PROMO_TICKET_CODE_RE = re.compile(r"([RML]\d{4})", re.IGNORECASE)
_PROMO_THREAD_NAME_RE = re.compile(
    r"^(?P<prefix>[RML])(?P<digits>\d{4})[-_\s]+(?P<slug>[A-Za-z0-9][A-Za-z0-9._-]*)"
    r"(?:[-_\s]+(?P<tag>[A-Za-z0-9]+))?$",
    re.IGNORECASE,
)
_CLOSED_MESSAGE_TOKEN = "ticket closed"
_NO_PLACEMENT_TAG = "NONE"
_WELCOME_HEADERS = onboarding_sheets.WELCOME_HEADERS
_PROMO_TYPE_MAP = {
    "R": "returning player",
    "M": "player move request",
    "L": "clan lead move request",
}


def _normalize_ticket_code(ticket: str | None) -> str:
    token = (ticket or "").strip().lstrip("#")
    if not token:
        return ""

    direct = re.fullmatch(r"(?P<prefix>[Ww]?)(?P<digits>\d{4})", token)
    if direct:
        digits = direct.group("digits")
        return f"W{digits}"

    match = _TICKET_CODE_RE.search(token)
    if match:
        raw = match.group(1).upper()
        digits = raw[1:] if raw.startswith("W") else raw
        if len(digits) == 4 and digits.isdigit():
            return f"W{digits}"
    return ""


def _normalize_promo_ticket(ticket: str | None) -> str:
    token = (ticket or "").strip().lstrip("#")
    if not token:
        return ""
    prefix = token[:1].upper()
    digits = token[1:5]
    if prefix in _PROMO_TYPE_MAP and len(digits) == 4 and digits.isdigit():
        return f"{prefix}{digits}"
    match = _PROMO_TICKET_CODE_RE.search(token)
    if match:
        normalized = match.group(1).upper()
        if normalized[:1] in _PROMO_TYPE_MAP:
            return normalized
    return ""


def parse_welcome_thread_name(name: str | None) -> Optional[ThreadNameParts]:
    if not name:
        return None

    normalized = (name or "").strip()
    match = re.search(r"(?<![A-Za-z0-9])(?P<code>W?\d{4})(?!\d)", normalized, re.IGNORECASE)
    if not match:
        return None

    ticket_code = _normalize_ticket_code(match.group("code"))
    if not ticket_code:
        return None
    prefix = normalized[: match.start()].strip(" -_") or None
    suffix = normalized[match.end():].strip(" -_")

    username = suffix or ""
    clan_tag: Optional[str] = None
    if suffix:
        parts = suffix.split("-", 1)
        username = parts[0].strip(" -_")
        if len(parts) > 1:
            clan_tag = parts[1].strip(" -_") or None

    if not username:
        return None

    return ThreadNameParts(
        ticket_code=ticket_code,
        username=username,
        prefix=prefix,
        clan_tag=clan_tag,
    )


def parse_promo_thread_name(name: str | None) -> Optional["PromoThreadNameParts"]:
    if not name:
        return None

    match = _PROMO_THREAD_NAME_RE.match(name.strip())
    if not match:
        return None

    prefix = match.group("prefix")
    digits = match.group("digits")
    slug = (match.group("slug") or "").strip(" -_")
    ticket_code = _normalize_promo_ticket(f"{prefix}{digits}")
    if not ticket_code:
        return None
    if not slug:
        return None

    promo_type = _PROMO_TYPE_MAP.get(ticket_code[:1], "")
    if not promo_type:
        return None
    clan_tag = (match.group("tag") or "").strip(" -_") or None
    if clan_tag is None:
        slug_parts = re.split(r"[-_\s]+", slug)
        if len(slug_parts) > 1 and slug_parts[-1].isalpha() and slug_parts[-1].isupper():
            clan_tag = slug_parts.pop().strip() or None
            slug = "-".join(part for part in slug_parts if part)

    return PromoThreadNameParts(
        ticket_code=ticket_code,
        username=slug,
        promo_type=promo_type,
        clan_tag=clan_tag,
    )


def build_open_thread_name(ticket_code: str, username: str) -> str:
    return f"{ticket_code}-{username}".strip("-")


def _get_subject_user_from_welcome_message(
    msg: discord.Message | None, *, bot_user_id: int | None = None
) -> discord.Member | None:
    if msg is None:
        return None
    mentions = getattr(msg, "mentions", None) or []
    for candidate in mentions:
        if getattr(candidate, "bot", False):
            continue
        if bot_user_id is not None and getattr(candidate, "id", None) == bot_user_id:
            continue
        return candidate
    return None


def _extract_subject_user_id(
    message: discord.Message,
    *,
    bot_user_id: int | None = None,
    log_on_fallback: bool = False,
) -> int | None:
    # Prefer Discord's parsed mentions; fallback to a simple <@...> regex only if needed.
    if message.mentions:
        for mention in message.mentions:
            if getattr(mention, "bot", False):
                continue
            if bot_user_id is not None and getattr(mention, "id", None) == bot_user_id:
                continue
            try:
                return int(getattr(mention, "id", None))
            except (TypeError, ValueError):
                continue

    match = re.search(r"<@!?(?P<user_id>\d+)>", message.content or "")
    if match:
        try:
            candidate = int(match.group("user_id"))
        except (TypeError, ValueError):
            candidate = None
        if candidate is not None and candidate != bot_user_id:
            return candidate

    if log_on_fallback:
        log.warning(
            "welcome_trigger_missing_recruit_mention",
            extra={"message_id": getattr(message, "id", None), "reason": "no_target"},
        )
    return None


async def resolve_subject_user_id(
    thread: discord.Thread, *, bot_user_id: int | None = None
) -> int | None:
    starter: discord.Message | None = None
    try:
        starter = await locate_welcome_message(thread)
    except Exception:
        log.debug(
            "failed to locate welcome message while resolving subject user",
            exc_info=True,
            extra={"thread_id": getattr(thread, "id", None)},
        )

    candidate: int | None = None
    if bot_user_id is None:
        bot_candidate = getattr(getattr(thread, "guild", None), "me", None)
        try:
            bot_user_id = int(getattr(bot_candidate, "id", None)) if bot_candidate else None
        except (TypeError, ValueError):
            bot_user_id = None

    if starter is not None:
        member = _get_subject_user_from_welcome_message(starter, bot_user_id=bot_user_id)
        if member is not None:
            candidate = getattr(member, "id", None)
        if candidate is None:
            candidate = _extract_subject_user_id(starter, bot_user_id=bot_user_id)

    if candidate is None:
        try:
            owner_id = getattr(thread, "owner_id", None)
            candidate = int(owner_id) if owner_id is not None else None
        except (TypeError, ValueError):
            candidate = None

    return candidate


def build_reserved_thread_name(ticket_code: str, username: str, clan_tag: str) -> str:
    tag = (clan_tag or "").strip().upper()
    return f"Res-{ticket_code}-{username}-{tag}".strip("-")


def build_closed_thread_name(ticket_code: str, username: str, clan_tag: str) -> str:
    tag = (clan_tag or "").strip().upper()
    return f"Closed-{ticket_code}-{username}-{tag}".strip("-")


def _normalize_dt(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def persist_session_for_thread(
    *,
    flow: str,
    ticket_number: str | None,
    thread: discord.Thread,
    user_id: int | str | None,
    username: str | None,
    created_at: datetime,
    panel_message_id: int | None = None,
    append_session_row: bool = True,
) -> None:
    thread_id = int(getattr(thread, "id", 0))
    thread_name = getattr(thread, "name", "")
    created = _normalize_dt(created_at)

    try:
        onboarding_sessions.upsert_session(
            thread_id=thread_id,
            thread_name=thread_name,
            user_id=user_id if user_id is not None else "",
            panel_message_id=panel_message_id,
            updated_at=created,
        )
    except Exception:
        log.exception(
            "failed to persist onboarding session", extra={"thread_id": thread_id, "user_id": user_id}
        )

    numeric_user_id: int | None = None
    try:
        numeric_user_id = int(user_id) if user_id is not None else None
    except (TypeError, ValueError):
        numeric_user_id = None

    if ticket_number and numeric_user_id is not None and append_session_row:
        log_prefix = f"{flow}_session_row" if flow else "session_row"
        try:
            session_result = await asyncio.to_thread(
                onboarding_sheets.append_onboarding_session_row,
                ticket=ticket_number,
                thread_id=thread_id,
                user_id=numeric_user_id,
                flow=flow,
                status="open",
                created_at=created,
            )
            log.info(
                "✅ %s — ticket=%s • user=%s • result=row_%s",
                log_prefix,
                ticket_number,
                username or numeric_user_id,
                session_result,
            )
        except Exception as exc:
            log.error(
                "❌ %s — ticket=%s • user=%s • result=error • reason=%s",
                log_prefix,
                ticket_number,
                username or numeric_user_id,
                exc,
            )

    if numeric_user_id is not None:
        try:
            session = await ensure_session_for_thread(
                numeric_user_id,
                thread_id,
                updated_at=created,
                thread_name=thread_name,
            )
            if session is not None and panel_message_id is not None:
                session.panel_message_id = panel_message_id
                try:
                    await session.asave_to_sheet()
                except Exception:
                    log.exception(
                        "failed to persist onboarding session panel id",
                        extra={"thread_id": thread_id, "user_id": numeric_user_id},
                    )
        except Exception:
            log.exception(
                "failed to ensure onboarding session", extra={"thread_id": thread_id, "user_id": numeric_user_id}
            )


def _session_has_answers(session: Session | None) -> bool:
    if session is None:
        return False
    return bool(getattr(session, "answers", {}))


def _determine_reminder_action(
    now: datetime,
    created_at: datetime,
    session: Session | None,
    *,
    has_answers: bool,
) -> str | None:
    if session and session.completed:
        return None
    if session and session.auto_closed_at:
        return None

    age = now - created_at
    case_suffix = "incomplete" if has_answers else "empty"
    reminder_sent = None
    warning_sent = None
    auto_closed_at = None
    if session:
        reminder_sent = session.first_reminder_at or session.empty_first_reminder_at
        warning_sent = session.warning_sent_at or session.empty_warning_sent_at
        auto_closed_at = session.auto_closed_at

    if age >= _AUTO_CLOSE_AFTER:
        if auto_closed_at is None and warning_sent is not None:
            return f"close_{case_suffix}"
        return None

    if age >= _WARNING_AFTER:
        if warning_sent is None:
            return f"warning_{case_suffix}"
        return None

    if age >= _FIRST_REMINDER_AFTER:
        if reminder_sent is None:
            return f"reminder_{case_suffix}"

    return None


def _ensure_reminder_job(bot: commands.Bot) -> None:
    global _REMINDER_TASK

    runtime = rt.get_active_runtime()
    if runtime is None:
        return

    if _REMINDER_TASK is not None and not _REMINDER_TASK.done():
        return

    job = runtime.scheduler.every(
        seconds=_REMINDER_INTERVAL_SECONDS,
        jitter="small",
        tag="welcome",
        name=_REMINDER_JOB_NAME,
    )

    _REMINDER_TASK = job.do(lambda: _scan_incomplete_threads(bot))


async def _scan_incomplete_threads(bot: commands.Bot) -> None:
    await bot.wait_until_ready()

    now = datetime.now(timezone.utc)

    session_rows_by_thread_id: dict[int, dict[str, object]] = {}
    try:
        session_rows = await onboarding_sessions.aload_all(timeout=15.0)
        session_rows_by_thread_id = onboarding_sessions.index_by_thread_id(session_rows)
    except Exception:
        log.exception("failed to load onboarding sessions for scan", exc_info=True)

    if feature_flags.is_enabled("welcome_dialog") and feature_flags.is_enabled("recruitment_welcome"):
        channel_id = get_welcome_channel_id()
        try:
            channel_int = int(channel_id) if channel_id is not None else None
        except (TypeError, ValueError):
            channel_int = None

        if channel_int is not None:
            threads = await _collect_threads(bot, channel_int, scope_check=thread_scopes.is_welcome_parent)
            for thread in threads:
                await _process_incomplete_thread(
                    bot,
                    thread,
                    now,
                    session_row=session_rows_by_thread_id.get(int(getattr(thread, "id", 0))),
                )

    if feature_flags.is_enabled("promo_enabled") and feature_flags.is_enabled("enable_promo_hook"):
        promo_channel = get_promo_channel_id()
        try:
            promo_channel_int = int(promo_channel) if promo_channel is not None else None
        except (TypeError, ValueError):
            promo_channel_int = None

        if promo_channel_int is not None:
            threads = await _collect_threads(bot, promo_channel_int, scope_check=thread_scopes.is_promo_parent)
            for thread in threads:
                await _process_promo_thread(
                    bot,
                    thread,
                    now,
                    session_row=session_rows_by_thread_id.get(int(getattr(thread, "id", 0))),
                )


async def _collect_threads(
    bot: commands.Bot, channel_id: int, *, scope_check
) -> list[discord.Thread]:
    threads: dict[int, discord.Thread] = {}

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            log.debug("welcome reminder: failed to fetch parent channel", exc_info=True)
            channel = None

    if channel is None:
        return []

    for candidate in getattr(channel, "threads", []) or []:
        if getattr(candidate, "parent_id", None) == channel_id:
            threads[candidate.id] = candidate

    guild = getattr(channel, "guild", None)
    if guild is not None:
        try:
            active_threads = await guild.active_threads()
        except Exception:
            log.debug("welcome reminder: failed to list active threads", exc_info=True)
        else:
            for thread in active_threads:
                if getattr(thread, "parent_id", None) == channel_id:
                    threads.setdefault(thread.id, thread)

    return [thread for thread in threads.values() if scope_check(thread)]


async def _resolve_target_user(thread: discord.Thread, parts: ThreadNameParts) -> tuple[int | None, str]:
    cached = _TARGET_CACHE.get(getattr(thread, "id", 0))
    if cached is not None:
        return cached, parts.username

    message = await locate_welcome_message(thread)
    target_id, _ = extract_target_from_message(message)
    _TARGET_CACHE[getattr(thread, "id", 0)] = target_id
    return target_id, parts.username


async def _persist_reminder_state(session: Session | None, *, action: str, timestamp: datetime) -> None:
    if session is None:
        return

    is_empty_case = "empty" in action

    if action.startswith("reminder"):
        session.first_reminder_at = timestamp
        if is_empty_case:
            session.empty_first_reminder_at = timestamp
    elif action.startswith("warning"):
        session.warning_sent_at = timestamp
        if is_empty_case:
            session.empty_warning_sent_at = timestamp
    elif action.startswith("close"):
        session.auto_closed_at = timestamp

    session._touch(timestamp=timestamp)
    try:
        await session.asave_to_sheet()
    except Exception:
        log.exception(
            "failed to persist welcome reminder state",
            extra={
                "thread_id": getattr(session, "thread_id", None),
                "applicant_id": getattr(session, "applicant_id", None),
                "action": action,
            },
        )


async def _resolve_onboarding_log_channel(bot: commands.Bot) -> discord.abc.Messageable | None:
    global _ONBOARDING_LOG_CHANNEL, _ONBOARDING_LOG_CHANNEL_FETCHED

    if _ONBOARDING_LOG_CHANNEL_FETCHED:
        return _ONBOARDING_LOG_CHANNEL

    _ONBOARDING_LOG_CHANNEL_FETCHED = True
    channel_id = get_onboarding_log_channel_id()
    if not channel_id:
        return None

    try:
        channel_id_int = int(channel_id)
    except (TypeError, ValueError):
        return None

    channel = bot.get_channel(channel_id_int) if bot else None
    if channel is None and bot:
        try:
            channel = await bot.fetch_channel(channel_id_int)
        except Exception:
            log.debug("onboarding log channel fetch failed", exc_info=True)
            channel = None

    if channel is not None and hasattr(channel, "send"):
        _ONBOARDING_LOG_CHANNEL = channel
    else:
        _ONBOARDING_LOG_CHANNEL = None

    return _ONBOARDING_LOG_CHANNEL


def _recruiter_ping() -> str:
    role_ids: set[int] = set()
    role_ids.update(int(rid) for rid in get_recruiter_role_ids() or [] if rid)
    role_ids.update(int(rid) for rid in get_recruitment_coordinator_role_ids() or [] if rid)
    role_ids.update(int(rid) for rid in get_guardian_knight_role_ids() or [] if rid)
    mentions = [f"<@&{rid}>" for rid in sorted(role_ids)]
    return " ".join(mentions).strip()


def _format_inactivity_log(scope: str, case: str, stage: str, ticket: str, user: str) -> str:
    emoji = "⚠️" if stage == "warning" else "❌"
    return f"{emoji} Onboarding {stage} — {scope} • case={case} • ticket={ticket} • user={user}"


async def _post_inactivity_log(
    bot: commands.Bot,
    *,
    scope: str,
    case: str,
    stage: str,
    ticket: str,
    user: str,
) -> None:
    try:
        channel = await _resolve_onboarding_log_channel(bot)
        if channel is None:
            return
        message = _format_inactivity_log(scope, case, stage, ticket, user)
        await channel.send(message)
    except Exception:
        log.debug("failed to post onboarding inactivity log", exc_info=True)


async def _process_incomplete_thread(
    bot: commands.Bot, thread: discord.Thread, now: datetime, *, session_row: dict[str, object] | None = None
) -> None:
    parsed = parse_welcome_thread_name(getattr(thread, "name", None))
    if parsed is None or parsed.state == "closed":
        return

    created_at = _normalize_dt(getattr(thread, "created_at", None))
    applicant_id, _username = await _resolve_target_user(thread, parsed)

    session: Session | None = None
    if applicant_id is not None:
        try:
            session = await ensure_session_for_thread(
                applicant_id,
                int(getattr(thread, "id", 0)),
                updated_at=created_at,
                thread_name=getattr(thread, "name", ""),
                create_if_missing=True,
                preloaded_session_row=session_row, 
            )
        except Exception:
            log.exception(
                "failed to ensure welcome session", extra={"thread_id": getattr(thread, "id", None)}
            )

    has_answers = _session_has_answers(session)
    action = _determine_reminder_action(now, created_at, session, has_answers=has_answers)
    if action is None:
        return

    watcher = bot.get_cog("PromoTicketWatcher") if bot else None
    context = await watcher._ensure_context(thread) if watcher is not None else None
    if context is not None:
        context.user_id = context.user_id or applicant_id
        context.username = parsed.username
        context.ticket_number = parsed.ticket_code
        context.promo_type = context.promo_type or parsed.promo_type

    watcher = bot.get_cog("WelcomeTicketWatcher") if bot else None
    context = await watcher._ensure_context(thread) if watcher is not None else None
    if context is not None:
        context.recruit_id = getattr(session, "applicant_id", None)
        context.recruit_display = parsed.username

    if applicant_id is None:
        log.debug(
            "welcome reminder skipped (no target)",
            extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
        )
        return

    if session is None:
        log.debug(
            "welcome reminder skipped (no session)",
            extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
        )
        return

    mention = f"<@{applicant_id}>"
    recruiter_ping = _recruiter_ping()
    if action == "reminder_empty":
        content = (
            f"Hey {mention}, your welcome ticket is open but we haven't seen any answers yet. "
            "Please click **Open questions** on the panel above and fill them out, or type a message here so we know how to help you find the right clan."
        )
        try:
            await thread.send(content)
        except Exception:
            log.warning(
                "welcome empty reminder send failed",
                exc_info=True,
                extra={"thread_id": getattr(thread, "id", None)},
            )
            return
        await _persist_reminder_state(session, action=action, timestamp=now)
        if watcher and context:
            await watcher._touch_welcome_sheet_for_reminder(
                phase="reminder_3h",
                thread=thread,
                context=context,
                created_at=created_at,
                user_ref=mention,
            )
        log.info(
            "welcome empty reminder posted",
            extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
        )
        return

    if action == "reminder_incomplete":
        content = (
            f"{mention} You started the onboarding questions but haven't finished yet. "
            "Please continue or restart so we can place you correctly."
        )
        try:
            await thread.send(content)
        except Exception:
            log.warning(
                "welcome incomplete reminder send failed",
                exc_info=True,
                extra={"thread_id": getattr(thread, "id", None)},
            )
            return
        await _persist_reminder_state(session, action=action, timestamp=now)
        if watcher and context:
            await watcher._touch_welcome_sheet_for_reminder(
                phase="reminder_3h",
                thread=thread,
                context=context,
                created_at=created_at,
                user_ref=mention,
            )
        log.info(
            "welcome incomplete reminder posted",
            extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
        )
        return

    if action == "warning_empty":
        audience = " ".join(part for part in [mention, recruiter_ping] if part).strip()
        content = (
            f"Quick heads-up, {audience}: your welcome ticket is still empty. "
            "If you don't start the questions or reply in this thread in the next 12 hours, this ticket will be closed for inactivity. "
            "You can always open a new ticket later if you still need help."
        )
        try:
            await thread.send(content)
        except Exception:
            log.warning(
                "welcome empty warning send failed",
                exc_info=True,
                extra={"thread_id": getattr(thread, "id", None)},
            )
            return
        await _persist_reminder_state(session, action=action, timestamp=now)
        if watcher and context:
            await watcher._touch_welcome_sheet_for_reminder(
                phase="reminder_24h",
                thread=thread,
                context=context,
                created_at=created_at,
                user_ref=audience,
            )
        await _post_inactivity_log(
            bot,
            scope="welcome",
            case="empty",
            stage="warning",
            ticket=parsed.ticket_code,
            user=mention,
        )
        log.info(
            "welcome empty warning posted",
            extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
        )
        return

    if action == "warning_incomplete":
        audience = " ".join(part for part in [mention, recruiter_ping] if part).strip()
        content = (
            f"{audience} You haven't finished your onboarding questions yet. "
            "If nothing changes in the next 12 hours, this ticket will be closed for inactivity."
        )
        try:
            await thread.send(content)
        except Exception:
            log.warning(
                "welcome incomplete warning send failed",
                exc_info=True,
                extra={"thread_id": getattr(thread, "id", None)},
            )
            return
        await _persist_reminder_state(session, action=action, timestamp=now)
        if watcher and context:
            await watcher._touch_welcome_sheet_for_reminder(
                phase="reminder_24h",
                thread=thread,
                context=context,
                created_at=created_at,
                user_ref=audience,
            )
        await _post_inactivity_log(
            bot,
            scope="welcome",
            case="incomplete",
            stage="warning",
            ticket=parsed.ticket_code,
            user=mention,
        )
        log.info(
            "welcome incomplete warning posted",
            extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
        )
        return

    if action == "close_empty":
        close_notice = (
            f"{recruiter_ping or 'Recruiters'}, this ticket was closed because onboarding never started.\n"
            f"Please remove {mention} from the server.\n"
            "If you still need a clan later, you're welcome to open a new ticket."
        )

        await _persist_reminder_state(session, action=action, timestamp=now)
        watcher = bot.get_cog("WelcomeTicketWatcher")
        if watcher is not None:
            context = await watcher._ensure_context(thread)
            if context:
                context.recruit_id = getattr(session, "applicant_id", None)
                context.recruit_display = parsed.username
                closed_name = build_closed_thread_name(
                    parsed.ticket_code, parsed.username, _NO_PLACEMENT_TAG
                )
                await watcher.auto_close_for_inactivity(
                    thread,
                    context,
                    notice=close_notice,
                    closed_name=closed_name,
                    final_tag=_NO_PLACEMENT_TAG,
                )
                await _post_inactivity_log(
                    bot,
                    scope="welcome",
                    case="empty",
                    stage="auto-close",
                    ticket=parsed.ticket_code,
                    user=mention,
                )
                log.info(
                    "welcome empty auto-close completed",
                    extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
                )
                return
        await _post_inactivity_log(
            bot,
            scope="welcome",
            case="empty",
            stage="auto-close",
            ticket=parsed.ticket_code,
            user=mention,
        )
        log.info(
            "welcome empty auto-close completed",
            extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
        )
        return

    if action == "close_incomplete":
        close_notice = (
            f"{recruiter_ping or 'Recruiters'}: onboarding was started but not completed. "
            f"Please remove {mention} from the server."
        )

        await _persist_reminder_state(session, action=action, timestamp=now)
        watcher = bot.get_cog("WelcomeTicketWatcher")
        if watcher is not None:
            context = await watcher._ensure_context(thread)
            if context:
                context.recruit_id = getattr(session, "applicant_id", None)
                context.recruit_display = parsed.username
                closed_name = build_closed_thread_name(
                    parsed.ticket_code, parsed.username, _NO_PLACEMENT_TAG
                )
                await watcher.auto_close_for_inactivity(
                    thread,
                    context,
                    notice=close_notice,
                    closed_name=closed_name,
                    final_tag=_NO_PLACEMENT_TAG,
                )
                await _post_inactivity_log(
                    bot,
                    scope="welcome",
                    case="incomplete",
                    stage="auto-close",
                    ticket=parsed.ticket_code,
                    user=mention,
                )
                log.info(
                    "welcome incomplete auto-close completed",
                    extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
                )
                return
        await _post_inactivity_log(
            bot,
            scope="welcome",
            case="incomplete",
            stage="auto-close",
            ticket=parsed.ticket_code,
            user=mention,
        )
        log.info(
            "welcome incomplete auto-close completed",
            extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
        )


async def _process_promo_thread(
    bot: commands.Bot, thread: discord.Thread, now: datetime, *, session_row: dict[str, object] | None = None
) -> None:
    parsed = parse_promo_thread_name(getattr(thread, "name", None))
    if parsed is None:
        return

    created_at = _normalize_dt(getattr(thread, "created_at", None))
    applicant_id, _username = await _resolve_target_user(thread, parsed)
    session: Session | None = None
    if applicant_id is not None:
        try:
            session = await ensure_session_for_thread(
                applicant_id,
                int(getattr(thread, "id", 0)),
                updated_at=created_at,
                thread_name=getattr(thread, "name", ""),
                create_if_missing=False,
                preloaded_session_row=session_row,
            )
        except Exception:
            log.exception(
                "failed to ensure promo session", extra={"thread_id": getattr(thread, "id", None)}
            )
    has_answers = _session_has_answers(session)
    action = _determine_reminder_action(now, created_at, session, has_answers=has_answers)

    if action is None:
        return

    if applicant_id is None:
        log.debug(
            "promo inactivity reminder skipped (no target)",
            extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
        )
        return

    if session is None:
        log.debug(
            "promo inactivity reminder skipped (no session)",
            extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
        )
        return

    mention = f"<@{applicant_id}>"
    recruiter_ping = _recruiter_ping()
    if action == "reminder_empty":
        content = (
            f"Hey {mention}, your move request ticket is open but we haven't seen any details yet. "
            "Please click **Open questions** on the panel above or type a message here so we can help with your clan move."
        )
        try:
            await thread.send(content)
        except Exception:
            log.warning(
                "promo empty reminder send failed",
                exc_info=True,
                extra={"thread_id": getattr(thread, "id", None)},
            )
            return
        await _persist_reminder_state(session, action=action, timestamp=now)
        if watcher and context:
            await watcher._touch_promo_sheet_for_reminder(
                phase="reminder_3h",
                thread=thread,
                context=context,
                created_at=created_at,
                user_ref=mention,
            )
        log.info(
            "promo empty reminder posted",
            extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
        )
        return

    if action == "reminder_incomplete":
        content = (
            f"{mention} You started sharing move details but didn't finish. "
            "Please continue or restart so we can review your promo request."
        )
        try:
            await thread.send(content)
        except Exception:
            log.warning(
                "promo incomplete reminder send failed",
                exc_info=True,
                extra={"thread_id": getattr(thread, "id", None)},
            )
            return
        await _persist_reminder_state(session, action=action, timestamp=now)
        if watcher and context:
            await watcher._touch_promo_sheet_for_reminder(
                phase="reminder_3h",
                thread=thread,
                context=context,
                created_at=created_at,
                user_ref=mention,
            )
        log.info(
            "promo incomplete reminder posted",
            extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
        )
        return

    if action == "warning_empty":
        audience = " ".join(part for part in [mention, recruiter_ping] if part).strip()
        content = (
            f"Quick heads-up, {audience}: your promo ticket is still empty. "
            "If you don't start the questions or reply here in the next 12 hours, this ticket will be closed for inactivity. "
            "You can always open a new ticket later if you still want to move clans."
        )
        try:
            await thread.send(content)
        except Exception:
            log.warning(
                "promo empty warning send failed",
                exc_info=True,
                extra={"thread_id": getattr(thread, "id", None)},
            )
            return
        await _persist_reminder_state(session, action=action, timestamp=now)
        if watcher and context:
            await watcher._touch_promo_sheet_for_reminder(
                phase="reminder_24h",
                thread=thread,
                context=context,
                created_at=created_at,
                user_ref=audience,
            )
        await _post_inactivity_log(
            bot,
            scope="promo",
            case="empty",
            stage="warning",
            ticket=parsed.ticket_code,
            user=mention,
        )
        log.info(
            "promo empty warning posted",
            extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
        )
        return

    if action == "warning_incomplete":
        audience = " ".join(part for part in [mention, recruiter_ping] if part).strip()
        content = (
            f"{audience} Your promo ticket still needs more details. "
            "If nothing changes in the next 12 hours, this ticket will be closed for inactivity."
        )
        try:
            await thread.send(content)
        except Exception:
            log.warning(
                "promo incomplete warning send failed",
                exc_info=True,
                extra={"thread_id": getattr(thread, "id", None)},
            )
            return
        await _persist_reminder_state(session, action=action, timestamp=now)
        if watcher and context:
            await watcher._touch_promo_sheet_for_reminder(
                phase="reminder_24h",
                thread=thread,
                context=context,
                created_at=created_at,
                user_ref=audience,
            )
        await _post_inactivity_log(
            bot,
            scope="promo",
            case="incomplete",
            stage="warning",
            ticket=parsed.ticket_code,
            user=mention,
        )
        log.info(
            "promo incomplete warning posted",
            extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
        )
        return

    if action == "close_empty":
        close_notice = (
            f"Promo ticket closed due to inactivity.\n{recruiter_ping or 'Recruiters'}: "
            "No move details were provided.\n"
            "If you still want to request a move later, feel free to open a new promo ticket anytime."
        )

        await _persist_reminder_state(session, action=action, timestamp=now)
        watcher = bot.get_cog("PromoTicketWatcher")
        if watcher is not None:
            context = await watcher._ensure_context(thread)
            if context:
                closed_name = build_closed_thread_name(
                    parsed.ticket_code, parsed.username, _NO_PLACEMENT_TAG
                )
                await watcher.auto_close_for_inactivity(
                    thread,
                    context,
                    notice=close_notice,
                    closed_name=closed_name,
                )
                await _post_inactivity_log(
                    bot,
                    scope="promo",
                    case="empty",
                    stage="auto-close",
                    ticket=parsed.ticket_code,
                    user=mention,
                )
                log.info(
                    "promo empty auto-close completed",
                    extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
                )
                return
        await _post_inactivity_log(
            bot,
            scope="promo",
            case="empty",
            stage="auto-close",
            ticket=parsed.ticket_code,
            user=mention,
        )
        log.info(
            "promo empty auto-close completed",
            extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
        )
        return

    if action == "close_incomplete":
        close_notice = (
            f"Promo ticket closed due to inactivity.\n{recruiter_ping or 'Recruiters'}: "
            "The move details were started but not completed.\n"
            "If you still want to request a move later, feel free to open a new promo ticket anytime."
        )

        await _persist_reminder_state(session, action=action, timestamp=now)
        watcher = bot.get_cog("PromoTicketWatcher")
        if watcher is not None:
            context = await watcher._ensure_context(thread)
            if context:
                closed_name = build_closed_thread_name(
                    parsed.ticket_code, parsed.username, _NO_PLACEMENT_TAG
                )
                await watcher.auto_close_for_inactivity(
                    thread,
                    context,
                    notice=close_notice,
                    closed_name=closed_name,
                )
                await _post_inactivity_log(
                    bot,
                    scope="promo",
                    case="incomplete",
                    stage="auto-close",
                    ticket=parsed.ticket_code,
                    user=mention,
                )
                log.info(
                    "promo incomplete auto-close completed",
                    extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
                )
                return
        await _post_inactivity_log(
            bot,
            scope="promo",
            case="incomplete",
            stage="auto-close",
            ticket=parsed.ticket_code,
            user=mention,
        )
        log.info(
            "promo incomplete auto-close completed",
            extra={"thread_id": getattr(thread, "id", None), "ticket": parsed.ticket_code},
        )


async def rename_thread_to_reserved(
    thread: discord.Thread, clan_tag: str
) -> bool:
    """Rename ``thread`` to the reserved naming pattern if applicable."""

    parts = parse_welcome_thread_name(getattr(thread, "name", None))
    normalized_tag = (clan_tag or "").strip().upper()

    if parts is None:
        thread_name = getattr(thread, "name", "unknown")
        log.error(
            "❌ welcome_reserve_rename_error — ticket=unknown • tag=%s • thread=%s • result=skipped_unparsed",
            normalized_tag,
            thread_name,
        )
        human_log.human(
            "error",
            (
                "❌ welcome_reserve_rename_error — scope=welcome "
                f"• ticket=unknown • tag={normalized_tag} • thread={thread_name} "
                "• reason=thread_name_unparsed"
            ),
        )
        return False

    if parts.state == "closed":
        log.info(
            "⚠️ welcome_reserve_rename — ticket=%s • user=%s • tag=%s • result=skipped_closed",
            parts.ticket_code,
            parts.username,
            normalized_tag,
        )
        return False

    new_name = build_reserved_thread_name(parts.ticket_code, parts.username, normalized_tag)
    if getattr(thread, "name", None) == new_name:
        log.info(
            "✅ welcome_reserve_rename — ticket=%s • user=%s • tag=%s • result=already_reserved",
            parts.ticket_code,
            parts.username,
            normalized_tag,
        )
        return True

    try:
        await thread.edit(name=new_name)
    except Exception:
        log.exception(
            "failed to rename welcome thread for reservation",
            extra={
                "thread_id": getattr(thread, "id", None),
                "ticket": parts.ticket_code,
                "tag": normalized_tag,
            },
        )
        log.warning(
            "⚠️ welcome_reserve_rename — ticket=%s • user=%s • tag=%s • result=rename_failed",
            parts.ticket_code,
            parts.username,
            normalized_tag,
        )
        return False

    log.info(
        "✅ welcome_reserve_rename — ticket=%s • user=%s • tag=%s • result=renamed_to_res",
        parts.ticket_code,
        parts.username,
        normalized_tag,
    )
    return True


@dataclass(frozen=True)
class ThreadNameParts:
    ticket_code: str
    username: str
    prefix: Optional[str] = None
    clan_tag: Optional[str] = None

    @property
    def state(self) -> str:
        prefix = (self.prefix or "").strip().lower()
        if prefix.startswith("closed"):
            return "closed"
        if prefix.startswith("res"):
            return "reserved"
        return "open"


@dataclass(frozen=True)
class PromoThreadNameParts:
    ticket_code: str
    username: str
    promo_type: str
    clan_tag: Optional[str] = None


@dataclass(frozen=True)
class PanelOutcome:
    result: str
    reason: str | None
    ticket_code: str | None
    thread_name: str | None
    elapsed_ms: int
    panel_message_id: int | None = None


@dataclass(slots=True)
class TicketContext:
    thread_id: int
    ticket_number: str
    username: str
    recruit_id: Optional[int] = None
    recruit_display: Optional[str] = None
    state: str = "open"
    prompt_message_id: Optional[int] = None
    final_clan: Optional[str] = None
    reservation_label: Optional[str] = None
    close_source: str = "ticket_tool"
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    ticket_tool_close_detected: bool = False
    row_created_during_close: bool = False


@dataclass(frozen=True)
class ReservationDecision:
    label: str
    status: Optional[str]
    open_deltas: Dict[str, int]
    recompute_tags: List[str]


@dataclass(slots=True)
class _ClanMathRowSnapshot:
    tag: str
    row_number: int | None
    values: Dict[str, str]


def _determine_reservation_decision(
    final_tag: str,
    reservation_row: reservations_sheets.ReservationRow | None,
    *,
    no_placement_tag: str,
    final_is_real: bool,
    consume_open_spot: bool = True,
    previous_final: str | None = None,
) -> ReservationDecision:
    normalized_final = (final_tag or "").strip().upper()
    normalized_previous = (previous_final or "").strip().upper()
    open_deltas: Dict[str, int] = {}
    recompute: List[str] = []

    if reservation_row is None:
        if (
            normalized_previous
            and normalized_previous != no_placement_tag
            and normalized_previous != normalized_final
        ):
            open_deltas[normalized_previous] = open_deltas.get(normalized_previous, 0) + 1
            recompute.append(normalized_previous)
        if (
            consume_open_spot
            and final_is_real
            and normalized_final
            and normalized_final != no_placement_tag
        ):
            open_deltas[normalized_final] = -1
            recompute.append(normalized_final)
        return ReservationDecision("none", None, open_deltas, recompute)

    reservation_tag = reservation_row.normalized_clan_tag
    if not reservation_tag:
        reservation_tag = (reservation_row.clan_tag or "").strip().upper()

    if normalized_final == no_placement_tag:
        label = "cancelled"
        status = "cancelled"
        if reservation_tag:
            open_deltas[reservation_tag] = open_deltas.get(reservation_tag, 0) + 1
            recompute.append(reservation_tag)
        return ReservationDecision(label, status, open_deltas, recompute)

    if reservation_tag and reservation_tag == normalized_final:
        label = "same"
        status = "closed_same_clan"
        if reservation_tag:
            recompute.append(reservation_tag)
        return ReservationDecision(label, status, open_deltas, recompute)

    label = "other"
    status = "closed_other_clan"
    if reservation_tag:
        open_deltas[reservation_tag] = open_deltas.get(reservation_tag, 0) + 1
        recompute.append(reservation_tag)
    if final_is_real and normalized_final and normalized_final != reservation_tag:
        open_deltas[normalized_final] = open_deltas.get(normalized_final, 0) - 1
        recompute.append(normalized_final)
    return ReservationDecision(label, status, open_deltas, recompute)


def _normalize_clan_tag_key(tag: str | None) -> str:
    text = "" if tag is None else str(tag).strip().upper()
    normalized = "".join(ch for ch in text if ch.isalnum())
    return normalized


def _clan_tags_for_logging(
    final_tag: str,
    decision: ReservationDecision,
    *,
    no_placement_tag: str,
    final_is_real: bool,
) -> set[str]:
    tags: set[str] = set(decision.open_deltas)
    tags.update(decision.recompute_tags)
    if final_is_real and final_tag and final_tag != no_placement_tag:
        tags.add(final_tag)
    return {tag for tag in tags if tag and tag != no_placement_tag}


def _normalize_clan_math_targets(tags: set[str]) -> OrderedDict[str, str]:
    ordered: "OrderedDict[str, str]" = OrderedDict()
    for tag in sorted(tags):
        key = _normalize_clan_tag_key(tag)
        if not key or key in ordered:
            continue
        ordered[key] = tag
    return ordered


def _clan_math_column_indices() -> Dict[str, int]:
    header_map = recruitment_sheets.get_clan_header_map()
    open_index = header_map.get(
        "open_spots", recruitment_sheets.FALLBACK_OPEN_SPOTS_INDEX
    )
    return {
        "open_spots": open_index,
        "AF": 31,
        "AG": 32,
        "AH": 33,
        "AI": 34,
    }


def _capture_clan_snapshots(
    targets: "OrderedDict[str, str]",
    column_map: Dict[str, int],
) -> Dict[str, _ClanMathRowSnapshot]:
    snapshots: Dict[str, _ClanMathRowSnapshot] = {}
    for key, tag in targets.items():
        if not key:
            continue
        try:
            entry = recruitment_sheets.find_clan_row(tag)
        except Exception:
            log.exception(
                "failed to capture clan row for logging",
                extra={"clan_tag": tag},
            )
            continue
        if entry is None:
            continue
        sheet_row, row_values = entry
        tag_cell = row_values[2] if len(row_values) > 2 else tag
        display_tag = (tag_cell or tag or "").strip() or tag
        values: Dict[str, str] = {}
        for label, index in column_map.items():
            if index < 0:
                values[label] = ""
                continue
            cell_value = row_values[index] if index < len(row_values) else ""
            values[label] = str(cell_value or "").strip()
        snapshots[key] = _ClanMathRowSnapshot(
            tag=display_tag,
            row_number=sheet_row,
            values=values,
        )
    return snapshots


def _format_snapshot_value(value: str | None) -> str:
    if value is None:
        return "-"
    text = " ".join(str(value).split())
    return text or "-"


def _format_metric(
    label: str,
    before: _ClanMathRowSnapshot | None,
    after: _ClanMathRowSnapshot | None,
) -> str:
    before_value = before.values.get(label) if before else None
    after_value = after.values.get(label) if after else None
    return (
        f"{label}: {_format_snapshot_value(before_value)} → "
        f"{_format_snapshot_value(after_value)}"
    )


def _format_clan_row_line(
    fallback_tag: str,
    before: _ClanMathRowSnapshot | None,
    after: _ClanMathRowSnapshot | None,
) -> str:
    tag_label = (after.tag if after else (before.tag if before else fallback_tag)).strip()
    tag_label = tag_label or fallback_tag or "unknown"
    row_number = after.row_number if after else (before.row_number if before else None)
    row_label = f"row {row_number}" if row_number else "row ?"
    if before is None and after is None:
        return f"- {tag_label} {row_label}: snapshot unavailable"
    metrics: List[str] = []
    if before is not None or after is not None:
        metrics.append(_format_metric("open_spots", before, after))
    for column in ("AF", "AG", "AH", "AI"):
        metrics.append(_format_metric(column, before, after))
    primary = metrics[0] if metrics else "open_spots: - → -"
    extras = ", ".join(metrics[1:]) if len(metrics) > 1 else ""
    if extras:
        return f"- {tag_label} {row_label}: {primary} ({extras})"
    return f"- {tag_label} {row_label}: {primary}"


def _build_clan_math_row_lines(
    targets: "OrderedDict[str, str]",
    before: Dict[str, _ClanMathRowSnapshot],
    after: Dict[str, _ClanMathRowSnapshot],
) -> List[str]:
    lines: List[str] = []
    for key, original in targets.items():
        lines.append(
            _format_clan_row_line(original, before.get(key), after.get(key))
        )
    return lines


def _normalize_finalize_result(result: str | None) -> str:
    token = (result or "").strip().lower()
    if token in {"ok", "fail", "error"}:
        return token
    if token in {"skip", "skipped"}:
        return "fail"
    return "error"


async def _log_clan_math_event(
    context: TicketContext,
    *,
    final_display: str,
    reservation_label: str,
    reservation_row: reservations_sheets.ReservationRow | None,
    result: str,
    reason: str | None,
    row_change_lines: List[str],
) -> None:
    normalized_result = _normalize_finalize_result(result)
    source = (context.close_source or "ticket_tool").strip() or "ticket_tool"
    if reservation_row is not None:
        reservation_field = (
            f"reservation=row{reservation_row.row_number}"
            f"({reservation_label or 'unknown'})"
        )
    else:
        reservation_field = f"reservation={reservation_label or 'none'}"
    reason_suffix = f", reason={reason}" if reason else ""
    header = (
        f"{context.ticket_number} • {context.username} → {final_display} "
        f"(source={source}, {reservation_field}, result={normalized_result}{reason_suffix})"
    )
    mentions: str = ""
    if normalized_result in {"fail", "error"}:
        role_ids = sorted(get_admin_role_ids())
        if role_ids:
            mentions = " " + " ".join(f"<@&{rid}>" for rid in role_ids)
    if mentions:
        header = f"{header}{mentions}"
    lines = [header]
    if not row_change_lines:
        lines.append("- no clan rows updated")
    else:
        lines.extend(row_change_lines)
    message = "\n".join(lines)
    try:
        await rt.send_log_message(message)
    except Exception:
        log.exception(
            "failed to send clan math log",
            extra={"ticket": context.ticket_number, "result": normalized_result},
        )
async def _send_runtime(message: str) -> None:
    try:
        await rt.send_log_message(message)
    except Exception:  # pragma: no cover - runtime notification best-effort
        log.warning("failed to send welcome watcher log message", exc_info=True)


def _channel_readable_label(bot: commands.Bot, channel_id: int | None) -> str:
    if channel_id is None:
        return "#unknown"
    try:
        cid = int(channel_id)
    except (TypeError, ValueError):
        return f"#{channel_id}"

    guild: discord.Guild | None = None
    channel = bot.get_channel(cid)
    if channel is not None:
        guild = getattr(channel, "guild", None)
    if guild is None:
        for candidate in getattr(bot, "guilds", []):
            try:
                if candidate.get_channel(cid):
                    guild = candidate
                    break
                getter = getattr(candidate, "get_thread", None)
                if callable(getter) and getter(cid):
                    guild = candidate
                    break
            except Exception:
                continue
    if guild is not None:
        try:
            return channel_label(guild, cid)
        except Exception:
            pass
    return f"#{cid}"


async def post_open_questions_panel(
    bot: commands.Bot,
    thread: discord.Thread,
    *,
    actor: discord.abc.User | None,
    flow: str = "welcome",
    ticket_code: str | None = None,
    trigger_message: discord.Message | None = None,
    subject_user_id: int | None = None,
) -> PanelOutcome:
    start = monotonic()

    def _elapsed() -> int:
        return int((monotonic() - start) * 1000)

    thread_name = getattr(thread, "name", None)

    resolution = welcome_flow.resolve_onboarding_flow(thread)
    normalized_flow = (flow or resolution.flow or "welcome").strip().lower()

    promo_flows = {"promo.r", "promo.m", "promo.l"}
    invalid_reason: str | None = None
    if normalized_flow.startswith("promo"):
        if normalized_flow not in promo_flows:
            invalid_reason = "invalid_flow_key"
        elif not resolution.flow:
            invalid_reason = resolution.error or "scope_or_role"
        else:
            normalized_flow = resolution.flow
    elif resolution.flow:
        normalized_flow = resolution.flow

    parser = parse_promo_thread_name if normalized_flow.startswith("promo") else parse_welcome_thread_name
    parsed_parts = parser(thread_name)
    if ticket_code is None:
        ticket_code = getattr(parsed_parts, "ticket_code", None)

    parent_channel = getattr(thread, "parent", None)
    question_count, schema_version = logs.question_stats(normalized_flow)

    async def _emit(result: str | None = None, reason: str | None = None) -> None:
        payload: dict[str, object] = {
            "ticket": ticket_code or thread,
            "actor": actor,
            "channel": parent_channel,
            "questions": question_count,
            "schema_version": schema_version,
            "scope": normalized_flow,
        }
        if result is not None:
            payload["result"] = result
        if reason is not None:
            payload["reason"] = reason
        await logs.log_onboarding_panel_lifecycle(event="open", **payload)

    if invalid_reason is not None:
        await _emit(result="error", reason=invalid_reason)
        return PanelOutcome("error", invalid_reason, ticket_code, thread_name, _elapsed())

    joined, join_error = await thread_membership.ensure_thread_membership(thread)
    if not joined:
        if join_error is not None:
            log.warning("failed to join welcome thread", exc_info=True)
        await _emit(result="error", reason="thread_join_failed")
        return PanelOutcome("error", "thread_join_failed", ticket_code, thread_name, _elapsed())

    bot_user_id = getattr(getattr(bot, "user", None), "id", None)
    existing_panel = await panels.find_panel_message(thread, bot_user_id=bot_user_id)

    def _has_open_button(message: discord.Message | None) -> bool:
        if message is None:
            return False
        for row in getattr(message, "components", []) or []:
            for component in getattr(row, "children", []) or []:
                if getattr(component, "custom_id", None) == panels.OPEN_QUESTIONS_CUSTOM_ID:
                    return True
        for component in getattr(message, "components", []) or []:
            if getattr(component, "custom_id", None) == panels.OPEN_QUESTIONS_CUSTOM_ID:
                return True
        return False

    if existing_panel is not None:
        if not _has_open_button(existing_panel):
            try:
                await existing_panel.edit(view=panels.OpenQuestionsPanelView())
            except Exception:
                log.debug("failed to attach open questions view to existing panel", exc_info=True)
        await _emit(result="skipped", reason="panel_exists")
        return PanelOutcome("skipped", "panel_exists", ticket_code, thread_name, _elapsed())

    view = panels.OpenQuestionsPanelView()
    content = "Ready when you are — tap below to open the onboarding questions."
    panel_message: discord.Message | None = None
    try:
        panel_message = await thread.send(content, view=view)
    except Exception:
        log.exception("failed to post onboarding panel message")
        await _emit(result="error", reason="panel_send_failed")
        return PanelOutcome("error", "panel_send_failed", ticket_code, thread_name, _elapsed())

    if ticket_code:
        await _emit(result="panel_created")

        panel_message_id = getattr(panel_message, "id", None)
        if subject_user_id is None:
            subject_user_id = await resolve_subject_user_id(thread, bot_user_id=bot_user_id)
        if subject_user_id is None and trigger_message is not None:
            subject_user_id = _extract_subject_user_id(
                trigger_message, bot_user_id=bot_user_id, log_on_fallback=True
            )

        flow_suffix = f" • flow={normalized_flow}" if normalized_flow.startswith("promo") else ""
        if subject_user_id is None:
            log.warning(
                "onboarding_session_save_skipped • reason=no_subject_user%s • thread_id=%s",
                flow_suffix,
                thread.id,
            )
        else:
            await persist_session_for_thread(
                flow=normalized_flow,
                ticket_number=ticket_code,
                thread=thread,
                user_id=subject_user_id,
                username=None,
                created_at=datetime.now(timezone.utc),
                panel_message_id=panel_message_id,
                append_session_row=False,
            )
            if normalized_flow == "welcome":
                try:
                    ticket_number, username = (getattr(thread, "name", "") or "").split("-", 1)
                    ticket_number = ticket_number.strip()
                    username = username.strip()
                    await welcome_tickets.save(ticket_number=ticket_number, username=username)
                except Exception:
                    log.exception(
                        "failed to persist welcome ticket log", extra={"thread_id": getattr(thread, "id", None)}
                    )

        return PanelOutcome(
            "panel_created",
            None,
            ticket_code,
            thread_name,
            _elapsed(),
            panel_message_id,
        )

    expected_patterns = "W####-Name | ####-Name | Res-W####-Name-CLAN | Closed-W####-Name-CLAN | R/M/L####-Name"
    log.warning(
        "failed to parse ticket context for panel post",
        extra={
            "reason": "ticket_not_parsed",
            "thread_id": getattr(thread, "id", None),
            "thread_name": thread_name,
            "flow": normalized_flow,
            "expected_patterns": expected_patterns,
        },
    )
    await _emit(result="skipped", reason="ticket_not_parsed")
    return PanelOutcome("skipped", "ticket_not_parsed", ticket_code, thread_name, _elapsed())

def _log_finalize_summary(
    context: TicketContext,
    thread: discord.Thread,
    *,
    final_display: str,
    reservation_label: str,
    result: str,
    reason: str | None = None,
) -> None:
    channel_ref = _channel_readable_label(getattr(thread, "guild", None), getattr(thread, "id", None))
    reason_suffix = f" • reason={reason}" if reason else ""
    log.info(
        "ℹ️ onboarding_finalize_reconcile — ticket=%s • user=%s • clan=%s • reservation=%s • channel=%s • result=%s%s",
        context.ticket_number,
        context.username,
        final_display,
        reservation_label,
        channel_ref,
        result,
        reason_suffix,
    )
    human_level = "info" if result == "ok" else "warning"
    emoji = "🧭" if result == "ok" else "⚠️"
    source_suffix = "" if context.close_source == "ticket_tool" else f" • source={context.close_source}"
    human_log.human(
        human_level,
        (
            f"{emoji} onboarding_finalize — ticket={context.ticket_number} "
            f"• user={context.username} • clan={final_display} • reservation={reservation_label} "
            f"• result={result}{source_suffix}{reason_suffix}"
        ),
    )


def _actor_id(actor: discord.abc.User | None) -> int | None:
    if actor is None:
        return None
    identifier = getattr(actor, "id", None)
    try:
        return int(identifier) if identifier is not None else None
    except (TypeError, ValueError):
        return None


def _collect_role_ids(member: discord.Member | None) -> set[int]:
    if member is None:
        return set()
    role_ids: set[int] = set()
    for role in getattr(member, "roles", ()) or ():
        rid = getattr(role, "id", None)
        if rid is None:
            continue
        try:
            role_ids.add(int(rid))
        except (TypeError, ValueError):
            continue
    return role_ids


class WelcomeWatcher(commands.Cog):
    """Gated watcher that attaches the persistent welcome questionnaire panel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.channel_id: int | None = None
        coordinator_roles = get_recruitment_coordinator_role_ids()
        guardian_roles = get_guardian_knight_role_ids()
        self._staff_role_ids = set(coordinator_roles) | set(guardian_roles)
        self._onb_registered: bool = False
        self._onb_reg_error: str | None = None
        self._announced = False
        self.ticket_tool_id = get_ticket_tool_bot_id()

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        # Guard against firing multiple times on reconnects
        if self._announced:
            return
        self._announced = True

        channel_id = get_welcome_channel_id()
        if not channel_id:
            line = log_lifecycle(
                log,
                "welcome",
                "enabled",
                scope_label="Welcome watcher",
                emoji="📴",
                result="disabled",
                reason="missing_channel_id",
            )
            if line:
                asyncio.create_task(_send_runtime(line))
            return

        try:
            channel_id_int = int(channel_id)
        except (TypeError, ValueError):
            self.channel_id = None
            line = log_lifecycle(
                log,
                "welcome",
                "enabled",
                scope_label="Welcome watcher",
                emoji="⚠️",
                result="error",
                reason="invalid_channel_id",
                channel_id=channel_id,
            )
            if line:
                asyncio.create_task(_send_runtime(line))
            return

        self.channel_id = channel_id_int

        if not feature_flags.is_enabled("welcome_dialog"):
            line = log_lifecycle(
                log,
                "welcome",
                "enabled",
                scope_label="Welcome watcher",
                emoji="📴",
                result="disabled",
                reason="feature_welcome_dialog_off",
            )
            if line:
                asyncio.create_task(_send_runtime(line))
            return

        if not feature_flags.is_enabled("recruitment_welcome"):
            line = log_lifecycle(
                log,
                "welcome",
                "enabled",
                scope_label="Welcome watcher",
                emoji="📴",
                result="disabled",
                reason="feature_recruitment_welcome_off",
            )
            if line:
                asyncio.create_task(_send_runtime(line))
            return

        self._register_persistent_view()

        if self._onb_registered:
            label = _channel_readable_label(self.bot, self.channel_id)
            line = log_lifecycle(
                log,
                "welcome",
                "enabled",
                scope_label="Welcome watcher",
                emoji="✅",
                channel=label,
                channel_id=self.channel_id,
            )
            if line:
                asyncio.create_task(_send_runtime(line))
        else:
            reason = self._onb_reg_error or "unknown"
            line = log_lifecycle(
                log,
                "welcome",
                "enabled",
                scope_label="Welcome watcher",
                emoji="⚠️",
                result="error",
                channel_id=self.channel_id,
                reason=reason,
            )
            if line:
                asyncio.create_task(_send_runtime(line))

    def _register_persistent_view(self) -> None:
        registration = panels.register_persistent_views(self.bot)

        view_name = registration.get("view") or "OpenQuestionsPanelView"
        components = registration.get("components") or "buttons:0,textinputs:0,selects:0"
        threads_default = registration.get("threads_default")
        duration_ms = registration.get("duration_ms")
        registered = bool(registration.get("registered"))
        duplicate = bool(registration.get("duplicate_registration"))
        error = registration.get("error")

        def _component_count(kind: str, fallback: int) -> int:
            if isinstance(registration.get(kind), int):
                return int(registration[kind])
            if isinstance(components, str):
                for part in components.split(","):
                    if not part:
                        continue
                    if ":" not in part:
                        continue
                    key, value = part.split(":", 1)
                    if key.strip() == kind:
                        try:
                            return int(value)
                        except (TypeError, ValueError):
                            return fallback
            return fallback

        buttons = _component_count("buttons", 2)
        selects = _component_count("selects", 0)
        textinputs = _component_count("textinputs", 0)

        payload: dict[str, object] = {
            "view": view_name,
            "buttons": buttons,
            "selects": selects,
            "textinputs": textinputs,
            "result": "ok" if registered else "error",
        }
        if threads_default is not None:
            payload["threads_default"] = threads_default
        if isinstance(duration_ms, int):
            payload["duration"] = f"{duration_ms}ms"
        if duplicate:
            payload["duplicate_registration"] = True
        if error is not None:
            payload["reason"] = f"{error.__class__.__name__}: {error}"

        log_lifecycle(
            log,
            "welcome",
            "view_registered",
            scope_label="Welcome watcher",
            **payload,
        )

        if registered:
            self._onb_registered = True
            self._onb_reg_error = None
        else:
            reason = payload.get("reason")
            if isinstance(reason, str):
                self._onb_reg_error = reason
            else:
                self._onb_reg_error = "unknown"
            self._onb_registered = False

    # ---- helpers -----------------------------------------------------------------
    @staticmethod
    def _features_enabled() -> bool:
        return feature_flags.is_enabled("recruitment_welcome") and feature_flags.is_enabled(
            "welcome_dialog"
        )

    @staticmethod
    def _thread_owner_id(thread: discord.Thread | None) -> int | None:
        if thread is None:
            return None
        owner = getattr(thread, "owner", None)
        if isinstance(owner, discord.Member):
            return _actor_id(owner)
        raw = getattr(thread, "owner_id", None)
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    def _eligible_member(self, member: discord.Member | None, thread: discord.Thread | None) -> bool:
        if member is None or thread is None:
            return False
        if getattr(member, "bot", False):
            return False
        try:
            perms = thread.permissions_for(member)
        except Exception:
            perms = None
        if perms is not None:
            can_post = getattr(perms, "send_messages_in_threads", None)
            if can_post is None:
                can_post = getattr(perms, "send_messages", False)
            if can_post:
                return True
        owner_id = self._thread_owner_id(thread)
        actor_id = _actor_id(member)
        if owner_id is not None and actor_id is not None and owner_id == actor_id:
            return True
        member_roles = _collect_role_ids(member)
        return bool(member_roles.intersection(self._staff_role_ids))

    def _log_context(
        self,
        thread: discord.Thread | None,
        actor: discord.abc.User | None,
        *,
        source: str,
        result: str,
        **extra: object,
    ) -> dict[str, object]:
        context = logs.thread_context(thread if isinstance(thread, discord.Thread) else None)
        context.update(
            {
                "view": "panel",
                "view_tag": panels.WELCOME_PANEL_TAG,
                "custom_id": panels.OPEN_QUESTIONS_CUSTOM_ID,
                "view_id": panels.OPEN_QUESTIONS_CUSTOM_ID,
                "actor": logs.format_actor(actor if isinstance(actor, discord.abc.User) else None),
                "actor_name": logs.format_actor_handle(
                    actor if isinstance(actor, discord.abc.User) else None
                ),
                "app_permissions": "-",
                "app_perms_text": "-",
                "result": result,
                "source": source,
            }
        )
        if actor is not None:
            actor_identifier = _actor_id(actor)
            if actor_identifier is not None:
                context["actor_id"] = actor_identifier
        if extra:
            context.update(extra)
        return context

    def _is_ticket_tool_author(self, user: discord.abc.User | None) -> bool:
        if user is None or self.ticket_tool_id is None:
            return False
        return getattr(user, "id", None) == self.ticket_tool_id

    def _log_panel_outcome(
        self,
        actor: discord.abc.User | None,
        thread: discord.Thread,
        outcome: PanelOutcome,
        *,
        flow: str | None = None,
        trigger: str | None = None,
    ) -> None:
        if outcome.result == "skipped" and outcome.reason not in {
            "panel_send_failed",
            "thread_join_failed",
            "panel_exists",
            "ticket_not_parsed",
        }:
            return

        actor_handle = logs.format_actor_handle(actor) or "<unknown>"
        thread_ref = outcome.thread_name or getattr(thread, "id", None) or "<unknown>"
        emoji = "📘" if outcome.result == "panel_created" else "⚠️"

        payload: dict[str, object] = {
            "actor": actor_handle,
            "thread": thread_ref,
            "result": outcome.result,
            "ms": outcome.elapsed_ms,
        }
        if outcome.reason:
            payload["reason"] = outcome.reason
        if flow:
            payload["flow"] = flow
        if trigger:
            payload["trigger"] = trigger

        log_lifecycle(
            log,
            "welcome",
            "triggered",
            scope_label="Welcome panel",
            emoji=emoji,
            dedupe=False,
            **payload,
        )

    async def _resolve_thread(self, payload: RawReactionActionEvent) -> discord.Thread | None:
        if payload.guild_id is None:
            return None
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return None
        thread = guild.get_thread(payload.channel_id)
        if thread is None:
            channel = self.bot.get_channel(payload.channel_id)
            thread = channel if isinstance(channel, discord.Thread) else None
        if thread is None:
            try:
                channel = await self.bot.fetch_channel(payload.channel_id)
            except Exception:  # pragma: no cover - network fallback
                channel = None
            if isinstance(channel, discord.Thread):
                thread = channel
        return thread

    async def _post_panel(
        self,
        thread: discord.Thread,
        *,
        actor: discord.abc.User | None,
        source: str,
        flow: str = "welcome",
        ticket_code: str | None = None,
        trigger_message: discord.Message | None = None,
    ) -> None:
        context_user_id: int | None = None
        try:
            context = await self._ensure_context(thread)
        except Exception:
            context = None
            log.exception(
                "failed to resolve ticket context for panel post",
                extra={
                    "thread_id": getattr(thread, "id", None),
                    "thread_name": getattr(thread, "name", None),
                },
            )
        if context is not None:
            context_user_id = getattr(context, "recruit_id", None)

        outcome = await post_open_questions_panel(
            self.bot,
            thread,
            actor=actor,
            flow=flow,
            ticket_code=ticket_code,
            trigger_message=trigger_message,
            subject_user_id=context_user_id,
        )
        self._log_panel_outcome(actor, thread, outcome, flow=flow)

    # ---- listeners ----------------------------------------------------------------
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        thread = message.channel if isinstance(message.channel, discord.Thread) else None
        if thread is None:
            return

        target_channel_id = self.channel_id
        parent_ref = getattr(thread, "parent_id", None)
        if target_channel_id is None or parent_ref != target_channel_id:
            return
        if not self._features_enabled():
            return
        if not thread_scopes.is_welcome_parent(thread):
            return
        if not isinstance(message.author, (discord.Member, discord.User)):
            return
        if getattr(message.author, "bot", False):
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
                log.warning("rolling card handler raised", exc_info=True)
            else:
                if handled:
                    return

        content = (message.content or "").lower()
        if _TRIGGER_PHRASE not in content:
            return

        try:
            await message.add_reaction("👍")
        except Exception:  # pragma: no cover - best effort
            log.debug("failed to add welcome auto-reaction", exc_info=True)

        await self._post_panel(
            thread,
            actor=message.author,
            source="phrase",
            trigger_message=message,
        )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: RawReactionActionEvent) -> None:
        if str(payload.emoji) != _TICKET_EMOJI:
            return
        thread = await self._resolve_thread(payload)
        if thread is None:
            return
        if not self._features_enabled():
            return
        if payload.guild_id is None:
            return
        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        target_channel_id = self.channel_id
        if (
            target_channel_id is None
            or thread.parent_id != target_channel_id
            or not thread_scopes.is_welcome_parent(thread)
        ):
            return

        bot_user = getattr(self.bot, "user", None)
        if bot_user and payload.user_id == getattr(bot_user, "id", None):
            return

        member: discord.Member | None = payload.member
        if member is None and guild is not None:
            member = guild.get_member(payload.user_id)
        actor: discord.abc.User | None = member or payload.member

        if not self._eligible_member(member, thread):
            context = self._log_context(
                thread,
                actor,
                source="emoji",
                result="not_eligible",
                reason="missing_role_or_owner",
                emoji=_TICKET_EMOJI,
            )
            await logs.send_welcome_log("warn", **context)
            return

        await self._post_panel(thread, actor=actor, source="emoji")


class _ClanSelect(discord.ui.Select):
    def __init__(self, parent_view: "ClanSelectView") -> None:
        self._parent_view = parent_view
        super().__init__(placeholder="Select a clan tag", min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:  # pragma: no cover - UI callback
        if not self.values:
            await interaction.response.defer()
            return
        await self._parent_view.handle_selection(interaction, self.values[0])


class ClanSelectView(discord.ui.View):
    def __init__(
        self,
        watcher: "WelcomeTicketWatcher",
        context: TicketContext,
        tags: List[str],
        *,
        page_size: int = 20,
    ) -> None:
        super().__init__(timeout=300)
        self.watcher = watcher
        self.context = context
        self.tags = [tag.strip().upper() for tag in tags if tag.strip()]
        self.page_size = max(1, page_size)
        self.page = 0
        self.message: Optional[discord.Message] = None

        self.select = _ClanSelect(self)
        self.add_item(self.select)

        self.prev_button = None
        self.next_button = None
        if len(self.tags) > self.page_size:
            self.prev_button = discord.ui.Button(label="◀", style=discord.ButtonStyle.secondary)
            self.prev_button.callback = self._on_prev  # type: ignore[assignment]
            self.next_button = discord.ui.Button(label="▶", style=discord.ButtonStyle.secondary)
            self.next_button.callback = self._on_next  # type: ignore[assignment]
            self.add_item(self.prev_button)
            self.add_item(self.next_button)

        self._refresh_options()

    def _page_slice(self) -> List[str]:
        start = self.page * self.page_size
        end = start + self.page_size
        return self.tags[start:end]

    def _refresh_options(self) -> None:
        page_tags = self._page_slice()
        if not page_tags:
            self.select.options = [
                discord.SelectOption(label="No clan tags available", value="none", default=True)
            ]
            self.select.disabled = True
        else:
            self.select.options = [discord.SelectOption(label=tag, value=tag) for tag in page_tags]
            self.select.disabled = False
        if self.prev_button is not None and self.next_button is not None:
            self.prev_button.disabled = self.page <= 0
            remaining = (self.page + 1) * self.page_size
            self.next_button.disabled = remaining >= len(self.tags)

    async def _on_prev(self, interaction: discord.Interaction) -> None:  # pragma: no cover - UI callback
        if self.page <= 0:
            await interaction.response.defer()
            return
        self.page -= 1
        self._refresh_options()
        await interaction.response.edit_message(view=self)

    async def _on_next(self, interaction: discord.Interaction) -> None:  # pragma: no cover - UI callback
        if (self.page + 1) * self.page_size >= len(self.tags):
            await interaction.response.defer()
            return
        self.page += 1
        self._refresh_options()
        await interaction.response.edit_message(view=self)

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
            log.debug("failed to disable clan select view on timeout", exc_info=True)


class WelcomeTicketWatcher(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        channel_id = get_welcome_channel_id()
        try:
            self.channel_id = int(channel_id) if channel_id is not None else None
        except (TypeError, ValueError):
            self.channel_id = None
        self.ticket_tool_id = get_ticket_tool_bot_id()
        self._tickets: Dict[int, TicketContext] = {}
        self._clan_tags: List[str] = []
        self._clan_tag_set: Set[str] = set()
        self._auto_closed_threads: set[int] = set()

        if self.channel_id is None:
            log.warning("welcome ticket watcher disabled — invalid WELCOME_CHANNEL_ID")

    @staticmethod
    def _features_enabled() -> bool:
        return feature_flags.is_enabled("recruitment_welcome")

    def _is_ticket_thread(self, thread: discord.Thread | None) -> bool:
        if thread is None:
            return False
        if self.channel_id is None:
            return False
        return getattr(thread, "parent_id", None) == self.channel_id

    def _parse_thread(self, name: str | None) -> Optional[ThreadNameParts]:
        return parse_welcome_thread_name(name)

    def _owner_matches(self, thread: discord.Thread) -> bool:
        if self.ticket_tool_id is None:
            return True
        owner_id = getattr(thread, "owner_id", None)
        try:
            owner_value = int(owner_id) if owner_id is not None else None
        except (TypeError, ValueError):
            owner_value = None
        return owner_value == self.ticket_tool_id

    def _is_ticket_tool(self, user: discord.abc.User | None) -> bool:
        if user is None:
            return False
        if self.ticket_tool_id is not None:
            return getattr(user, "id", None) == self.ticket_tool_id
        name = getattr(user, "name", "") or ""
        return "ticket" in name.lower() and "tool" in name.lower()

    async def _ensure_context(self, thread: discord.Thread) -> Optional[TicketContext]:
        context = self._tickets.get(thread.id)
        if context is not None:
            return context
        parsed = self._parse_thread(thread.name)
        if not parsed:
            return None
        context = TicketContext(
            thread_id=thread.id,
            ticket_number=parsed.ticket_code,
            username=parsed.username,
            recruit_display=parsed.username,
        )
        self._tickets[thread.id] = context
        return context

    async def _handle_ticket_open(self, thread: discord.Thread, context: TicketContext) -> None:
        existing_row: List[str] | None = None
        try:
            lookup = await asyncio.to_thread(
                onboarding_sheets.find_welcome_row, context.ticket_number
            )
        except Exception:
            log.exception(
                "failed to read existing welcome row",
                extra={"thread_id": getattr(thread, "id", None), "ticket": context.ticket_number},
            )
            lookup = None

        if lookup:
            _, existing_row = lookup

        clan_value = ""
        closed_value = ""
        if existing_row:
            clan_idx = onboarding_sheets.WELCOME_CLAN_TAG_INDEX
            closed_idx = onboarding_sheets.WELCOME_DATE_CLOSED_INDEX
            if clan_idx < len(existing_row):
                clan_value = existing_row[clan_idx] or ""
            if closed_idx < len(existing_row):
                closed_value = existing_row[closed_idx] or ""

        created_at = _normalize_dt(getattr(thread, "created_at", None))

        starter: discord.Message | None = None
        try:
            starter = await locate_welcome_message(thread)
            # Only use the first mentioned user in the welcome message.
            # If there is no mention, we don't guess or fall back.
            applicant_id: int | None = None
            if starter is not None and starter.mentions:
                try:
                    applicant_id = int(getattr(starter.mentions[0], "id", None))
                except (TypeError, ValueError):
                    applicant_id = None
        except Exception:
            applicant_id = None
            log.debug(
                "failed to resolve applicant on ticket open",
                exc_info=True,
                extra={"thread_id": getattr(thread, "id", None)},
            )

        # Prefer the explicitly mentioned user and avoid guessing a fallback when none exists.
        subject_resolved = applicant_id

        if applicant_id is not None:
            try:
                context.recruit_id = int(applicant_id)
            except (TypeError, ValueError):
                context.recruit_id = None
        if context.recruit_id is None and subject_resolved is not None:
            try:
                context.recruit_id = int(subject_resolved)
            except (TypeError, ValueError):
                context.recruit_id = None

        ticket_user = context.recruit_id if context.recruit_id is not None else subject_resolved
        ticket_number = None
        ticket_username = None
        try:
            ticket_number, ticket_username = (getattr(thread, "name", "") or "").split("-", 1)
            ticket_number = ticket_number.strip()
            ticket_username = ticket_username.strip()
        except ValueError:
            ticket_number = None
            ticket_username = None

        try:
            result = await log_sheet_write(
                flow="welcome",
                phase="created",
                tab="Welcome",
                logger=log,
                thread=thread,
                user=context.username,
                write_coro=lambda: asyncio.to_thread(
                    onboarding_sheets.append_welcome_ticket_row,
                    context.ticket_number,
                    context.username,
                    clan_value,
                    closed_value,
                    thread_name=getattr(thread, "name", ""),
                    user_id=ticket_user,
                    thread_id=int(getattr(thread, "id", 0)),
                    panel_message_id=None,
                    status="open",
                    created_at=created_at,
                ),
            )
            log.info(
                "✅ welcome_ticket_open — ticket=%s • user=%s • result=row_%s",
                context.ticket_number,
                context.username,
                result,
            )
        except Exception as exc:
            log.error(
                "❌ welcome_ticket_open — ticket=%s • user=%s • result=error • reason=%s",
                context.ticket_number,
                context.username,
                exc,
            )

    async def _touch_welcome_sheet_for_reminder(
        self,
        *,
        phase: str,
        thread: discord.Thread,
        context: TicketContext,
        created_at: datetime,
        user_ref: str,
    ) -> None:
        headers = _WELCOME_HEADERS
        created_value = created_at.isoformat()
        updated_value = datetime.now(timezone.utc).isoformat()

        try:
            existing = await asyncio.to_thread(
                onboarding_sheets.find_welcome_row, context.ticket_number
            )
        except Exception:
            log.debug(
                "welcome reminder: failed to load welcome row for sheet logging",
                exc_info=True,
                extra={"ticket": context.ticket_number, "thread_id": getattr(thread, "id", None)},
            )
            existing = None

        if existing:
            row_values = list(existing[1])
        else:
            row_values = [
                context.ticket_number,
                context.username,
                context.final_clan or "",
                "",
                getattr(thread, "name", ""),
                str(context.recruit_id or ""),
                str(getattr(thread, "id", 0)),
                "",
                "open",
                created_value,
                "",
            ]

        if len(row_values) < len(headers):
            row_values.extend(["" for _ in range(len(headers) - len(row_values))])

        try:
            created_idx = headers.index("created_at")
            updated_idx = headers.index("updated_at")
        except ValueError:
            return

        if not row_values[created_idx]:
            row_values[created_idx] = created_value
        row_values[updated_idx] = updated_value

        try:
            await log_sheet_write(
                flow="welcome",
                phase=phase,
                tab="Welcome",
                logger=log,
                thread=thread,
                user=user_ref,
                write_coro=lambda: asyncio.to_thread(
                    onboarding_sheets.upsert_welcome, row_values, headers
                ),
            )
        except Exception:
            log.debug(
                "welcome reminder: sheet touch failed",
                exc_info=True,
                extra={"ticket": context.ticket_number, "thread_id": getattr(thread, "id", None)},
            )

    async def _load_clan_tags(self) -> List[str]:
        if self._clan_tags:
            return self._clan_tags

        raw_tags: List[str] = []
        bucket = sheets_cache.get_bucket("clan_tags")
        if bucket is not None:
            value = bucket.value
            if not value:
                try:
                    await sheets_cache.refresh_now("clan_tags", actor="welcome_watcher")
                    value = bucket.value
                except Exception:
                    log.debug("failed to refresh clan_tags cache", exc_info=True)
            if isinstance(value, list):
                raw_tags = [str(tag) for tag in value]

        if not raw_tags:
            try:
                raw_tags = await asyncio.to_thread(onboarding_sheets.load_clan_tags)
            except Exception:
                log.exception("failed to load clan tags from Sheets")
                return []

        normalized: List[str] = []
        seen: Set[str] = set()
        for tag in raw_tags:
            cleaned = str(tag or "").strip().upper()
            if not cleaned:
                continue
            if cleaned in seen:
                continue
            normalized.append(cleaned)
            seen.add(cleaned)

        self._clan_tags = normalized
        self._clan_tag_set = seen
        return self._clan_tags

    def _tag_known(self, tag: str) -> bool:
        normalized = (tag or "").strip().upper()
        if not normalized:
            return False
        if normalized in self._clan_tag_set:
            return True
        if normalized == _NO_PLACEMENT_TAG and normalized in self._clan_tags:
            return True
        return False

    async def _send_invalid_tag_notice(
        self,
        thread: discord.Thread,
        actor: Optional[discord.abc.User],
        attempted_tag: str,
    ) -> None:
        notice = (
            f"⚠️ I couldn't find the clan tag `{attempted_tag}`. "
            "Please choose a tag from the picker or enter a valid clan tag (e.g. C1CE)."
        )
        if actor is not None:
            try:
                await actor.send(notice)
                return
            except Exception:
                log.debug("failed to send invalid clan tag DM", exc_info=True)
        try:
            await thread.send(notice, delete_after=30)
        except Exception:
            log.debug("failed to send invalid clan tag notice", exc_info=True)

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        if not self._features_enabled():
            return
        if not self._is_ticket_thread(thread):
            return
        if not self._owner_matches(thread):
            return
        context = await self._ensure_context(thread)
        if context is None:
            return
        await self._handle_ticket_open(thread, context)

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread) -> None:
        if not self._features_enabled():
            return
        if not self._is_ticket_thread(after):
            return

        context = await self._ensure_context(after)
        if context is None:
            return

        parsed = self._parse_thread(after.name)
        if parsed:
            context.ticket_number = parsed.ticket_code
            context.username = parsed.username

        if context.state in {"awaiting_clan", "closed"}:
            return

        if getattr(after, "id", None) in self._auto_closed_threads:
            context.state = "closed"
            return

        session_row = onboarding_sessions.get_by_thread_id(getattr(after, "id", None))
        if session_row and session_row.get("auto_closed_at"):
            context.state = "closed"
            return

        reason = ""
        parsed_state = parsed.state if parsed else "open"
        if parsed_state == "closed":
            reason = "manual_close_without_ticket_tool"
        else:
            archived_now = bool(getattr(after, "archived", False))
            archived_before = bool(getattr(before, "archived", False))
            locked_now = bool(getattr(after, "locked", False))
            locked_before = bool(getattr(before, "locked", False))
            if (archived_now and not archived_before) or (locked_now and not locked_before):
                if not context.ticket_tool_close_detected:
                    reason = "manual_close_without_ticket_tool"

        if not reason:
            return

        await self._handle_manual_close(after, context, reason=reason)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if not self._features_enabled():
            return
        thread = message.channel if isinstance(message.channel, discord.Thread) else None
        if not self._is_ticket_thread(thread):
            return
        if thread is None:
            return
        context = await self._ensure_context(thread)
        if context is None:
            return

        if self._is_ticket_tool(message.author):
            content = (message.content or "").lower()
            if _CLOSED_MESSAGE_TOKEN in content and context.state not in {"awaiting_clan", "closed"}:
                context.ticket_tool_close_detected = True
                await self._handle_ticket_closed(thread, context, manual=False)
            return

        if getattr(message.author, "bot", False):
            return

        if context.state != "awaiting_clan" or context.final_clan:
            return

        candidate = (message.content or "").strip().upper()
        if not candidate:
            return
        await self._load_clan_tags()
        if not self._tag_known(candidate):
            await self._send_invalid_tag_notice(thread, message.author, candidate)
            return
        await self._finalize_clan_tag(
            thread,
            context,
            candidate,
            actor=message.author,
            source="message",
            prompt_message=None,
            view=None,
        )

    async def _handle_manual_close(
        self, thread: discord.Thread, context: TicketContext, *, reason: str
    ) -> None:
        if context.state in {"awaiting_clan", "closed"}:
            return

        parsed = self._parse_thread(thread.name)
        if parsed:
            context.ticket_number = parsed.ticket_code
            context.username = parsed.username

        row_info: tuple[int, List[str]] | None = None
        try:
            row_info = await asyncio.to_thread(
                onboarding_sheets.find_welcome_row, context.ticket_number
            )
        except Exception:
            log.exception(
                "failed to locate onboarding row for manual close",
                extra={"ticket": context.ticket_number, "thread_id": getattr(thread, "id", None)},
            )

        row_values: List[str] | None = row_info[1] if row_info else None
        if not row_values:
            row_values = [context.ticket_number, context.username, "", ""]
            try:
                await asyncio.to_thread(onboarding_sheets.upsert_welcome, row_values, _WELCOME_HEADERS)
            except Exception:
                log.exception(
                    "failed to insert onboarding row during manual close",
                    extra={"ticket": context.ticket_number, "thread_id": getattr(thread, "id", None)},
                )
                row_values = None
            else:
                context.row_created_during_close = True
                log.warning(
                    "⚠️ welcome_close_manual — ticket=%s • user=%s • reason=onboarding_row_missing_manual_close • action=row_inserted_no_reconcile",
                    context.ticket_number,
                    context.username,
                )

        clan_value = ""
        if row_values:
            clan_idx = onboarding_sheets.WELCOME_CLAN_TAG_INDEX
            if clan_idx < len(row_values):
                clan_value = (row_values[clan_idx] or "").strip()

        if clan_value:
            return

        await self._handle_ticket_closed(thread, context, manual=True)
        log.warning(
            "⚠️ welcome_close_manual — ticket=%s • user=%s • reason=%s • action=prompt_posted • source=manual_fallback",
            context.ticket_number,
            context.username,
            reason,
        )

    async def auto_close_ticket(
        self,
        thread: discord.Thread,
        context: TicketContext,
        *,
        final_tag: str = _NO_PLACEMENT_TAG,
        rename_thread: bool = True,
    ) -> None:
        thread_id = getattr(thread, "id", 0)
        if thread_id:
            self._auto_closed_threads.add(int(thread_id))
        context.close_source = "auto_close"
        context.state = "awaiting_clan"
        await self._finalize_clan_tag(
            thread,
            context,
            final_tag,
            actor=None,
            source="auto_close",
            prompt_message=None,
            view=None,
            notify=False,
            rename_thread=rename_thread,
            sheet_phase="auto_close",
        )

    async def auto_close_for_inactivity(
        self,
        thread: discord.Thread,
        context: TicketContext,
        *,
        notice: str,
        closed_name: str | None,
        final_tag: str = _NO_PLACEMENT_TAG,
    ) -> None:
        thread_id = getattr(thread, "id", 0)
        if thread_id:
            self._auto_closed_threads.add(int(thread_id))

        if closed_name:
            try:
                await thread.edit(name=closed_name)
            except Exception:
                log.warning(
                    "welcome auto-close rename failed",
                    exc_info=True,
                    extra={"thread_id": getattr(thread, "id", None), "ticket": context.ticket_number},
                )

        try:
            await thread.send(notice)
        except Exception:
            log.warning(
                "welcome auto-close notice failed",
                exc_info=True,
                extra={"thread_id": getattr(thread, "id", None)},
            )

        try:
            await thread.edit(archived=True, locked=True)
        except Exception:
            log.warning(
                "welcome auto-close archive failed",
                exc_info=True,
                extra={"thread_id": getattr(thread, "id", None)},
            )

        await self.auto_close_ticket(
            thread, context, final_tag=final_tag, rename_thread=False
        )

    async def _handle_ticket_closed(
        self, thread: discord.Thread, context: TicketContext, *, manual: bool = False
    ) -> None:
        tags = await self._load_clan_tags()
        if not tags:
            await thread.send(
                "⚠️ I couldn't load the clan tag list right now. Please try again in a moment."
            )
            log.warning(
                "⚠️ welcome_close — ticket=%s • user=%s • reason=clan_tags_unavailable • result=error",
                context.ticket_number,
                context.username,
            )
            return

        context.close_source = "manual_fallback" if manual else "ticket_tool"
        context.state = "awaiting_clan"
        content = (
            f"Which clan tag for {context.username} (ticket {context.ticket_number})?\n"
            f"{CLAN_TAG_PROMPT_HELPER}"
        )
        view = ClanSelectView(self, context, tags)
        try:
            message = await thread.send(content, view=view)
        except Exception:
            context.state = "open"
            log.exception(
                "failed to post clan selection prompt",
                extra={"thread_id": getattr(thread, "id", None), "ticket": context.ticket_number},
            )
            return
        view.message = message
        context.prompt_message_id = message.id

    async def finalize_from_interaction(
        self,
        context: TicketContext,
        tag: str,
        interaction: discord.Interaction,
        view: ClanSelectView,
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
            source="select",
            prompt_message=interaction.message,
            view=view,
        )

    async def _finalize_clan_tag(
        self,
        thread: discord.Thread,
        context: TicketContext,
        final_tag: str,
        *,
        actor: discord.abc.User | None,
        source: str,
        prompt_message: Optional[discord.Message],
        view: Optional[ClanSelectView],
        notify: bool = True,
        rename_thread: bool = True,
        sheet_phase: str | None = None,
    ) -> None:
        if context.state == "closed":
            return

        final_tag = (final_tag or "").strip().upper() or _NO_PLACEMENT_TAG
        if final_tag != _NO_PLACEMENT_TAG:
            await self._load_clan_tags()
            if not self._tag_known(final_tag):
                await self._send_invalid_tag_notice(thread, actor, final_tag)
                return

        previous_final = ""
        try:
            existing_row = await asyncio.to_thread(
                onboarding_sheets.find_welcome_row, context.ticket_number
            )
        except Exception:
            existing_row = None
            log.exception(
                "failed to fetch onboarding row before finalize",
                extra={
                    "ticket": context.ticket_number,
                    "user": context.username,
                    "source": source,
                },
            )
        else:
            if existing_row:
                row_values = existing_row[1]
                clan_idx = onboarding_sheets.WELCOME_CLAN_TAG_INDEX
                if clan_idx < len(row_values):
                    previous_final = (row_values[clan_idx] or "").strip()

        previous_final_normalized = previous_final.strip().upper()
        consume_open_spot = final_tag != _NO_PLACEMENT_TAG and (
            not previous_final_normalized
            or previous_final_normalized == _NO_PLACEMENT_TAG
            or previous_final_normalized != final_tag
        )

        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        row = [context.ticket_number, context.username, final_tag, timestamp]

        try:
            result = await log_sheet_write(
                flow="welcome",
                phase=sheet_phase or source,
                tab="Welcome",
                logger=log,
                thread=thread,
                user=context.username,
                write_coro=lambda: asyncio.to_thread(
                    onboarding_sheets.upsert_welcome, row, _WELCOME_HEADERS
                ),
            )
        except Exception:
            log.exception(
                "❌ welcome_close — ticket=%s • user=%s • final=%s • result=error • reason=sheet_write",
                context.ticket_number,
                context.username,
                final_tag,
            )
            await thread.send(
                "⚠️ Something went wrong while updating the onboarding log. Please try again later or contact an admin."
            )
            return

        row_missing = result not in {"updated", "inserted"}
        reservation_label = "none"
        reservation_row: reservations_sheets.ReservationRow | None = None
        actions_ok = True
        recompute_tags: List[str] = []
        row_change_lines: List[str] = []
        row_targets: "OrderedDict[str, str]" = OrderedDict()
        before_snapshots: Dict[str, _ClanMathRowSnapshot] = {}
        column_map: Dict[str, int] | None = None

        if not row_missing:
            final_entry = (
                recruitment_sheets.find_clan_row(final_tag)
                if final_tag != _NO_PLACEMENT_TAG
                else None
            )
            final_is_real = final_entry is not None

            try:
                matches = await reservations_sheets.find_active_reservations_for_recruit(
                    context.recruit_id,
                    context.recruit_display or context.username,
                )
            except Exception:
                matches = []
                log.exception(
                    "failed to look up reservations for recruit",
                    extra={"ticket": context.ticket_number, "user": context.username},
                )
            if matches:
                reservation_row = matches[0]
                if len(matches) > 1:
                    log.warning(
                        "multiple active reservations matched",
                        extra={
                            "ticket": context.ticket_number,
                            "user": context.username,
                            "rows": [row.row_number for row in matches],
                        },
                    )

            decision = _determine_reservation_decision(
                final_tag,
                reservation_row,
                no_placement_tag=_NO_PLACEMENT_TAG,
                final_is_real=final_is_real,
                consume_open_spot=consume_open_spot,
                previous_final=previous_final,
            )
            reservation_label = decision.label

            target_tags = _clan_tags_for_logging(
                final_tag,
                decision,
                no_placement_tag=_NO_PLACEMENT_TAG,
                final_is_real=final_is_real,
            )
            if target_tags:
                row_targets = _normalize_clan_math_targets(target_tags)
                try:
                    column_map = _clan_math_column_indices()
                    before_snapshots = _capture_clan_snapshots(row_targets, column_map)
                except Exception:
                    column_map = None
                    row_targets = OrderedDict()
                    log.exception(
                        "failed to capture clan math before-state",
                        extra={"ticket": context.ticket_number},
                    )

            if reservation_row is not None and decision.status:
                try:
                    await reservations_sheets.update_reservation_status(
                        reservation_row.row_number, decision.status
                    )
                except Exception:
                    actions_ok = False
                    log.exception(
                        "failed to update reservation status",
                        extra={
                            "row": reservation_row.row_number,
                            "ticket": context.ticket_number,
                            "status": decision.status,
                        },
                    )

            for tag, delta in decision.open_deltas.items():
                try:
                    await availability.adjust_manual_open_spots(tag, delta)
                except Exception:
                    actions_ok = False
                    log.exception(
                        "failed to adjust manual open spots",
                        extra={"clan_tag": tag, "delta": delta, "ticket": context.ticket_number},
                    )

            recompute_tags = decision.recompute_tags
            for tag in recompute_tags:
                try:
                    await availability.recompute_clan_availability(tag, guild=thread.guild)
                except Exception:
                    actions_ok = False
                    log.exception(
                        "failed to recompute clan availability",
                        extra={"clan_tag": tag, "ticket": context.ticket_number},
                    )

            if row_targets and column_map is not None:
                after_snapshots = _capture_clan_snapshots(row_targets, column_map)
                row_change_lines = _build_clan_math_row_lines(
                    row_targets, before_snapshots, after_snapshots
                )

        final_display = final_tag if final_tag else _NO_PLACEMENT_TAG
        confirmation = (
            f"Got it — set clan tag to **{final_display}** and logged to the sheet. ✅"
        )
        if prompt_message is None and context.prompt_message_id:
            try:
                prompt_message = await thread.fetch_message(context.prompt_message_id)
            except Exception:
                prompt_message = None

        if notify:
            if prompt_message is not None:
                try:
                    await prompt_message.edit(content=confirmation, view=None)
                except Exception:
                    await thread.send(confirmation)
            else:
                await thread.send(confirmation)

        if view is not None:
            view.stop()

        if rename_thread:
            try:
                new_name = build_closed_thread_name(
                    context.ticket_number, context.username, final_display
                )
                await thread.edit(name=new_name)
            except Exception:
                actions_ok = False
                log.exception(
                    "failed to rename welcome thread",
                    extra={"thread_id": getattr(thread, "id", None), "ticket": context.ticket_number},
                )

        context.final_clan = final_display
        context.reservation_label = reservation_label
        context.state = "closed"

        if row_missing:
            log.warning(
                "⚠️ welcome_close — ticket=%s • user=%s • reason=onboarding_row_missing • action=row_inserted_no_reconcile",
                context.ticket_number,
                context.username,
            )
            _log_finalize_summary(
                context,
                thread,
                final_display=final_display,
                reservation_label=reservation_label,
                result="fail",
                reason="onboarding_row_missing",
            )
            try:
                await _log_clan_math_event(
                    context,
                    final_display=final_display,
                    reservation_label=reservation_label,
                    reservation_row=reservation_row,
                    result="fail",
                    reason="onboarding_row_missing",
                    row_change_lines=row_change_lines,
                )
            except Exception:
                log.exception(
                    "failed to emit clan math log after row insert",
                    extra={"ticket": context.ticket_number},
                )
            return

        log_result = "ok" if actions_ok else "error"
        is_manual = context.close_source == "manual_fallback"
        event_name = "welcome_close_manual" if is_manual else "welcome_close"
        emoji = "⚠️" if is_manual and log_result == "ok" else ("✅" if log_result == "ok" else "❌")
        extra_bits = ""
        if log_result != "ok":
            extra_bits = " • reason=partial_actions"
        if is_manual:
            extra_bits = f"{extra_bits} • source=manual_fallback"
        log.info(
            "%s %s — ticket=%s • user=%s • final=%s • reservation=%s • result=%s%s",
            emoji,
            event_name,
            context.ticket_number,
            context.username,
            final_display,
            reservation_label,
            log_result,
            extra_bits,
        )
        summary_reason = "partial_actions" if log_result != "ok" else None
        _log_finalize_summary(
            context,
            thread,
            final_display=final_display,
            reservation_label=reservation_label,
            result=log_result,
            reason=summary_reason,
        )
        try:
            await _log_clan_math_event(
                context,
                final_display=final_display,
                reservation_label=reservation_label,
                reservation_row=reservation_row,
                result=log_result,
                reason=summary_reason,
                row_change_lines=row_change_lines,
            )
        except Exception:
            log.exception(
                "failed to emit clan math log",
                extra={"ticket": context.ticket_number, "result": log_result},
            )


async def setup(bot: commands.Bot) -> None:
    existing_welcome_watcher = bot.get_cog("WelcomeWatcher")
    if existing_welcome_watcher is None:
        await bot.add_cog(WelcomeWatcher(bot))
    elif not isinstance(existing_welcome_watcher, WelcomeWatcher):
        raise RuntimeError("cog name collision for WelcomeWatcher")

    existing_ticket_watcher = bot.get_cog("WelcomeTicketWatcher")
    if existing_ticket_watcher is None:
        await bot.add_cog(WelcomeTicketWatcher(bot))
    elif not isinstance(existing_ticket_watcher, WelcomeTicketWatcher):
        raise RuntimeError("cog name collision for WelcomeTicketWatcher")

    _ensure_reminder_job(bot)
    from modules.onboarding.idle_watcher import ensure_idle_watcher

    await ensure_idle_watcher(bot)
