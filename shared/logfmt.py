"""Human-friendly logging helpers and templates for Discord posts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

import discord

__all__ = [
    "LOG_EMOJI",
    "BucketResult",
    "channel_label",
    "user_label",
    "guild_label",
    "fmt_duration",
    "fmt_count",
    "human_reason",
    "LogTemplates",
]

LOG_EMOJI = {
    "success": "✅",
    "info": "📋",
    "lifecycle": "📘",
    "refresh": "♻️",
    "watchdog": "🐶",
    "security": "🔐",
    "scheduler": "🧭",
    "warning": "⚠️",
    "error": "❌",
}

_DEFAULT_SCHEDULER_BUCKETS: tuple[str, ...] = (
    "clans",
    "templates",
    "clan_tags",
    "onboarding_questions",
)

def _clean_name(name: Optional[str], default: str) -> str:
    if not name:
        return default
    text = str(name).strip()
    return text or default


def channel_label(guild: Optional[discord.Guild], cid: Optional[int]) -> str:
    """Return a human-friendly label for a guild channel or thread."""

    if guild is None or cid is None:
        return "#unknown"

    channel = guild.get_channel(cid)
    thread: Optional[discord.Thread] = None
    if channel is None:
        try:
            thread = guild.get_thread(cid)  # type: ignore[attr-defined]
        except AttributeError:
            thread = None
        except Exception:
            thread = None
        if thread is not None:
            channel = thread

    if isinstance(channel, discord.Thread):
        parent = getattr(channel, "parent", None)
        parent_name = _clean_name(getattr(parent, "name", None), "unknown")
        thread_name = _clean_name(channel.name, "thread")
        label = f"#{parent_name} › {thread_name}"
        return label

    if isinstance(channel, discord.abc.GuildChannel):
        name = _clean_name(getattr(channel, "name", None), "channel")
        category = getattr(channel, "category", None)
        if category is not None:
            cat_name = _clean_name(getattr(category, "name", None), "category")
            label = f"#{cat_name} › {name}"
        else:
            label = f"#{name}"
        return label

    return "#unknown"


def user_label(guild: Optional[discord.Guild], uid: Optional[int]) -> str:
    """Return a human label for a guild user/member."""

    if uid is None:
        return "unknown"
    if guild is None:
        return "unknown"
    getter = getattr(guild, "get_member", None)
    member = getter(uid) if callable(getter) else None
    if member is None:
        return "unknown"
    display = _clean_name(getattr(member, "display_name", None), "unknown")
    return display


def guild_label(bot: discord.Client, gid: Optional[int]) -> str:
    """Return a human label for a guild known to the bot."""

    if gid is None:
        return "unknown guild"
    guild = bot.get_guild(gid)
    if guild is None:
        return "unknown guild"
    name = _clean_name(getattr(guild, "name", None), "guild")
    return name


def _format_unit(value: float, unit: str) -> str:
    if abs(value - round(value)) < 0.05:
        return f"{int(round(value))}{unit}"
    return f"{value:.1f}{unit}"


def fmt_duration(value: float | int) -> str:
    """Format a duration expressed in milliseconds or seconds."""

    try:
        raw = float(value)
    except (TypeError, ValueError):
        return "0s"
    seconds = raw
    if raw > 120:  # assume milliseconds if clearly above 2 minutes when treated as seconds
        seconds = raw / 1000.0 if raw >= 1000 else raw
    if seconds < 0:
        seconds = 0.0
    if seconds < 60:
        return _format_unit(seconds, "s")
    minutes = seconds / 60.0
    if minutes < 60:
        return _format_unit(minutes, "m")
    hours = minutes / 60.0
    return _format_unit(hours, "h")


def fmt_count(value: Optional[int]) -> str:
    if value is None:
        return "-"
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "-"


def _ordered_scheduler_buckets(
    intervals: Mapping[str, str], upcoming: Mapping[str, str]
) -> list[str]:
    order: list[str] = []
    seen: set[str] = set()
    for key in _DEFAULT_SCHEDULER_BUCKETS:
        if key in intervals or key in upcoming:
            order.append(key)
            seen.add(key)
    for mapping in (intervals, upcoming):
        for key in mapping:
            if key not in seen:
                order.append(key)
                seen.add(key)
    if not order:
        order = list(_DEFAULT_SCHEDULER_BUCKETS)
    return order


def _format_pairs(pairs: Sequence[tuple[str, Optional[str]]]) -> list[str]:
    formatted: list[str] = []
    for key, value in pairs:
        if not key:
            continue
        if value is None:
            continue
        text = str(value).strip()
        if not text or text == "-":
            continue
        formatted.append(f"{key}={text}")
    return formatted


_HTTP_ERROR_CODES = {
    50001: "Missing Access",
    50013: "Missing Permissions",
    50035: "Invalid Form Body",
    50073: "Cannot Send Messages to This User",
    50083: "Webhooks Rate Limited",
}


def human_reason(exc_or_msg: object) -> str:
    """Normalize Discord HTTP errors to human-friendly text."""

    if exc_or_msg is None:
        return "-"
    if isinstance(exc_or_msg, str):
        text = " ".join(exc_or_msg.split())
        return text or "-"
    if isinstance(exc_or_msg, discord.HTTPException):
        status = getattr(exc_or_msg, "status", None)
        code = getattr(exc_or_msg, "code", None)
        base = _HTTP_ERROR_CODES.get(code, exc_or_msg.__class__.__name__)
        suffix = ""
        if status or code:
            suffix = f" ({status or '?'}" + (f"/{code}" if code else "") + ")"
        detail = " ".join(str(getattr(exc_or_msg, "text", "")).split())
        if detail:
            return f"{base}{suffix}: {detail}"
        return f"{base}{suffix}".strip()
    if isinstance(exc_or_msg, Exception):
        text = " ".join(str(exc_or_msg).split())
        label = exc_or_msg.__class__.__name__
        return f"{label}: {text}" if text else label
    return "-"


@dataclass(frozen=True)
class BucketResult:
    name: str
    status: str
    duration_s: float
    item_count: Optional[int]
    ttl_ok: Optional[bool]
    retries: Optional[int] = None
    reason: Optional[str] = None
    metadata: Mapping[str, str] | None = None

    @property
    def ok(self) -> bool:
        normalized = self.status.lower().strip()
        return normalized in {"ok", "success"}


class LogTemplates:
    """Factory helpers for humanized log messages."""

    @staticmethod
    def scheduler(*, intervals: Mapping[str, str], upcoming: Mapping[str, str]) -> str:
        bucket_order = _ordered_scheduler_buckets(intervals, upcoming)
        interval_pairs = _format_pairs(
            [(bucket, intervals.get(bucket, "-")) for bucket in bucket_order]
        )
        cadence = " • ".join(interval_pairs) or "-"
        lines = [f"{LOG_EMOJI['scheduler']} **Scheduler** — intervals: {cadence}"]
        for bucket in bucket_order:
            next_value = upcoming.get(bucket)
            text = str(next_value).strip() if next_value else "-"
            lines.append(f"• {bucket}={text or '-'}")
        return "\n".join(lines)

    @staticmethod
    def scheduler_failure(*, job: str, reason: str) -> str:
        detail = reason or "-"
        return (
            f"{LOG_EMOJI['error']} **Scheduler** — failure • job={job} • reason={detail}"
        )

    @staticmethod
    def allowlist(*, allowed: Sequence[str], connected: Sequence[str], ok: bool) -> str:
        emoji = LOG_EMOJI["success"] if ok else LOG_EMOJI["warning"]
        allowed_text = ", ".join(allowed) or "—"
        connected_text = ", ".join(connected) or "—"
        state = "verified" if ok else "warning"
        return (
            f"{emoji} **Guild allow-list** — {state} • "
            f"allowed=[{allowed_text}] • connected=[{connected_text}]"
        )

    @staticmethod
    def allowlist_violation(*, allowed: Sequence[str], offending: Sequence[str]) -> str:
        allowed_text = ", ".join(allowed) or "—"
        offending_text = ", ".join(offending) or "—"
        return (
            f"{LOG_EMOJI['error']} **Guild allow-list** — violation • "
            f"connected=[{offending_text}] • allowed=[{allowed_text}]"
        )

    @staticmethod
    def watchdog(*, interval_s: int, stall_s: int, disconnect_grace_s: int) -> str:
        return (
            f"{LOG_EMOJI['watchdog']} **Watchdog started** — "
            f"interval={interval_s}s • stall={stall_s}s • disconnect_grace={disconnect_grace_s}s"
        )

    @staticmethod
    def refresh(scope: str, buckets: Sequence[BucketResult], total_s: float) -> str:
        lines = [f"{LOG_EMOJI['refresh']} **Refresh** — scope={scope}"]
        for result in buckets:
            details: list[str] = [fmt_duration(result.duration_s)]
            if result.item_count is not None:
                details.append(fmt_count(result.item_count))
            if result.ttl_ok is True:
                details.append("ttl")
            elif result.ttl_ok is False:
                details.append("stale")
            if result.retries:
                details.append(f"{result.retries}× retry")
            if result.reason and not result.ok:
                details.append(result.reason)
            if result.metadata:
                for key, value in sorted(result.metadata.items()):
                    if result.name == "onboarding_questions" and key in {"sheet", "tab"}:
                        continue
                    if key and value:
                        details.append(f"{key}={value}")
            detail_text = ", ".join(details) if details else "-"
            lines.append(f"• {result.name} {result.status} ({detail_text})")
        lines.append(f"• total={fmt_duration(total_s)}")
        return "\n".join(lines)

    @staticmethod
    def refresh_table(scope: str, buckets: Sequence[BucketResult], total_s: float) -> str:
        header = f"{LOG_EMOJI['refresh']} Refresh • {scope} — total {fmt_duration(total_s)}"
        lines = ["bucket result items ttl duration"]
        for result in buckets:
            ttl_text = "yes" if result.ttl_ok else ("no" if result.ttl_ok is False else "?")
            lines.append(
                " ".join(
                    [
                        result.name,
                        result.status,
                        fmt_count(result.item_count),
                        ttl_text,
                        fmt_duration(result.duration_s),
                    ]
                )
            )
        table = "\n".join(lines)
        return "\n".join([header, f"```text\n{table}\n```"])

    @staticmethod
    def report(
        *,
        kind: str,
        actor: str,
        user: str,
        guild: str,
        dest: str,
        date: str,
        ok: bool,
        reason: Optional[str],
    ) -> str:
        emoji = LOG_EMOJI["success"] if ok else LOG_EMOJI["error"]
        reason_text = reason or "-"
        return (
            f"{emoji} **Report: {kind}** — actor={actor} • user={user} • "
            f"guild={guild} • dest={dest} • date={date} • reason={reason_text}"
        )

    @staticmethod
    def cache(
        *,
        bucket: str,
        ok: bool,
        duration_s: float,
        retries: Optional[int],
        reason: Optional[str],
    ) -> str:
        emoji = LOG_EMOJI["refresh"]
        status = "OK" if ok else "FAIL"
        details: list[str] = [fmt_duration(duration_s)]
        if retries:
            details.append(f"{fmt_count(retries)} retries")
        if reason and not ok:
            details.append(reason)
        detail_text = " • ".join(details)
        return f"{emoji} **Cache: {bucket}** — {status} • {detail_text}"

    @staticmethod
    def cmd_error(*, command: str, user: str, reason: str) -> str:
        return (
            f"{LOG_EMOJI['warning']} **Command error** — cmd={command or '-'} "
            f"• user={user or '-'} • reason={reason or '-'}"
        )

    @staticmethod
    def welcome(
        *,
        tag: str,
        recruit: str,
        channel: str,
        result: str,
        details: Sequence[str] | None,
    ) -> str:
        emoji = {
            "ok": LOG_EMOJI["success"],
            "partial": LOG_EMOJI["warning"],
            "error": LOG_EMOJI["error"],
        }.get(result, LOG_EMOJI["info"])
        extras = "; ".join(details or []) if details else "-"
        return (
            f"{emoji} **Welcome** — tag={tag} • recruit={recruit} • channel={channel} "
            f"• result={result} • details: {extras}"
        )

    @staticmethod
    def welcome_panel(
        *,
        actor: str,
        thread: str,
        parent: str | None,
        result: str,
        details: Sequence[str] | None = None,
    ) -> str:
        palette = {
            "allowed": LOG_EMOJI["success"],
            "launched": LOG_EMOJI["success"],
            "saved": LOG_EMOJI["success"],
            "submitted": LOG_EMOJI["success"],
            "completed": LOG_EMOJI["success"],
            "started": LOG_EMOJI["success"],
            "reopened": LOG_EMOJI["success"],
            "refreshed": LOG_EMOJI["success"],
            "restarted": LOG_EMOJI["info"],
            "emoji_received": LOG_EMOJI["info"],
            "deduped": LOG_EMOJI["info"],
            "feature_disabled": LOG_EMOJI["warning"],
            "role_gate": LOG_EMOJI["warning"],
            "scope_gate": LOG_EMOJI["warning"],
            "denied_role": LOG_EMOJI["warning"],
            "denied_perms": LOG_EMOJI["warning"],
            "ambiguous_target": LOG_EMOJI["warning"],
            "timeout": LOG_EMOJI["warning"],
            "inactive": LOG_EMOJI["warning"],
            "no_trigger": LOG_EMOJI["warning"],
            "wrong_scope": LOG_EMOJI["warning"],
            "panel_failed": LOG_EMOJI["error"],
            "launch_failed": LOG_EMOJI["error"],
            "error": LOG_EMOJI["error"],
        }
        emoji = palette.get(result, LOG_EMOJI["info"])
        actor_text = (actor or "").strip()
        thread_text = (thread or "#unknown").strip() or "#unknown"
        cleaned_details = [item for item in (details or []) if item and item != "-"]

        segments: list[str] = []
        if actor_text and actor_text != "-":
            segments.append(f"actor={actor_text}")
        if thread_text and thread_text != "-":
            segments.append(f"thread={thread_text}")
        if parent and parent != "-":
            segments.append(f"channel={parent}")
        if result and result != "-":
            segments.append(f"result={result}")
        if cleaned_details:
            segments.append("details: " + "; ".join(cleaned_details))

        if segments:
            return f"{emoji} Welcome panel — " + " • ".join(segments)
            return f"{emoji} Welcome panel"

    @staticmethod
    def _welcome_panel_event(
        *,
        event: str,
        ticket: Optional[str],
        actor: Optional[str],
        channel: Optional[str],
        questions: Optional[str],
        schema: Optional[str] = None,
        level_detail: Optional[str] = None,
        result: Optional[str] = None,
        reason: Optional[str] = None,
        extras: Sequence[str] | None = None,
    ) -> str:
        emoji = LogTemplates._resolve_welcome_panel_emoji(event, result)
        summary_fields = [("ticket", ticket), ("actor", actor)]
        summary_text = " • ".join(_format_pairs(summary_fields)) or "-"
        detail_fields = [
            ("channel", channel),
            ("questions", questions),
            ("schema", schema),
            ("level_detail", level_detail),
            ("result", result),
        ]
        details = _format_pairs(detail_fields)
        if reason and emoji in (LOG_EMOJI["warning"], LOG_EMOJI["error"]):
            details.append(f"reason={reason}")
        if extras:
            details.extend(item for item in extras if item)
        lines = [f"{emoji} {event} — {summary_text}"]
        if details:
            lines.append(f"• {' • '.join(details)}")
        return "\n".join(lines)

    @staticmethod
    def _resolve_welcome_panel_emoji(event: str, result: Optional[str]) -> str:
        normalized_result = (result or "").strip().lower()
        if normalized_result in {"error", "failed", "exception"}:
            return LOG_EMOJI["error"]
        if normalized_result in {"skipped", "not_eligible", "partial"}:
            return LOG_EMOJI["warning"]
        event_lower = event.strip().lower()
        if event_lower.endswith("_restart"):
            return LOG_EMOJI["refresh"]
        if event_lower.endswith("_complete"):
            return LOG_EMOJI["success"]
        return LOG_EMOJI["lifecycle"]

    @staticmethod
    def welcome_panel_open(
        *,
        ticket: Optional[str],
        actor: Optional[str],
        channel: Optional[str],
        questions: Optional[str],
        result: Optional[str] = None,
        reason: Optional[str] = None,
        extras: Sequence[str] | None = None,
    ) -> str:
        return LogTemplates._welcome_panel_event(
            event="welcome_panel_open",
            ticket=ticket,
            actor=actor,
            channel=channel,
            questions=questions,
            result=result,
            reason=reason,
            extras=extras,
        )

    @staticmethod
    def welcome_panel_start(
        *,
        ticket: Optional[str],
        actor: Optional[str],
        channel: Optional[str],
        questions: Optional[str],
        schema: Optional[str],
        result: Optional[str] = None,
        reason: Optional[str] = None,
        extras: Sequence[str] | None = None,
    ) -> str:
        return LogTemplates._welcome_panel_event(
            event="welcome_panel_start",
            ticket=ticket,
            actor=actor,
            channel=channel,
            questions=questions,
            schema=schema,
            result=result,
            reason=reason,
            extras=extras,
        )

    @staticmethod
    def welcome_panel_restart(
        *,
        ticket: Optional[str],
        actor: Optional[str],
        channel: Optional[str],
        questions: Optional[str],
        schema: Optional[str],
        result: Optional[str] = None,
        reason: Optional[str] = None,
        extras: Sequence[str] | None = None,
    ) -> str:
        return LogTemplates._welcome_panel_event(
            event="welcome_panel_restart",
            ticket=ticket,
            actor=actor,
            channel=channel,
            questions=questions,
            schema=schema,
            result=result,
            reason=reason,
            extras=extras,
        )

    @staticmethod
    def welcome_panel_complete(
        *,
        ticket: Optional[str],
        actor: Optional[str],
        channel: Optional[str],
        questions: Optional[str],
        level_detail: Optional[str],
        result: Optional[str] = None,
        reason: Optional[str] = None,
        extras: Sequence[str] | None = None,
    ) -> str:
        return LogTemplates._welcome_panel_event(
            event="welcome_panel_complete",
            ticket=ticket,
            actor=actor,
            channel=channel,
            questions=questions,
            level_detail=level_detail,
            result=result,
            reason=reason,
            extras=extras,
        )

    @staticmethod
    def welcome_panel_generic(
        *,
        event: str,
        ticket: Optional[str],
        actor: Optional[str],
        channel: Optional[str],
        questions: Optional[str],
        schema: Optional[str],
        level_detail: Optional[str],
        result: Optional[str] = None,
        reason: Optional[str] = None,
        extras: Sequence[str] | None = None,
    ) -> str:
        return LogTemplates._welcome_panel_event(
            event=f"welcome_panel_{event}",
            ticket=ticket,
            actor=actor,
            channel=channel,
            questions=questions,
            schema=schema,
            level_detail=level_detail,
            result=result,
            reason=reason,
            extras=extras,
        )

    @staticmethod
    def select_refresh_template(scope: str, buckets: Sequence[BucketResult], total_s: float) -> str:
        return LogTemplates.refresh(scope, buckets, total_s)
