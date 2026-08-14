from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

import discord
from discord.ext import commands

from c1c_coreops.rbac import is_admin_member, is_staff_member
from modules.common import feature_flags
from shared.sheets import async_core, recruitment

log = logging.getLogger("c1c.housekeeping.staff_thread_guard")

FEATURE_TOGGLE = "HOUSEKEEPING_STAFF_THREAD_GUARD_ENABLED"
CONFIG_RULES_TAB = "HOUSEKEEPING_STAFF_THREAD_GUARD_TAB"
CONFIG_OFFENSES_TAB = "HOUSEKEEPING_STAFF_THREAD_GUARD_OFFENSES_TAB"
RULE_CACHE_TTL_SECONDS = 60.0

RULE_HEADERS = (
    "enabled",
    "guard_id",
    "thread_id",
    "thread_name",
    "parent_name",
    "action",
    "redirect_target_id",
    "redirect_target_name",
    "warning_delete_after_seconds",
    "offense_window_minutes",
    "timeout_after_offenses",
    "timeout_seconds",
    "warning_text",
    "timeout_text",
    "redirect_notice_text",
    "redirect_header_text",
    "failure_text",
    "notes",
)
OFFENSE_HEADERS = (
    "guard_id",
    "thread_id",
    "user_id",
    "offense_count",
    "window_started_at_utc",
    "last_offense_at_utc",
    "last_timeout_at_utc",
    "last_action",
    "last_message_id",
)


@dataclass(frozen=True, slots=True)
class GuardRule:
    guard_id: str
    thread_id: int
    action: str
    redirect_target_id: int | None
    warning_delete_after_seconds: int
    offense_window_minutes: int
    timeout_after_offenses: int
    timeout_seconds: int
    warning_text: str
    timeout_text: str
    redirect_notice_text: str
    redirect_header_text: str
    failure_text: str


@dataclass(slots=True)
class OffenseState:
    guard_id: str
    thread_id: int
    user_id: int
    offense_count: int = 0
    window_started_at_utc: str = ""
    last_offense_at_utc: str = ""
    last_timeout_at_utc: str = ""
    last_action: str = ""
    last_message_id: str = ""
    sheet_row: int | None = None


_RULE_CACHE: dict[int, GuardRule] = {}
_RULE_CACHE_LOADED_AT = 0.0
_RULE_CACHE_LOCK = asyncio.Lock()
_OFFENSE_LOCKS: dict[tuple[str, int], asyncio.Lock] = {}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: str | None) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _parse_bool(value: Any) -> bool:
    return str(value or "").strip().lower() in {"true", "1", "yes", "y", "on"}


def _parse_int(value: Any, *, default: int = 0, minimum: int = 0) -> int:
    try:
        parsed = int(str(value or "").strip())
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= minimum else default


def _row_value(row: Mapping[str, Any], header: str) -> str:
    for key, value in row.items():
        if str(key or "").strip().lower() == header:
            return str(value or "").strip()
    return ""


def _feature_enabled() -> bool:
    status = feature_flags.status(FEATURE_TOGGLE)
    return bool(status.get("present") and status.get("enabled") and not status.get("invalid"))


