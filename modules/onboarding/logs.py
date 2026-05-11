"""Helpers for routing onboarding logs to the shared log channel."""

from __future__ import annotations

import logging
import traceback
from functools import lru_cache
from typing import Any, Callable, Iterable, Mapping

import discord

from modules.common import runtime as rt
from shared import logfmt
from shared.dedupe import EventDeduper
from shared.sheets import onboarding_questions

__all__ = [
    "channel_path",
    "format_actor",
    "format_actor_handle",
    "format_channel",
    "format_tag",
    "format_match",
    "format_parent",
    "format_thread",
    "log_onboarding_panel_lifecycle",
    "question_stats",
    "thread_context",
    "send_welcome_log",
    "send_welcome_exception",
    "log_view_error",
    "human",
]

log = logging.getLogger("c1c.onboarding.logs")

_LOG_METHODS: dict[str, Callable[[str, Any], None]] = {}
_PANEL_DEDUPER = EventDeduper()

_LOG_RECORD_RESERVED_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
    }
)


def _resolve_logger(level: str) -> Callable[[str, Any], None]:
    """Return a logging function for the requested level."""

    if not _LOG_METHODS:
        _LOG_METHODS.update(
            {
                "debug": log.debug,
                "info": log.info,
                "warn": log.warning,
                "warning": log.warning,
                "error": log.error,
            }
        )
    return _LOG_METHODS.get(level, _LOG_METHODS["info"])


