from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

import discord
from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only

from modules.common import feature_flags
from modules.common import runtime as runtime_helpers
from shared.config import get_command_prefix
from shared.sheets import async_core
from shared.sheets import recruitment

log = logging.getLogger("c1c.housekeeping.cleanup")

CONFIG_ENABLED = "HOUSEKEEPING_CLEANUP_ENABLED"
CONFIG_TAB = "HOUSEKEEPING_CLEANUP_TAB"
CONFIG_RUN_EVERY_HOURS = "HOUSEKEEPING_CLEANUP_RUN_EVERY_HOURS"
CONFIG_DRY_RUN = "HOUSEKEEPING_CLEANUP_DRY_RUN"
CONFIG_BOT_ROLE_IDS = "HOUSEKEEPING_CLEANUP_BOT_ROLE_IDS"
REQUIRED_CONFIG_KEYS = (CONFIG_TAB, CONFIG_RUN_EVERY_HOURS, CONFIG_DRY_RUN)
REQUIRED_HEADERS = (
    "enabled",
    "target_id",
    "target_type",
    "target_name",
    "parent_name",
    "cleanup_mode",
    "min_age_hours",
    "last_checked_at_utc",
    "last_deleted_count",
    "last_candidate_count",
    "last_skipped_count",
    "last_status",
    "notes",
)
BOT_WRITABLE_HEADERS = (
    "target_type",
    "target_name",
    "parent_name",
    "last_checked_at_utc",
    "last_deleted_count",
    "last_candidate_count",
    "last_skipped_count",
    "last_status",
)
ALLOWED_TARGET_TYPES = {"thread", "channel"}
# ``target_type=channel`` means cleanup of the configured target's own
# message history. Child threads are not discovered or traversed automatically.
SUPPORTED_TARGET_TYPES = {"thread", "channel"}
ALLOWED_CLEANUP_MODES = {
    "all_non_pinned",
    "bot_messages_only",
    "commands_only",
    "bot_messages_and_commands",
    "bot_and_webhook_messages_only",
    "bot_webhook_messages_and_commands",
    "automod_system_messages_only",
    "automod_system_and_webhook_messages_only",
}
_CLEANUP_RUN_LOCK = asyncio.Lock()


@dataclass(frozen=True)
class CleanupConfig:
    enabled: bool
    tab_name: str
    run_every_hours: float
    dry_run: bool
    source: str = "Config"


@dataclass
class CleanupRow:
    sheet_row: int
    values: dict[str, str]


@dataclass
class CleanupResult:
    status: str
    deleted: int = 0
    candidates: int = 0
    skipped: int = 0
    errors: int = 0


@dataclass
class CleanupRunSummary:
    checked_rows: int = 0
    dry_run: bool = False
    writeback: bool = True
    deleted: int = 0
    candidates: int = 0
    skipped: int = 0
    errors: int = 0
    status: str = "ok"
    first_error: str = ""
    summary_notice_failed: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ZeroMatchDiagnostics:
    total_seen: int = 0
    bot_author_seen: int = 0
    command_seen: int = 0
    unique_author_count: int = 0
    author_bot_true_count: int = 0
    own_bot_author_count: int = 0
    webhook_message_count: int = 0
    system_message_count: int = 0
    automod_system_seen: int = 0
    message_type_counts: str = "-"
    empty_content_count: int = 0
    messages_with_content_count: int = 0
    prefix_matched_count: int = 0
    role_matched_count: int = 0
    top_author_sample: str = "-"


@dataclass
class _AuthorSourceCounts:
    count: int = 0
    bot_true_count: int = 0
    webhook_count: int = 0


def _short_error(exc: BaseException, *, limit: int = 180) -> str:
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


def _format_summary_error(summary: CleanupRunSummary, *, limit: int = 180) -> str:
    first_error = " ".join((summary.first_error or "").split())
    if not first_error:
        return ""
    stage_marker = " stage="
    if stage_marker in first_error:
        first_error, stage = first_error.split(stage_marker, 1)
        stage = stage.split()[0].strip()
        first_error = f"{first_error.strip()} at {stage}" if stage else first_error.strip()
    if len(first_error) > limit:
        first_error = first_error[: max(0, limit - 1)].rstrip() + "…"
    return first_error