def _validate_rule(row: Mapping[str, Any], *, row_number: int) -> GuardRule | None:
    if not _parse_bool(_row_value(row, "enabled")):
        return None

    guard_id = _row_value(row, "guard_id")
    thread_id = _parse_int(_row_value(row, "thread_id"))
    action = _row_value(row, "action").lower()
    redirect_target_id = _parse_int(_row_value(row, "redirect_target_id")) or None

    if not guard_id or not thread_id:
        log.warning("staff thread guard row skipped: row=%s missing guard_id/thread_id", row_number)
        return None
    if action not in {"delete", "redirect"}:
        log.warning("staff thread guard row skipped: row=%s invalid action=%r", row_number, action)
        return None
    if action == "redirect" and not redirect_target_id:
        log.warning("staff thread guard row skipped: row=%s redirect_target_id missing", row_number)
        return None

    warning_text = _row_value(row, "warning_text")
    timeout_text = _row_value(row, "timeout_text")
    redirect_notice_text = _row_value(row, "redirect_notice_text")
    redirect_header_text = _row_value(row, "redirect_header_text")
    failure_text = _row_value(row, "failure_text")
    if not warning_text or not failure_text:
        log.warning("staff thread guard row skipped: row=%s warning/failure text missing", row_number)
        return None
    if action == "redirect" and (not redirect_notice_text or not redirect_header_text):
        log.warning("staff thread guard row skipped: row=%s redirect text missing", row_number)
        return None

    offense_window_minutes = _parse_int(_row_value(row, "offense_window_minutes"))
    timeout_after_offenses = _parse_int(_row_value(row, "timeout_after_offenses"))
    timeout_seconds = _parse_int(_row_value(row, "timeout_seconds"))
    if timeout_after_offenses > 0:
        if offense_window_minutes <= 0 or timeout_seconds <= 0 or not timeout_text:
            log.warning(
                "staff thread guard row=%s timeout disabled: repeat window, timeout seconds, and timeout text are required",
                row_number,
            )
            timeout_after_offenses = 0
            timeout_seconds = 0

    return GuardRule(
        guard_id=guard_id,
        thread_id=thread_id,
        action=action,
        redirect_target_id=redirect_target_id,
        warning_delete_after_seconds=_parse_int(_row_value(row, "warning_delete_after_seconds")),
        offense_window_minutes=offense_window_minutes,
        timeout_after_offenses=timeout_after_offenses,
        timeout_seconds=timeout_seconds,
        warning_text=warning_text,
        timeout_text=timeout_text,
        redirect_notice_text=redirect_notice_text,
        redirect_header_text=redirect_header_text,
        failure_text=failure_text,
    )


async def _tab_names() -> tuple[str, str] | None:
    rules_tab, offenses_tab = await asyncio.gather(
        recruitment.get_config_value_async(CONFIG_RULES_TAB, None),
        recruitment.get_config_value_async(CONFIG_OFFENSES_TAB, None),
    )
    if not rules_tab or not offenses_tab:
        log.warning(
            "staff thread guard unavailable: missing Config routing key(s) %s/%s",
            CONFIG_RULES_TAB,
            CONFIG_OFFENSES_TAB,
        )
        return None
    return str(rules_tab).strip(), str(offenses_tab).strip()


async def load_rules(*, force: bool = False) -> dict[int, GuardRule]:
    global _RULE_CACHE, _RULE_CACHE_LOADED_AT
    if not _feature_enabled():
        return {}

    now = time.monotonic()
    if not force and _RULE_CACHE_LOADED_AT and now - _RULE_CACHE_LOADED_AT < RULE_CACHE_TTL_SECONDS:
        return dict(_RULE_CACHE)

    async with _RULE_CACHE_LOCK:
        now = time.monotonic()
        if not force and _RULE_CACHE_LOADED_AT and now - _RULE_CACHE_LOADED_AT < RULE_CACHE_TTL_SECONDS:
            return dict(_RULE_CACHE)

        tabs = await _tab_names()
        if tabs is None:
            _RULE_CACHE = {}
            _RULE_CACHE_LOADED_AT = now
            return {}
        rules_tab, _offenses_tab = tabs
        sheet_id = recruitment.get_recruitment_sheet_id()
        try:
            records = await async_core.afetch_records(sheet_id, rules_tab)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("staff thread guard rules load failed: tab=%s", rules_tab)
            return dict(_RULE_CACHE)

        rules: dict[int, GuardRule] = {}
        guard_ids: set[str] = set()
        for row_number, row in enumerate(records, start=2):
            rule = _validate_rule(row, row_number=row_number)
            if rule is None:
                continue
            if rule.guard_id in guard_ids:
                log.warning("staff thread guard duplicate guard_id skipped: %s", rule.guard_id)
                continue
            if rule.thread_id in rules:
                log.warning("staff thread guard duplicate thread_id skipped: %s", rule.thread_id)
                continue
            guard_ids.add(rule.guard_id)
            rules[rule.thread_id] = rule

        _RULE_CACHE = rules
        _RULE_CACHE_LOADED_AT = now
        log.info("staff thread guard rules loaded: enabled_rules=%s", len(rules))
        return dict(rules)


def invalidate_cache() -> None:
    global _RULE_CACHE, _RULE_CACHE_LOADED_AT
    _RULE_CACHE = {}
    _RULE_CACHE_LOADED_AT = 0.0