def _sanitize_log_extra(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of ``payload`` that is safe for ``logging`` extras."""

    sanitized: dict[str, Any] = {}
    reserved_targets = set(payload)
    for key, value in payload.items():
        if key in _LOG_RECORD_RESERVED_ATTRS:
            alias_base = f"context_{key}"
            alias = alias_base
            suffix = 1
            while alias in sanitized or alias in reserved_targets:
                suffix += 1
                alias = f"{alias_base}_{suffix}"
            sanitized[alias] = value
        else:
            sanitized[key] = value
    return sanitized

def _stringify(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, str):
        text = " ".join(value.split())
        return text or "-"
    if isinstance(value, Mapping):
        inner = ", ".join(f"{key}={_stringify(inner_value)}" for key, inner_value in value.items())
        return inner or "-"
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        return ", ".join(_stringify(item) for item in value) or "-"
    return str(value)


def channel_path(
    channel: discord.abc.GuildChannel | discord.Thread | None,
) -> str:
    """Return a ``#category › channel`` style label without numeric IDs."""

    if isinstance(channel, discord.Thread):
        parent = getattr(channel, "parent", None)
        return channel_path(parent)
    if isinstance(channel, discord.abc.GuildChannel):
        channel_name = (getattr(channel, "name", None) or "channel").strip() or "channel"
        category = getattr(channel, "category", None)
        if category is not None:
            category_name = (getattr(category, "name", None) or "category").strip() or "category"
            return f"#{category_name} › {channel_name}"
        return f"#{channel_name}"
    return "#unknown"


def _short_schema_version(raw: str | None) -> str | None:
    if not raw:
        return None
    token = str(raw).strip()
    if not token:
        return None
    if token.startswith("v") and len(token) <= 8:
        return token
    slug = "".join(ch for ch in token if ch.isalnum()) or token
    return f"v{slug[:6]}"


def _resolve_ticket_from_thread(
    thread: discord.Thread | None, *, scope: str | None = None
) -> str | None:
    if thread is None:
        return None
    name = getattr(thread, "name", None)
    if not name:
        return None
    try:
        from modules.onboarding.watcher_welcome import (
            parse_promo_thread_name,
            parse_welcome_thread_name,
        )
    except Exception:
        return None

    parsers = []
    normalized_scope = (scope or "").strip().lower()
    if normalized_scope.startswith("promo"):
        parsers.append(parse_promo_thread_name)
    else:
        parsers.append(parse_welcome_thread_name)
    if not parsers or normalized_scope == "":
        parsers = [parse_welcome_thread_name, parse_promo_thread_name]

    for parser in parsers:
        try:
            parts = parser(name)
        except Exception:
            continue
        if parts:
            ticket = getattr(parts, "ticket_code", None)
            if ticket:
                return ticket
    return None


@lru_cache(maxsize=4)
def question_stats(flow: str) -> tuple[int | None, str | None]:
    """Return the cached ``(question_count, schema_version)`` for ``flow``."""

    count: int | None = None
    schema: str | None = None
    try:
        count = len(onboarding_questions.get_questions(flow))
    except Exception:
        log.debug("failed to load onboarding questions for stats", exc_info=True)
    try:
        schema = onboarding_questions.schema_hash(flow)
    except Exception:
        log.debug("failed to resolve onboarding schema hash", exc_info=True)
    return count, _short_schema_version(schema)


def _normalize_actor(actor: Any) -> str | None:
    if not actor:
        return None
    if isinstance(actor, str):
        text = actor.strip()
        return text or None
    if isinstance(actor, (discord.Member, discord.abc.User)):
        return format_actor_handle(actor)
    handle = getattr(actor, "mention", None)
    if isinstance(handle, str):
        text = handle.strip()
        return text or None
    return None


def _normalize_channel(target: Any) -> str | None:
    if not target:
        return None
    if isinstance(target, str):
        text = target.strip()
        return text or None
    if isinstance(target, discord.Thread):
        parent = getattr(target, "parent", None)
        return channel_path(parent)
    if isinstance(target, discord.abc.GuildChannel):
        return channel_path(target)
    parent = getattr(target, "parent", None)
    if parent is not None:
        return channel_path(parent)
    return None


_RESULT_SEVERITY = {
    "error": "error",
    "failed": "error",
    "failure": "error",
    "exception": "error",
    "timeout": "warning",
    "skipped": "warning",
    "not_eligible": "warning",
    "partial": "warning",
    "retry": "warning",
    "deduped": "warning",
    "completed": "success",
    "complete": "success",
    "ok": "success",
    "success": "success",
}

_EVENT_SEVERITY = {
    "complete": "success",
    "error": "error",
}

_SEVERITY_LEVEL = {
    "info": logging.INFO,
    "success": logging.INFO,
    "warning": logging.WARNING,
    "error": logging.ERROR,
}

def _resolve_severity(event: str, result: str | None) -> str:
    normalized_result = (result or "").strip().lower()
    if normalized_result in _RESULT_SEVERITY:
        return _RESULT_SEVERITY[normalized_result]
    normalized_event = event.strip().lower()
    return _EVENT_SEVERITY.get(normalized_event, "info")


async def log_onboarding_panel_lifecycle(
    *,
    event: str,
    ticket: str | discord.Thread | None = None,
    actor: str | discord.abc.User | discord.Member | None = None,
    channel: str | discord.abc.GuildChannel | discord.Thread | None = None,
    questions: int | None = None,
    schema_version: str | None = None,
    result: str | None = None,
    reason: str | None = None,
    extras: Mapping[str, Any] | None = None,
    scope: str = "welcome",
    scope_label: str | None = None,
) -> None:
    """Emit a single lifecycle log entry for onboarding panel events."""

    # Normalize scope for CI and choose a sensible default label.
    scope_slug = (scope or "welcome").strip().lower() or "welcome"
    if scope_label is None:
        if scope_slug.startswith("promo"):
            scope_label = "Promo panel"
        elif scope_slug.startswith("welcome"):
            scope_label = "Welcome panel"
        else:
            scope_label = f"{scope_slug.capitalize()} panel"
    scope = scope_slug

    event_slug = (event or "event").strip().lower() or "event"
    emit_events = {"open", "complete", "timeout", "error"}
    if event_slug not in emit_events:
        log.debug("welcome panel lifecycle skipped", extra={"event": event_slug})
        return

    ticket_code: str | None
    if isinstance(ticket, discord.Thread):
        ticket_code = _resolve_ticket_from_thread(ticket, scope=scope)
    else:
        ticket_code = ticket
    missing_ticket = ticket is not None and ticket_code is None
    if ticket_code is None and isinstance(channel, discord.Thread):
        ticket_code = _resolve_ticket_from_thread(channel, scope=scope)

    actor_label = _normalize_actor(actor)
    channel_label = _normalize_channel(channel) or "#unknown"
    actor_name = format_actor_handle(actor) or actor_label

    question_label: str | None = None
    if questions is not None:
        try:
            question_label = str(int(questions))
        except (TypeError, ValueError):
            question_label = str(questions)

    schema_label = _short_schema_version(schema_version)

    resolved_result = result
    resolved_reason = reason
    if missing_ticket and resolved_result is None:
        resolved_result = "skipped"
        if not resolved_reason:
            resolved_reason = "ticket_not_parsed"

    extras_items: list[str] = []
    level_detail_value: str | None = None
    if extras:
        for key, value in extras.items():
            if value is None:
                continue
            cleaned = _stringify(value)
            if not cleaned or cleaned == "-":
                continue
            if key == "level_detail":
                level_detail_value = cleaned
                continue
            extras_items.append(f"{key}={cleaned}")
    extras_items.sort()

    level_map = {
        "open": logging.INFO,
        "complete": logging.INFO,
        "timeout": logging.WARNING,
        "error": logging.ERROR,
    }
    emoji_map = {
        "open": "🧭",
        "complete": "✅",
        "timeout": "⚠️",
        "error": "❌",
    }

    severity = _resolve_severity(event_slug, resolved_result)
    level = _SEVERITY_LEVEL.get(severity, level_map.get(event_slug, logging.INFO))
    severity_emoji = {
        "error": logfmt.LOG_EMOJI["error"],
        "warning": logfmt.LOG_EMOJI["warning"],
        "success": logfmt.LOG_EMOJI["success"],
    }
    emoji = severity_emoji.get(severity, emoji_map.get(event_slug, logfmt.LOG_EMOJI["lifecycle"]))

    resolved_scope = scope
    resolved_label = scope_label or (
        "Promo panel"
        if resolved_scope.startswith("promo")
        else "Welcome panel" if resolved_scope.startswith("welcome") else f"{resolved_scope.capitalize()} panel"
    )

    fields: list[str] = [f"scope={resolved_scope}"]
    if ticket_code:
        fields.append(f"ticket={ticket_code}")
    if actor_name and actor_name != "-":
        fields.append(f"actor={actor_name}")
    if channel_label and channel_label != "-":
        fields.append(f"channel={channel_label}")
    if question_label:
        fields.append(f"questions={question_label}")
    fields.append(f"action={event_slug}")
    result_label = _stringify(resolved_result) if resolved_result is not None else None
    if result_label and result_label != "-":
        fields.append(f"result={result_label}")
    if event_slug == "complete" and level_detail_value:
        fields.append(f"level_detail={level_detail_value}")
    reason_label = _stringify(resolved_reason) if resolved_reason is not None else None
    if reason_label and reason_label != "-":
        fields.append(f"reason={reason_label}")

    message = f"{emoji} {resolved_label} — " + " • ".join(fields)
    log.log(level, "%s", message)
    try:
        await rt.send_log_message(message)
    except Exception:
        log.warning("failed to emit onboarding lifecycle log", exc_info=True)


def format_actor(actor: discord.abc.User | discord.Member | None) -> str:
    """Return a stable representation for actors in welcome logs."""

    if actor is None:
        return "<unknown>"
    actor_id = getattr(actor, "id", None)
    if actor_id is None:
        return "<unknown>"
    return f"<{actor_id}>"


def format_actor_handle(actor: discord.abc.User | discord.Member | None) -> str | None:
    if actor is None:
        return None
    display = (
        getattr(actor, "display_name", None)
        or getattr(actor, "global_name", None)
        or getattr(actor, "name", None)
        or None
    )
    if not display:
        return None
    handle = f"@{display}".replace(" ", "_")
    return handle


def format_channel(channel: discord.abc.GuildChannel | discord.Thread | None) -> str | None:
    if channel is None:
        return None
    guild = getattr(channel, "guild", None)
    identifier = getattr(channel, "id", None)
    return logfmt.channel_label(guild, identifier)


def _extract_thread_tag(thread: discord.Thread | None) -> str | None:
    if thread is None:
        return None
    applied = getattr(thread, "applied_tags", None)
    if applied:
        for tag in applied:
            name = getattr(tag, "name", None)
            if name:
                return str(name)
    name = getattr(thread, "name", "") or ""
    if name.startswith("[") and "]" in name:
        inner = name[1 : name.find("]")].strip()
        if inner:
            return inner
    return None


def format_tag(thread: discord.Thread | None) -> str | None:
    tag = _extract_thread_tag(thread)
    if tag:
        return f"<{tag}>"
    return "<unknown>"


def format_thread(thread_id: int | None) -> str | None:
    if thread_id is None:
        return None
    return f"<{thread_id}>"


def format_parent(parent_id: int | None) -> str | None:
    if parent_id is None:
        return None
    return f"<{parent_id}>"


def format_match(match: str | None) -> str | None:
    if match is None:
        return None
    return f"<{match}>"


def thread_context(thread: discord.Thread | None) -> dict[str, Any]:
    if thread is None:
        return {}
    context: dict[str, Any] = {
        "tag": format_tag(thread),
        "channel": format_channel(thread),
        "thread": format_thread(getattr(thread, "id", None)),
        "parent": format_channel(getattr(thread, "parent", None)),
    }
    guild = getattr(thread, "guild", None)
    thread_id = getattr(thread, "id", None)
    parent = getattr(thread, "parent", None)
    parent_id = getattr(parent, "id", None)
    if thread_id is not None:
        context["thread_label"] = logfmt.channel_label(guild, thread_id)
    if parent_id is not None:
        context["channel_label"] = logfmt.channel_label(guild, parent_id)
    return context


def human(level: str, event: str, **payload: Any) -> None:
    """Emit a lightweight human log entry for onboarding flows."""

    logger = _resolve_logger(level)
    details = {**payload, "event": event}
    formatted = ", ".join(
        f"{key}={_stringify(value)}" for key, value in sorted(details.items())
    )
    logger("👤 %s", formatted)


async def send_welcome_log(level: str, **kv: Any) -> None:
    """Send a formatted welcome log message to the shared log channel."""

    payload_map = dict(kv)

    message = _render_payload(payload_map)
    if not message:
        return

    sanitized_extra = _sanitize_log_extra(payload_map)
    log_method = _resolve_logger(level)
    log_method("%s", sanitized_extra)

    dedupe_key = _dedupe_key(payload_map)
    should_emit = True if dedupe_key is None else _PANEL_DEDUPER.should_emit(dedupe_key)
    if not should_emit:
        return

    try:
        await rt.send_log_message(message)
    except Exception:  # pragma: no cover - defensive logging path
        log.warning("failed to send welcome log message", exc_info=True)
        log.info(message)


async def send_welcome_exception(level: str, error: BaseException, **kv: Any) -> None:
    trace = "".join(traceback.format_exception(error))
    details = dict(kv)
    details.setdefault("result", "error")
    details.setdefault("error", f"{error.__class__.__name__}: {error}")
    details["trace"] = trace
    await send_welcome_log(level, **details)


def log_view_error(
    interaction: discord.Interaction,
    view: discord.ui.View,
    err: BaseException,
    *,
    tag: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> None:
    """Emit a resilient error log for view callbacks.

    The Discord interaction payload frequently omits optional attributes. Guard
    every access so the logging path never raises and masks the original
    exception.
    """

    def _safe(getter: Callable[[], Any], default: Any = None) -> Any:
        try:
            return getter()
        except Exception:
            return default

    data = _safe(lambda: getattr(interaction, "data", None)) or {}
    response = getattr(interaction, "response", None)

    def _response_is_done() -> bool:
        if response is None:
            return False
        flag = getattr(response, "is_done", None)
        if callable(flag):
            return bool(_safe(flag, False))
        if flag is None:
            return False
        try:
            return bool(flag)
        except Exception:
            return False

    payload: dict[str, Any] = {
        "diag": "welcome_flow",
        "event": "view_error",
        "view": type(view).__name__,
        "view_tag": getattr(view, "tag", None) or tag or "unknown",
        "custom_id": _safe(lambda: data.get("custom_id")),
        "component_type": _safe(lambda: data.get("component_type")),
        "message_id": _safe(lambda: getattr(interaction.message, "id", None)),
        "interaction_id": _safe(lambda: getattr(interaction, "id", None)),
        "actor": str(_safe(lambda: getattr(interaction, "user", None))),
        "actor_id": _safe(lambda: getattr(getattr(interaction, "user", None), "id", None)),
        "actor_name": _safe(lambda: getattr(getattr(interaction, "user", None), "mention", None)),
        "response_is_done": _response_is_done(),
        "app_permissions": str(getattr(interaction, "app_permissions", None)),
        "claimed": getattr(interaction, "_c1c_claimed", False),
    }

    if extra:
        payload.update(extra)

    sanitized = _sanitize_log_extra(payload)
    sanitized.setdefault("error_class", err.__class__.__name__)
    sanitized.setdefault("error_message", str(err))

    try:
        log.error(
            "welcome view error",
            exc_info=(err.__class__, err, err.__traceback__),
            extra=sanitized,
        )
    except Exception:
        # As an extreme fallback emit a minimal record so the originating error
        # never disappears behind a logging failure.
        log.error(
            "welcome view error (minimal)",
            exc_info=(err.__class__, err, err.__traceback__),
            extra={"diag": "welcome_flow", "event": "view_error_min"},
        )


def _dedupe_key(payload: Mapping[str, Any]) -> str | None:
    if "tag" in payload and "recruit" in payload:
        return ":".join(
            str(part)
            for part in (
                "welcome_summary",
                payload.get("tag") or "-",
                payload.get("recruit") or "-",
                payload.get("result") or "-",
            )
        )
    thread_id = payload.get("thread_id") or payload.get("thread")
    message_id = payload.get("message_id")
    actor_id = payload.get("actor_id") or payload.get("actor")
    view_tag = payload.get("view_tag") or payload.get("view") or "welcome_panel"
    result = payload.get("result")
    if not result:
        return None
    return ":".join(
        str(part)
        for part in (
            view_tag,
            thread_id or "unknown",
            message_id or "-",
            actor_id or "-",
            result,
        )
    )


def _render_payload(payload: Mapping[str, Any]) -> str | None:
    message: str | None
    if "tag" in payload and "recruit" in payload and "channel" in payload:
        details = payload.get("details")
        detail_items: list[str] = []
        if isinstance(details, Mapping):
            detail_items = [f"{key}={_stringify(value)}" for key, value in details.items()]
        elif isinstance(details, Iterable) and not isinstance(details, (str, bytes, bytearray)):
            detail_items = [_stringify(details)]
        elif details is not None:
            detail_items = [_stringify(details)]
        message = logfmt.LogTemplates.welcome(
            tag=_stringify(payload.get("tag")),
            recruit=_stringify(payload.get("recruit")),
            channel=_stringify(payload.get("channel")),
            result=_stringify(payload.get("result")),
            details=detail_items,
        )
    else:
        actor_display = payload.get("actor_name") or _stringify(payload.get("actor"))
        thread_label = _stringify(
            payload.get("thread_label") or payload.get("channel") or payload.get("thread")
        )
        parent_label = _stringify(
            payload.get("channel_label") or payload.get("parent") or payload.get("parent_channel")
        )
        result = _stringify(payload.get("result"))

        detail_items: list[str] = []
        detail_fields = [
            ("view", ("view_tag", "view")),
            ("view_id", ("view_id",)),
            ("missing", ("missing",)),
            ("source", ("source",)),
            ("reason", ("reason",)),
            ("schema", ("schema",)),
            ("questions", ("questions",)),
            ("emoji", ("emoji",)),
        ]
        for name, keys in detail_fields:
            value = None
            for key in keys:
                candidate = payload.get(key)
                if candidate is not None:
                    value = candidate
                    break
            if value is None:
                continue
            detail_items.append(f"{name}={_stringify(value)}")


        extra_details = payload.get("details")
        if isinstance(extra_details, Mapping):
            detail_items.extend(
                f"{key}={_stringify(value)}" for key, value in extra_details.items()
            )
        elif isinstance(extra_details, Iterable) and not isinstance(extra_details, (str, bytes, bytearray)):
            detail_items.append(_stringify(extra_details))
        elif extra_details is not None:
            detail_items.append(_stringify(extra_details))

        error_text = payload.get("error")
        if error_text:
            detail_items.append(f"error={_stringify(error_text)}")

        message = logfmt.LogTemplates.welcome_panel(
            actor=str(actor_display) if actor_display else "-",
            thread=thread_label,
            parent=parent_label if parent_label != "-" else None,
            result=result,
            details=detail_items,
        )

    if not message:
        return None
    return message