def _manual_finished_message(summary: CleanupRunSummary) -> str:
    has_errors = summary.errors > 0 or bool(summary.first_error)
    prefix = "Cleanup run finished with errors: " if has_errors else "Cleanup run finished: "
    message = (
        prefix +
        f"deleted={summary.deleted} candidates={summary.candidates} "
        f"skipped={summary.skipped} errors={summary.errors}"
    )
    if has_errors:
        message += f" status={summary.status}"
        first_error = _format_summary_error(summary)
        if first_error:
            message += f" first_error={first_error}"
    if summary.summary_notice_failed:
        message += "; Discord summary notice failed. See app logs."
    return message


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(value: datetime | None) -> str:
    if value is None:
        return ""
    normalized = _normalize_timestamp(value)
    return normalized.isoformat().replace("+00:00", "Z") if normalized else ""


def _parse_bool(value: str | None) -> bool | None:
    text = (value or "").strip().lower()
    if text in {"true", "1", "yes", "y", "on"}:
        return True
    if text in {"false", "0", "no", "n", "off"}:
        return False
    return None


def _parse_positive_hours(value: str | None) -> float | None:
    try:
        parsed = float((value or "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_nonnegative_hours(value: str | None) -> float | None:
    try:
        parsed = float((value or "").strip())
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def resolve_cleanup_config(
    logger: logging.Logger | None = None,
    *,
    force_refresh: bool = True,
) -> CleanupConfig | None:
    logger = logger or log
    toggle = feature_flags.status(CONFIG_ENABLED)
    if toggle.get("invalid"):
        logger.warning(
            "cleanup not scheduled; required Feature Toggle %s has invalid value %r in %s",
            CONFIG_ENABLED,
            toggle.get("invalid_value"),
            toggle.get("source_tab") or "Feature Toggles",
        )
        return None
    if not toggle.get("present"):
        logger.warning(
            "cleanup not scheduled; missing Feature Toggle %s",
            CONFIG_ENABLED,
        )
        return None
    if not toggle.get("enabled"):
        logger.info("cleanup disabled by Feature Toggle %s=FALSE", CONFIG_ENABLED)
        return None

    raw = {
        key: recruitment.get_config_value(key, None, force=force_refresh)
        for key in REQUIRED_CONFIG_KEYS
    }
    missing = [key for key, value in raw.items() if value is None or not str(value).strip()]
    if missing:
        logger.warning("cleanup not scheduled; missing Config key(s): %s", ", ".join(missing))
        return None

    run_every = _parse_positive_hours(raw[CONFIG_RUN_EVERY_HOURS])
    dry_run = _parse_bool(raw[CONFIG_DRY_RUN])
    if run_every is None:
        logger.warning("cleanup not scheduled; invalid Config key %s", CONFIG_RUN_EVERY_HOURS)
        return None
    if dry_run is None:
        logger.warning("cleanup not scheduled; invalid Config key %s", CONFIG_DRY_RUN)
        return None
    config = CleanupConfig(
        True,
        str(raw[CONFIG_TAB]).strip(),
        run_every,
        dry_run,
        source=f"{recruitment.get_config_tab_name()}:Config",
    )
    logger.info(
        "cleanup config resolved: tab=%s run_every_hours=%s dry_run=%s source=%s",
        config.tab_name,
        f"{config.run_every_hours:g}",
        str(config.dry_run).lower(),
        config.source,
    )
    return config


def build_header_map(headers: Sequence[Any]) -> dict[str, int]:
    mapping = {str(header).strip().lower(): idx for idx, header in enumerate(headers) if str(header).strip()}
    missing = [header for header in REQUIRED_HEADERS if header not in mapping]
    if missing:
        raise ValueError(f"cleanup tab missing required header(s): {', '.join(missing)}")
    return mapping


def rows_from_values(values: Sequence[Sequence[Any]], header_map: Mapping[str, int]) -> list[CleanupRow]:
    rows: list[CleanupRow] = []
    for offset, raw_row in enumerate(values[1:], start=2):
        row_values = {
            header: (str(raw_row[idx]).strip() if idx < len(raw_row) and raw_row[idx] is not None else "")
            for header, idx in header_map.items()
        }
        if any(row_values.values()):
            rows.append(CleanupRow(sheet_row=offset, values=row_values))
    return rows


def _normalize_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _resolve_any(bot: commands.Bot, target_id: int) -> tuple[Any | None, str | None, str | None]:
    channel = bot.get_channel(target_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(target_id)
        except discord.NotFound:
            return None, None, "not_found"
        except discord.Forbidden:
            return None, None, "missing_permissions"
        except discord.HTTPException:
            return None, None, "fetch_failed"
    if isinstance(channel, discord.Thread):
        return channel, "thread", None
    if isinstance(channel, discord.TextChannel) or callable(getattr(channel, "history", None)):
        return channel, "channel", None
    return channel, None, "invalid_target_type"


def _normalize_prefixes(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        text = values.strip()
        return (text,) if text else ()
    if isinstance(values, Iterable):
        prefixes: list[str] = []
        for item in values:
            text = str(item).strip()
            if text:
                prefixes.append(text)
        return tuple(dict.fromkeys(prefixes))
    return ()


def _command_prefixes(bot: commands.Bot) -> tuple[str, ...]:
    prefix = getattr(bot, "command_prefix", None)
    prefixes: tuple[str, ...] = ()
    if isinstance(prefix, str) or isinstance(prefix, Iterable):
        prefixes = _normalize_prefixes(prefix)
    elif callable(prefix):
        try:
            maybe_prefixes = prefix(bot, None)
        except Exception:
            maybe_prefixes = None
        prefixes = _normalize_prefixes(maybe_prefixes)
    fallback = _normalize_prefixes(get_command_prefix())
    return prefixes or fallback


def _cleanup_bot_role_ids() -> set[int]:
    try:
        raw = recruitment.get_config_value(CONFIG_BOT_ROLE_IDS, "")
    except Exception:
        raw = ""
    role_ids: set[int] = set()
    for part in str(raw or "").split(","):
        text = part.strip()
        if not text:
            continue
        try:
            role_ids.add(int(text))
        except ValueError:
            continue
    return role_ids


def _author_has_cleanup_bot_role(author: Any, target: Any | None, role_ids: set[int]) -> bool:
    if not role_ids:
        return False
    member = author
    roles = getattr(member, "roles", None)
    if roles is None:
        guild = getattr(target, "guild", None) if target is not None else None
        get_member = getattr(guild, "get_member", None)
        if callable(get_member):
            try:
                member = get_member(getattr(author, "id", None))
            except Exception:
                member = None
            roles = getattr(member, "roles", None)
    if roles is None:
        return False
    return any(getattr(role, "id", None) in role_ids for role in roles)


def _is_webhook_message(message: discord.Message) -> bool:
    return getattr(message, "webhook_id", None) is not None


def _is_system_message(message: discord.Message) -> bool:
    message_type = getattr(message, "type", None)
    default_type = getattr(getattr(discord, "MessageType", None), "default", None)
    return message_type is not None and message_type != default_type


def _message_type_name(message: discord.Message) -> str:
    message_type = getattr(message, "type", None)
    name = getattr(message_type, "name", None)
    if name:
        return str(name)
    if message_type is None:
        return "none"
    return str(message_type).rsplit(".", 1)[-1]


def _has_automod_indicator(message: discord.Message) -> bool:
    for attr in (
        "automod_rule_id",
        "auto_moderation_rule_id",
        "automod_action",
        "auto_moderation_action",
        "automod_rule",
        "auto_moderation_rule",
    ):
        if getattr(message, attr, None) is not None:
            return True
    return False


def _has_automod_alert_phrase(message: discord.Message) -> bool:
    # Do not log or return message content; this is only a conservative local
    # fallback for discord.py versions without a dedicated AutoMod enum.
    text = " ".join(str(getattr(message, attr, "") or "") for attr in ("system_content", "content")).lower()
    return bool(text) and any(
        phrase in text
        for phrase in (
            "automod",
            "auto mod",
            "auto moderation",
            "automoderation",
            "blocked a message",
            "flagged by auto",
        )
    )


def _is_automod_system_message(message: discord.Message) -> bool:
    if not _is_system_message(message):
        return False
    message_type = getattr(message, "type", None)
    message_type_name = _message_type_name(message).lower()
    if message_type_name in {"auto_moderation_action", "automod_action", "auto_moderation_alert"}:
        return True
    message_type_enum = getattr(discord, "MessageType", None)
    for enum_name in ("auto_moderation_action", "automod_action", "auto_moderation_alert"):
        enum_value = getattr(message_type_enum, enum_name, None)
        if enum_value is not None and message_type == enum_value:
            return True
    default_type = getattr(message_type_enum, "default", None)
    author = getattr(message, "author", None)
    if message_type == default_type or getattr(author, "bot", False) is True or _is_webhook_message(message):
        return False
    return _has_automod_indicator(message) or _has_automod_alert_phrase(message)


def _is_cleanup_bot_message(message: discord.Message, bot: commands.Bot, target: Any | None = None) -> bool:
    author = getattr(message, "author", None)
    if author is None:
        return False
    bot_user = getattr(bot, "user", None)
    if bot_user is not None and getattr(author, "id", None) == getattr(bot_user, "id", None):
        return True
    if getattr(author, "bot", False) is True:
        return True
    try:
        return _author_has_cleanup_bot_role(author, target, _cleanup_bot_role_ids())
    except Exception:
        return False


def _is_cleanup_bot_or_webhook_message(message: discord.Message, bot: commands.Bot, target: Any | None = None) -> bool:
    return _is_cleanup_bot_message(message, bot, target) or _is_webhook_message(message)


def _is_cleanup_command_message(message: discord.Message, bot: commands.Bot) -> bool:
    content = getattr(message, "content", "") or ""
    if not content:
        return False
    return any(content.startswith(prefix) for prefix in _command_prefixes(bot))


def _matches_mode(message: discord.Message, mode: str, bot: commands.Bot, target: Any | None = None) -> bool:
    if getattr(message, "pinned", False):
        return False
    if mode == "all_non_pinned":
        return True
    if mode == "bot_messages_only":
        return _is_cleanup_bot_message(message, bot, target)
    if mode == "commands_only":
        return _is_cleanup_command_message(message, bot)
    if mode == "bot_messages_and_commands":
        return _is_cleanup_bot_message(message, bot, target) or _is_cleanup_command_message(message, bot)
    if mode == "bot_and_webhook_messages_only":
        return _is_cleanup_bot_or_webhook_message(message, bot, target)
    if mode == "bot_webhook_messages_and_commands":
        return _is_cleanup_bot_or_webhook_message(message, bot, target) or _is_cleanup_command_message(message, bot)
    if mode == "automod_system_messages_only":
        return _is_automod_system_message(message)
    if mode == "automod_system_and_webhook_messages_only":
        return _is_automod_system_message(message) or _is_webhook_message(message)
    return False


def _masked_prefix_values(prefixes: Sequence[str]) -> str:
    if not prefixes:
        return "-"
    values: list[str] = []
    for prefix in prefixes[:5]:
        if len(prefix) <= 1:
            values.append(prefix)
        else:
            values.append(f"{prefix[:1]}…({len(prefix)})")
    return ",".join(values)


def _format_message_type_counts(message_type_counts: Mapping[str, int]) -> str:
    if not message_type_counts:
        return "-"
    ranked = sorted(message_type_counts.items(), key=lambda item: (-item[1], item[0]))[:8]
    return ",".join(f"{name}:{count}" for name, count in ranked)


def _format_top_author_sample(author_counts: Mapping[str, _AuthorSourceCounts]) -> str:
    if not author_counts:
        return "-"
    parts: list[str] = []
    ranked = sorted(author_counts.items(), key=lambda item: (-item[1].count, item[0]))[:5]
    for author_id, counts in ranked:
        bot_flag = "true" if counts.bot_true_count else "false"
        parts.append(f"{author_id}:{counts.count}:{bot_flag}:{counts.webhook_count}")
    return ",".join(parts)


async def _delete_messages(messages: Sequence[discord.Message], logger: logging.Logger) -> CleanupResult:
    deleted = errors = 0
    for message in messages:
        try:
            try:
                await message.delete(reason="housekeeping cleanup")
            except TypeError as exc:
                error_text = str(exc)
                if "reason" not in error_text or "unexpected" not in error_text:
                    raise
                await message.delete()
        except discord.NotFound:
            continue
        except discord.Forbidden:
            errors += 1
            return CleanupResult("missing_permissions", deleted, len(messages), 0, errors)
        except discord.HTTPException as exc:
            logger.warning("cleanup delete failed: target_id=%s error=%s", getattr(getattr(message, "channel", None), "id", None), exc)
            errors += 1
        except Exception as exc:
            logger.warning(
                "cleanup delete failed unexpectedly: target_id=%s error_type=%s error=%s",
                getattr(getattr(message, "channel", None), "id", None),
                exc.__class__.__name__,
                _short_error(exc),
            )
            errors += 1
        else:
            deleted += 1
    if errors and deleted:
        return CleanupResult("partial_delete_failed", deleted, len(messages), 0, errors)
    if errors:
        return CleanupResult("delete_failed", deleted, len(messages), 0, errors)
    return CleanupResult("deleted" if deleted else "ok_no_matches", deleted, len(messages), 0, 0)


async def _scan_message_history(target: Any, *, min_age_hours: float, mode: str, dry_run: bool, bot: commands.Bot, logger: logging.Logger, context: dict[str, Any] | None = None) -> CleanupResult:
    candidates: list[discord.Message] = []
    skipped = 0
    cutoff = _utc_now() - timedelta(hours=min_age_hours)
    prefixes = _command_prefixes(bot)
    try:
        if context is not None:
            context["stage"] = "scan_history"
        total_seen = 0
        bot_author_seen = 0
        command_seen = 0
        author_counts: dict[str, _AuthorSourceCounts] = {}
        author_bot_true_count = 0
        own_bot_author_count = 0
        webhook_message_count = 0
        system_message_count = 0
        automod_system_seen = 0
        message_type_counts: dict[str, int] = {}
        empty_content_count = 0
        messages_with_content_count = 0
        prefix_matched_count = 0
        role_matched_count = 0
        role_ids = _cleanup_bot_role_ids()
        bot_user = getattr(bot, "user", None)
        bot_user_id = getattr(bot_user, "id", None)
        async for message in target.history(limit=None, oldest_first=True):
            total_seen += 1
            author = getattr(message, "author", None)
            author_id = str(getattr(author, "id", "unknown"))
            author_bucket = author_counts.setdefault(author_id, _AuthorSourceCounts())
            author_bucket.count += 1
            author_bot = getattr(author, "bot", False) is True
            if author_bot:
                author_bot_true_count += 1
                author_bucket.bot_true_count += 1
            if bot_user_id is not None and getattr(author, "id", None) == bot_user_id:
                own_bot_author_count += 1
            message_type = _message_type_name(message)
            message_type_counts[message_type] = message_type_counts.get(message_type, 0) + 1
            webhook_message = _is_webhook_message(message)
            if webhook_message:
                webhook_message_count += 1
                author_bucket.webhook_count += 1
            if _is_system_message(message):
                system_message_count += 1
            if _is_automod_system_message(message):
                automod_system_seen += 1
            content = getattr(message, "content", "") or ""
            prefix_match = False
            if content:
                messages_with_content_count += 1
                prefix_match = any(content.startswith(prefix) for prefix in prefixes)
                if prefix_match:
                    prefix_matched_count += 1
            else:
                empty_content_count += 1
            try:
                role_matched = _author_has_cleanup_bot_role(author, target, role_ids)
            except Exception:
                role_matched = False
            if role_matched:
                role_matched_count += 1
            if _is_cleanup_bot_message(message, bot, target):
                bot_author_seen += 1
            if prefix_match:
                command_seen += 1
            created = _normalize_timestamp(getattr(message, "created_at", None))
            if getattr(message, "pinned", False) or created is None or created > cutoff or not _matches_mode(message, mode, bot, target):
                skipped += 1
                continue
            candidates.append(message)
    except discord.Forbidden:
        return CleanupResult("missing_permissions", 0, len(candidates), skipped, 1)
    except discord.HTTPException as exc:
        logger.warning("cleanup history fetch failed: target_id=%s error=%s", getattr(target, "id", None), exc)
        return CleanupResult("fetch_failed", 0, len(candidates), skipped, 1)
    if total_seen > 0 and not candidates:
        diagnostics = ZeroMatchDiagnostics(
            total_seen=total_seen,
            bot_author_seen=bot_author_seen,
            command_seen=command_seen,
            unique_author_count=len(author_counts),
            author_bot_true_count=author_bot_true_count,
            own_bot_author_count=own_bot_author_count,
            webhook_message_count=webhook_message_count,
            system_message_count=system_message_count,
            automod_system_seen=automod_system_seen,
            message_type_counts=_format_message_type_counts(message_type_counts),
            empty_content_count=empty_content_count,
            messages_with_content_count=messages_with_content_count,
            prefix_matched_count=prefix_matched_count,
            role_matched_count=role_matched_count,
            top_author_sample=_format_top_author_sample(author_counts),
        )
        logger.info(
            "cleanup row scan complete: row=%s target_id=%s mode=%s total_seen=%s candidates=0 skipped=%s bot_author_seen=%s command_seen=%s unique_author_count=%s author_bot_true_count=%s own_bot_author_count=%s webhook_message_count=%s system_message_count=%s automod_system_seen=%s message_type_counts=%s empty_content_count=%s messages_with_content_count=%s prefix_matched_count=%s prefix_count=%s prefix_values_used=%s role_matched_count=%s top_author_sample=%s",
            (context or {}).get("row"),
            getattr(target, "id", None),
            mode,
            diagnostics.total_seen,
            skipped,
            diagnostics.bot_author_seen,
            diagnostics.command_seen,
            diagnostics.unique_author_count,
            diagnostics.author_bot_true_count,
            diagnostics.own_bot_author_count,
            diagnostics.webhook_message_count,
            diagnostics.system_message_count,
            diagnostics.automod_system_seen,
            diagnostics.message_type_counts,
            diagnostics.empty_content_count,
            diagnostics.messages_with_content_count,
            diagnostics.prefix_matched_count,
            len(prefixes),
            _masked_prefix_values(prefixes),
            diagnostics.role_matched_count,
            diagnostics.top_author_sample,
        )
    if dry_run:
        return CleanupResult("dry_run_ok", 0, len(candidates), skipped, 0)
    if context is not None:
        context["stage"] = "delete_messages"
    result = await _delete_messages(candidates, logger)
    result.candidates = len(candidates)
    result.skipped = skipped
    return result


async def _scan_thread(thread: discord.Thread, *, min_age_hours: float, mode: str, dry_run: bool, bot: commands.Bot, logger: logging.Logger, context: dict[str, Any] | None = None) -> CleanupResult:
    return await _scan_message_history(thread, min_age_hours=min_age_hours, mode=mode, dry_run=dry_run, bot=bot, logger=logger, context=context)


async def _scan_channel(channel: Any, *, min_age_hours: float, mode: str, dry_run: bool, bot: commands.Bot, logger: logging.Logger, context: dict[str, Any] | None = None) -> CleanupResult:
    """Clean only the configured channel target's own message history.

    Child threads are not discovered or traversed automatically.
    """
    return await _scan_message_history(channel, min_age_hours=min_age_hours, mode=mode, dry_run=dry_run, bot=bot, logger=logger, context=context)


def _cell_name(row: int, col_zero: int) -> str:
    col = col_zero + 1
    letters = ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"


def _row_update(row: CleanupRow, header_map: Mapping[str, int], updates: Mapping[str, str]) -> dict[str, str]:
    safe_updates = {key: value for key, value in updates.items() if key in BOT_WRITABLE_HEADERS}
    return {_cell_name(row.sheet_row, header_map[key]): value for key, value in safe_updates.items() if key in header_map}


async def _flush_updates(worksheet: Any, updates: Mapping[str, str]) -> None:
    if not updates:
        return
    await async_core.a_to_thread_with_backoff(
        worksheet.batch_update,
        [{"range": cell, "values": [[value]]} for cell, value in updates.items()],
    )


async def run_cleanup(
    bot: commands.Bot,
    logger: logging.Logger | None = None,
    *,
    startup_validation: bool = False,
    writeback: bool = True,
) -> CleanupRunSummary:
    logger = logger or log
    if startup_validation and _CLEANUP_RUN_LOCK.locked():
        summary = CleanupRunSummary(writeback=writeback, dry_run=True, status="startup_validation_skipped", first_error="cleanup run already in progress stage=startup_validation")
        logger.info("cleanup startup validation skipped; another cleanup run is already in progress")
        return summary
    async with _CLEANUP_RUN_LOCK:
        return await _run_cleanup_locked(bot, logger, startup_validation=startup_validation, writeback=writeback)


async def _run_cleanup_locked(
    bot: commands.Bot,
    logger: logging.Logger | None = None,
    *,
    startup_validation: bool = False,
    writeback: bool = True,
) -> CleanupRunSummary:
    logger = logger or log
    stage = "resolve_config"
    context: dict[str, Any] = {"stage": stage, "target_id": None, "target_type": None, "row": None}
    summary = CleanupRunSummary(writeback=writeback)
    try:
        config = resolve_cleanup_config(logger)
        if config is None or not config.enabled:
            summary.status = "disabled"
            return summary

        effective_dry_run = config.dry_run or startup_validation
        summary.dry_run = effective_dry_run
        updates: dict[str, str] = {}
        try:
            stage = "get_worksheet"
            context["stage"] = stage
            worksheet = await async_core.aget_worksheet(recruitment.get_recruitment_sheet_id(), config.tab_name)
            stage = "read_values"
            context["stage"] = stage
            values = await async_core.a_to_thread_with_backoff(worksheet.get_all_values)
            if not values:
                raise ValueError("cleanup tab is empty")
            stage = "build_headers"
            context["stage"] = stage
            header_map = build_header_map(values[0])
            stage = "parse_rows"
            context["stage"] = stage
            rows = rows_from_values(values, header_map)
        except Exception as exc:
            summary.status = "sheet_unavailable_or_invalid"
            summary.errors += 1
            summary.first_error = f"{exc.__class__.__name__}: {_short_error(exc)} stage={context.get('stage')}"
            logger.warning(
                "cleanup sheet unavailable or invalid: error_type=%s error=%s stage=%s",
                exc.__class__.__name__,
                _short_error(exc),
                context.get("stage"),
                extra={"error_type": exc.__class__.__name__, "error": _short_error(exc), "stage": context.get("stage")},
            )
            return summary

        for row in rows:
            context.update({"row": row.sheet_row, "target_id": None, "target_type": None})
            now_text = _format_utc(_utc_now())
            base_update = {"last_checked_at_utc": now_text, "last_deleted_count": "0", "last_candidate_count": "0", "last_skipped_count": "0"}
            enabled = _parse_bool(row.values.get("enabled"))
            if enabled is None:
                summary.errors += 1
                updates.update(_row_update(row, header_map, base_update | {"last_status": "invalid_enabled"}))
                continue
            if not enabled:
                updates.update(_row_update(row, header_map, base_update | {"last_status": "disabled"}))
                continue
            summary.checked_rows += 1
            try:
                target_id = int(row.values.get("target_id", ""))
            except ValueError:
                summary.errors += 1
                updates.update(_row_update(row, header_map, base_update | {"last_status": "invalid_target_id"}))
                continue
            context["target_id"] = target_id
            explicit_type = row.values.get("target_type", "").strip().lower()
            if explicit_type and explicit_type not in ALLOWED_TARGET_TYPES:
                summary.errors += 1
                updates.update(_row_update(row, header_map, base_update | {"last_status": "invalid_target_type"}))
                continue
            mode = row.values.get("cleanup_mode", "").strip().lower()
            if mode not in ALLOWED_CLEANUP_MODES:
                summary.errors += 1
                updates.update(_row_update(row, header_map, base_update | {"last_status": "invalid_cleanup_mode"}))
                continue
            min_age = _parse_nonnegative_hours(row.values.get("min_age_hours"))
            if min_age is None:
                summary.errors += 1
                updates.update(_row_update(row, header_map, base_update | {"last_status": "invalid_min_age_hours"}))
                continue
            stage = "resolve_target"
            context["stage"] = stage
            target, detected_type, resolve_status = await _resolve_any(bot, target_id)
            context["target_type"] = detected_type or explicit_type or None
            if target is None or detected_type is None:
                summary.errors += 1
                updates.update(_row_update(row, header_map, base_update | {"last_status": resolve_status or "not_found"}))
                continue
            if explicit_type and explicit_type != detected_type:
                summary.errors += 1
                updates.update(_row_update(row, header_map, base_update | {"last_status": "target_type_mismatch"}))
                continue
            effective_type = explicit_type or detected_type
            context["target_type"] = effective_type
            name_updates = {"target_type": effective_type, "target_name": getattr(target, "name", "") or "", "parent_name": ""}
            if effective_type == "thread":
                parent = getattr(target, "parent", None)
                name_updates["parent_name"] = getattr(parent, "name", "") or ""
            if effective_type not in SUPPORTED_TARGET_TYPES:
                summary.errors += 1
                updates.update(_row_update(row, header_map, base_update | name_updates | {"last_status": "unsupported_target_type"}))
                continue
            if effective_type == "thread":
                result = await _scan_thread(target, min_age_hours=min_age, mode=mode, dry_run=effective_dry_run, bot=bot, logger=logger, context=context)
            else:
                result = await _scan_channel(target, min_age_hours=min_age, mode=mode, dry_run=effective_dry_run, bot=bot, logger=logger, context=context)
            summary.deleted += result.deleted
            summary.candidates += result.candidates
            summary.skipped += result.skipped
            summary.errors += result.errors
            updates.update(_row_update(row, header_map, base_update | name_updates | {
                "last_deleted_count": str(0 if effective_dry_run else result.deleted),
                "last_candidate_count": str(result.candidates),
                "last_skipped_count": str(result.skipped),
                "last_status": result.status,
            }))

        try:
            if writeback:
                stage = "sheet_writeback"
                context["stage"] = stage
                await _flush_updates(worksheet, updates)
        except Exception as exc:
            summary.errors += 1
            if not summary.first_error:
                summary.first_error = f"{exc.__class__.__name__}: {_short_error(exc)} stage={stage}"
            logger.warning(
                "cleanup sheet writeback failed: error_type=%s error=%s stage=%s",
                exc.__class__.__name__, _short_error(exc), stage,
                extra={"error_type": exc.__class__.__name__, "error": _short_error(exc), "stage": stage},
            )
        trigger = "startup_validation" if startup_validation else "scheduled_or_manual"
        writeback_label = str(writeback).lower()
        summary_text = f"cleanup run complete: trigger={trigger} checked_rows={summary.checked_rows} dry_run={str(effective_dry_run).lower()} writeback={writeback_label} deleted={summary.deleted} candidates={summary.candidates} skipped={summary.skipped} errors={summary.errors}"
        if summary.errors > 0 or summary.first_error:
            summary_text += f" status={summary.status}"
            first_error = _format_summary_error(summary)
            if first_error:
                summary_text += f" first_error={first_error}"
        logger.info(summary_text)
        try:
            stage = "send_summary_log"
            context["stage"] = stage
            await runtime_helpers.send_log_message(f"🧹 {summary_text}")
        except Exception as exc:
            summary.summary_notice_failed = True
            if not summary.first_error:
                summary.first_error = f"summary_notice_failed: {exc.__class__.__name__}: {_short_error(exc)}"
            logger.warning(
                "cleanup summary notice failed; cleanup completed: error_type=%s error=%s stage=%s",
                exc.__class__.__name__, _short_error(exc), stage,
                exc_info=True,
                extra={"error_type": exc.__class__.__name__, "error": _short_error(exc), "stage": stage},
            )
        return summary
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error_text = _short_error(exc)
        setattr(exc, "cleanup_stage", context.get("stage"))
        logger.exception(
            "cleanup run unexpected failure: error_type=%s error=%s stage=%s row=%s target_id=%s target_type=%s",
            exc.__class__.__name__, error_text, context.get("stage"), context.get("row"), context.get("target_id"), context.get("target_type"),
            extra={
                "error_type": exc.__class__.__name__,
                "error": error_text,
                "stage": context.get("stage"),
                "row": context.get("row"),
                "target_id": context.get("target_id"),
                "target_type": context.get("target_type"),
            },
        )
        raise


class CleanupCog(commands.Cog):
    """Admin commands for housekeeping cleanup."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @tier("admin")
    @help_metadata(function_group="operational", section="housekeeping", access_tier="admin")
    @commands.group(name="cleanup", invoke_without_command=True, help="Housekeeping cleanup admin commands.")
    @commands.guild_only()
    @admin_only()
    async def cleanup(self, ctx: commands.Context) -> None:
        await ctx.reply("Usage: `!cleanup run`", mention_author=False)

    @tier("admin")
    @help_metadata(function_group="operational", section="housekeeping", access_tier="admin")
    @cleanup.command(name="run", help="Run housekeeping cleanup immediately.")
    @commands.guild_only()
    @admin_only()
    async def cleanup_run(self, ctx: commands.Context) -> None:
        actor_id = getattr(getattr(ctx, "author", None), "id", None)
        channel_id = getattr(getattr(ctx, "channel", None), "id", None)
        log.info("cleanup manual run requested: actor=%s channel=%s", actor_id, channel_id)
        try:
            await runtime_helpers.send_log_message(
                f"🧹 cleanup manual run requested: actor={actor_id} channel={channel_id}"
            )
        except Exception:
            log.warning("cleanup manual run notice failed", exc_info=True)

        await ctx.reply("Cleanup run started.", mention_author=False)
        try:
            summary = await run_cleanup(ctx.bot, log, startup_validation=False, writeback=True)
            if summary is None:  # Compatibility for tests or external monkeypatches.
                summary = CleanupRunSummary()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_type = exc.__class__.__name__
            error_text = _short_error(exc)
            stage = getattr(exc, "cleanup_stage", None)
            log.exception(
                "cleanup manual run failed: error_type=%s error=%s",
                error_type,
                error_text,
                extra={
                    "error_type": error_type,
                    "error": error_text,
                    "actor": actor_id,
                    "channel": channel_id,
                    "stage": stage,
                },
            )
            try:
                await runtime_helpers.send_log_message(
                    f"🧹 cleanup manual run failed: {error_type}: {error_text}"
                )
            except Exception:
                log.warning("cleanup manual failure notice failed", exc_info=True)
            await ctx.reply(f"Cleanup run failed: {error_type}. See logs.", mention_author=False)
            return

        finished = _manual_finished_message(summary)
        try:
            await runtime_helpers.send_log_message(f"🧹 {finished}")
        except Exception:
            log.warning("cleanup manual completion notice failed", exc_info=True)
        await ctx.reply(finished, mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(CleanupCog(bot))


__all__ = [
    "BOT_WRITABLE_HEADERS", "CleanupConfig", "CleanupRunSummary", "REQUIRED_CONFIG_KEYS", "REQUIRED_HEADERS",
    "CleanupCog", "build_header_map", "resolve_cleanup_config", "rows_from_values", "run_cleanup", "setup", "_matches_mode", "_row_update",
]