def _offense_lock(guard_id: str, user_id: int) -> asyncio.Lock:
    key = (guard_id, user_id)
    lock = _OFFENSE_LOCKS.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _OFFENSE_LOCKS[key] = lock
    return lock


def _header_map(values: Sequence[Sequence[Any]]) -> dict[str, int]:
    if not values:
        return {}
    return {
        str(cell or "").strip().lower(): idx
        for idx, cell in enumerate(values[0])
        if str(cell or "").strip()
    }


def _state_from_values(
    values: Sequence[Sequence[Any]], *, guard_id: str, user_id: int
) -> OffenseState | None:
    headers = _header_map(values)
    missing = [header for header in OFFENSE_HEADERS if header not in headers]
    if missing:
        raise ValueError(f"staff thread guard offenses tab missing headers: {', '.join(missing)}")

    for sheet_row, raw in enumerate(values[1:], start=2):
        def cell(name: str) -> str:
            idx = headers[name]
            return str(raw[idx]).strip() if idx < len(raw) and raw[idx] is not None else ""

        if cell("guard_id") != guard_id or _parse_int(cell("user_id")) != user_id:
            continue
        return OffenseState(
            guard_id=guard_id,
            thread_id=_parse_int(cell("thread_id")),
            user_id=user_id,
            offense_count=_parse_int(cell("offense_count")),
            window_started_at_utc=cell("window_started_at_utc"),
            last_offense_at_utc=cell("last_offense_at_utc"),
            last_timeout_at_utc=cell("last_timeout_at_utc"),
            last_action=cell("last_action"),
            last_message_id=cell("last_message_id"),
            sheet_row=sheet_row,
        )
    return None


def _advance_offense(state: OffenseState | None, rule: GuardRule, *, user_id: int, now: datetime) -> OffenseState:
    window = timedelta(minutes=rule.offense_window_minutes) if rule.offense_window_minutes > 0 else None
    window_started = _parse_utc(state.window_started_at_utc) if state else None
    within_window = bool(window and window_started and now - window_started < window)

    if state is None:
        state = OffenseState(guard_id=rule.guard_id, thread_id=rule.thread_id, user_id=user_id)

    if within_window:
        state.offense_count += 1
    else:
        state.offense_count = 1
        state.window_started_at_utc = _format_utc(now)
    state.last_offense_at_utc = _format_utc(now)
    return state


def _state_row(state: OffenseState) -> list[str | int]:
    return [
        state.guard_id,
        str(state.thread_id),
        str(state.user_id),
        state.offense_count,
        state.window_started_at_utc,
        state.last_offense_at_utc,
        state.last_timeout_at_utc,
        state.last_action,
        state.last_message_id,
    ]


async def _load_and_advance_state(rule: GuardRule, message: discord.Message) -> OffenseState:
    tabs = await _tab_names()
    if tabs is None:
        return _advance_offense(None, rule, user_id=message.author.id, now=_utc_now())
    _rules_tab, offenses_tab = tabs
    sheet_id = recruitment.get_recruitment_sheet_id()
    values = await async_core.afetch_values(sheet_id, offenses_tab)
    state = _state_from_values(values, guard_id=rule.guard_id, user_id=message.author.id)
    return _advance_offense(state, rule, user_id=message.author.id, now=_utc_now())


async def _save_state(state: OffenseState) -> None:
    tabs = await _tab_names()
    if tabs is None:
        return
    _rules_tab, offenses_tab = tabs
    sheet_id = recruitment.get_recruitment_sheet_id()
    worksheet = await async_core.aget_worksheet(sheet_id, offenses_tab)
    row = _state_row(state)
    if state.sheet_row is None:
        await async_core.acall_with_backoff(worksheet.append_row, row)
    else:
        await async_core.acall_with_backoff(
            worksheet.update,
            f"A{state.sheet_row}:I{state.sheet_row}",
            [row],
        )


def _safe_format(template: str, values: Mapping[str, Any]) -> str:
    class SafeValues(dict[str, Any]):
        def __missing__(self, key: str) -> str:
            return "{" + key + "}"

    try:
        return str(template or "").format_map(SafeValues(values))
    except Exception:
        log.warning("staff thread guard message formatting failed", exc_info=True)
        return str(template or "")


def _format_values(
    rule: GuardRule,
    message: discord.Message,
    *,
    offense_count: int,
    redirect_target: Any | None = None,
) -> dict[str, Any]:
    author = message.author
    source = message.channel
    redirect_label = getattr(redirect_target, "mention", None) or (
        f"#{getattr(redirect_target, 'name', 'configured destination')}" if redirect_target is not None else "the proper deck"
    )
    source_label = getattr(source, "mention", None) or f"#{getattr(source, 'name', 'this thread')}"
    return {
        "user": getattr(author, "mention", None) or getattr(author, "display_name", "matey"),
        "user_name": getattr(author, "display_name", None) or getattr(author, "name", "matey"),
        "offense_count": offense_count,
        "timeout_seconds": rule.timeout_seconds,
        "redirect_target": redirect_label,
        "source_thread": source_label,
        "guard_id": rule.guard_id,
    }


async def _resolve_redirect_target(bot: commands.Bot, rule: GuardRule) -> discord.TextChannel | discord.Thread | None:
    if not rule.redirect_target_id:
        return None
    target = bot.get_channel(rule.redirect_target_id)
    if target is None:
        try:
            target = await bot.fetch_channel(rule.redirect_target_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            log.warning(
                "staff thread guard redirect target resolution failed: guard_id=%s target_id=%s",
                rule.guard_id,
                rule.redirect_target_id,
                exc_info=True,
            )
            return None
    if isinstance(target, (discord.TextChannel, discord.Thread)):
        return target
    log.warning(
        "staff thread guard redirect target is not a text channel/thread: guard_id=%s target_id=%s type=%s",
        rule.guard_id,
        rule.redirect_target_id,
        type(target).__name__,
    )
    return None


def _redirect_description(message: discord.Message) -> str:
    author = getattr(message.author, "display_name", None) or getattr(message.author, "name", "Unknown member")
    source = getattr(message.channel, "mention", None) or f"#{getattr(message.channel, 'name', 'unknown-thread')}"
    content = str(message.content or "").strip()
    if len(content) > 2600:
        content = content[:2599] + "…"
    if not content:
        content = "*No text content.*"

    lines = [f"**From:** {author}", f"**Originally posted in:** {source}", "", content]
    attachments = list(getattr(message, "attachments", []) or [])[:8]
    if attachments:
        lines.extend(["", "**Attachments:**"])
        for attachment in attachments:
            name = str(getattr(attachment, "filename", None) or "attachment")
            url = str(getattr(attachment, "url", None) or "").strip()
            if url:
                lines.append(f"[{name}]({url})")
    description = "\n".join(lines)
    return description[:4096]


async def _redirect_message(
    bot: commands.Bot,
    rule: GuardRule,
    message: discord.Message,
    *,
    offense_count: int,
) -> tuple[bool, Any | None]:
    target = await _resolve_redirect_target(bot, rule)
    if target is None:
        return False, None
    values = _format_values(rule, message, offense_count=offense_count, redirect_target=target)
    title = _safe_format(rule.redirect_header_text, values)[:256]
    embed = discord.Embed(title=title, description=_redirect_description(message))
    try:
        await target.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except (discord.Forbidden, discord.HTTPException):
        log.warning("staff thread guard redirect send failed: guard_id=%s", rule.guard_id, exc_info=True)
        return False, target
    return True, target


async def _delete_original(rule: GuardRule, message: discord.Message) -> bool:
    try:
        await message.delete()
    except (discord.Forbidden, discord.NotFound, discord.HTTPException):
        log.warning(
            "staff thread guard message delete failed: guard_id=%s message_id=%s",
            rule.guard_id,
            getattr(message, "id", None),
            exc_info=True,
        )
        return False
    return True


async def _apply_timeout(rule: GuardRule, message: discord.Message, *, offense_count: int) -> bool:
    if rule.timeout_after_offenses <= 0 or offense_count < rule.timeout_after_offenses:
        return False
    member = message.author
    if not isinstance(member, discord.Member):
        return False
    try:
        await member.timeout(
            timedelta(seconds=rule.timeout_seconds),
            reason=f"Woadkeeper staff-thread guard repeat offense ({rule.guard_id})",
        )
    except (discord.Forbidden, discord.HTTPException):
        log.warning(
            "staff thread guard timeout failed: guard_id=%s user_id=%s",
            rule.guard_id,
            getattr(member, "id", None),
            exc_info=True,
        )
        return False
    return True


async def _send_notice(
    rule: GuardRule,
    message: discord.Message,
    text: str,
    *,
    offense_count: int,
    redirect_target: Any | None,
) -> None:
    if not text:
        return
    values = _format_values(rule, message, offense_count=offense_count, redirect_target=redirect_target)
    rendered = _safe_format(text, values)
    embed = discord.Embed(description=rendered)
    delete_after = rule.warning_delete_after_seconds or None
    try:
        await message.channel.send(
            embed=embed,
            delete_after=delete_after,
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False, replied_user=False),
        )
    except (discord.Forbidden, discord.HTTPException):
        log.warning("staff thread guard warning send failed: guard_id=%s", rule.guard_id, exc_info=True)


async def handle_message(bot: commands.Bot, message: discord.Message) -> bool:
    """Enforce a configured staff-only thread rule. Return True when consumed."""

    if not _feature_enabled():
        return False
    if getattr(message, "guild", None) is None:
        return False
    if not isinstance(getattr(message, "channel", None), discord.Thread):
        return False
    author = getattr(message, "author", None)
    if author is None or getattr(author, "bot", False):
        return False
    if is_staff_member(author) or is_admin_member(author):
        return False

    rules = await load_rules()
    rule = rules.get(message.channel.id)
    if rule is None:
        return False

    async with _offense_lock(rule.guard_id, message.author.id):
        try:
            state = await _load_and_advance_state(rule, message)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("staff thread guard offense state load failed: guard_id=%s", rule.guard_id)
            state = _advance_offense(None, rule, user_id=message.author.id, now=_utc_now())

        offense_count = state.offense_count
        action_ok = False
        redirect_target: Any | None = None
        if rule.action == "redirect":
            redirect_ok, redirect_target = await _redirect_message(
                bot, rule, message, offense_count=offense_count
            )
            if redirect_ok:
                action_ok = await _delete_original(rule, message)
            else:
                action_ok = False
        else:
            action_ok = await _delete_original(rule, message)

        timed_out = await _apply_timeout(rule, message, offense_count=offense_count)
        if timed_out:
            state.last_timeout_at_utc = _format_utc(_utc_now())
            state.offense_count = 0
            state.window_started_at_utc = ""
            state.last_action = "timeout" if action_ok else "timeout_action_failed"
        else:
            state.last_action = rule.action if action_ok else f"{rule.action}_failed"
        state.last_message_id = str(getattr(message, "id", "") or "")

        try:
            await _save_state(state)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("staff thread guard offense state save failed: guard_id=%s", rule.guard_id)

        if not action_ok:
            notice = rule.failure_text
        elif timed_out and rule.action == "redirect":
            notice = f"{rule.redirect_notice_text}\n{rule.timeout_text}".strip()
        elif timed_out:
            notice = rule.timeout_text
        elif rule.action == "redirect":
            notice = rule.redirect_notice_text
        else:
            notice = rule.warning_text

        await _send_notice(
            rule,
            message,
            notice,
            offense_count=offense_count,
            redirect_target=redirect_target,
        )
        log.info(
            "staff thread guard enforced: guard_id=%s thread_id=%s user_id=%s action=%s action_ok=%s offense_count=%s timed_out=%s",
            rule.guard_id,
            rule.thread_id,
            message.author.id,
            rule.action,
            action_ok,
            offense_count,
            timed_out,
        )
        return True


def install(bot: commands.Bot) -> None:
    """Install the guard ahead of the app-level on_message handler, once per bot."""

    if getattr(bot, "_c1c_staff_thread_guard_installed", False):
        return
    original = getattr(bot, "on_message", None)
    if not callable(original):
        raise RuntimeError("bot.on_message unavailable for staff thread guard installation")

    async def guarded_on_message(message: discord.Message) -> None:
        try:
            handled = await handle_message(bot, message)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("staff thread guard unexpected enforcement failure")
            handled = False
        if handled:
            return
        await original(message)

    bot.on_message = guarded_on_message  # type: ignore[method-assign]
    setattr(bot, "_c1c_staff_thread_guard_installed", True)
    log.info("staff thread guard installed ahead of app on_message routing")
