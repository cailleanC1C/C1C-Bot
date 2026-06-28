from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, Mapping, Sequence

import discord
from discord.ext import commands

from modules.common import feature_flags
from modules.common import runtime as runtime_helpers
from shared.logfmt import channel_label
from shared.sheets import async_core
from shared.sheets import recruitment

log = logging.getLogger("c1c.housekeeping.keepalive")

CONFIG_ENABLED = "HOUSEKEEPING_KEEPALIVE_ENABLED"
CONFIG_TAB = "HOUSEKEEPING_KEEPALIVE_TAB"
CONFIG_DEFAULT_MESSAGE = "HOUSEKEEPING_KEEPALIVE_DEFAULT_MESSAGE"
CONFIG_STALE_AFTER_HOURS = "HOUSEKEEPING_KEEPALIVE_STALE_AFTER_HOURS"
CONFIG_RUN_EVERY_HOURS = "HOUSEKEEPING_KEEPALIVE_RUN_EVERY_HOURS"
REQUIRED_CONFIG_KEYS = (
    CONFIG_TAB,
    CONFIG_DEFAULT_MESSAGE,
    CONFIG_STALE_AFTER_HOURS,
    CONFIG_RUN_EVERY_HOURS,
)
REQUIRED_HEADERS = (
    "enabled",
    "target_id",
    "target_type",
    "target_name",
    "parent_name",
    "keepalive_message",
    "last_seen_at_utc",
    "last_keepalive_sent_at_utc",
    "last_status",
    "last_checked_at_utc",
    "notes",
)
BOT_WRITABLE_HEADERS = (
    "target_type",
    "target_name",
    "parent_name",
    "last_seen_at_utc",
    "last_keepalive_sent_at_utc",
    "last_status",
    "last_checked_at_utc",
)
ALLOWED_TARGET_TYPES = {"thread", "channel"}


@dataclass(frozen=True)
class KeepaliveConfig:
    enabled: bool
    tab_name: str
    default_message: str
    stale_after_hours: float
    run_every_hours: float


@dataclass
class KeepaliveRow:
    sheet_row: int
    values: dict[str, str]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _format_utc(value: datetime | None) -> str:
    if value is None:
        return ""
    return (
        _normalize_timestamp(value).isoformat().replace("+00:00", "Z")
        if _normalize_timestamp(value)
        else ""
    )


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
    if parsed <= 0:
        return None
    return parsed


def resolve_keepalive_config(
    logger: logging.Logger | None = None,
) -> KeepaliveConfig | None:
    """Resolve sheet-driven keepalive settings; enabled comes from Feature Toggles."""

    logger = logger or log
    toggle = feature_flags.status(CONFIG_ENABLED)
    if toggle.get("invalid"):
        logger.warning(
            "thread keepalive not scheduled; required Feature Toggle %s has invalid value %r in %s",
            CONFIG_ENABLED,
            toggle.get("invalid_value"),
            toggle.get("source_tab") or "Feature Toggles",
        )
        return None
    if not toggle.get("present"):
        logger.warning(
            "thread keepalive not scheduled; required Feature Toggle %s is missing from %s",
            CONFIG_ENABLED,
            toggle.get("source_tab") or "Feature Toggles",
        )
        return None
    if not toggle.get("enabled"):
        logger.info(
            "thread keepalive disabled by Feature Toggle %s=FALSE",
            CONFIG_ENABLED,
        )
        return None

    # Mirralith keepalive lives in the recruitment/Mirralith workbook Config tab,
    # so this intentionally uses the recruitment sheet Config helper only for
    # non-toggle settings.
    raw = {key: recruitment.get_config_value(key, None) for key in REQUIRED_CONFIG_KEYS}
    missing = [
        key for key, value in raw.items() if value is None or not str(value).strip()
    ]
    if missing:
        logger.warning(
            "thread keepalive config missing required Config key(s): %s",
            ", ".join(missing),
        )
        return None

    stale_after_hours = _parse_positive_hours(raw[CONFIG_STALE_AFTER_HOURS])
    run_every_hours = _parse_positive_hours(raw[CONFIG_RUN_EVERY_HOURS])
    if stale_after_hours is None:
        logger.warning(
            "thread keepalive config invalid: %s must be a positive number",
            CONFIG_STALE_AFTER_HOURS,
        )
        return None
    if run_every_hours is None:
        logger.warning(
            "thread keepalive config invalid: %s must be a positive number",
            CONFIG_RUN_EVERY_HOURS,
        )
        return None

    return KeepaliveConfig(
        enabled=True,
        tab_name=str(raw[CONFIG_TAB]).strip(),
        default_message=str(raw[CONFIG_DEFAULT_MESSAGE]).strip(),
        stale_after_hours=stale_after_hours,
        run_every_hours=run_every_hours,
    )


def build_header_map(headers: Sequence[Any]) -> dict[str, int]:
    mapping = {
        str(header).strip().lower(): idx
        for idx, header in enumerate(headers)
        if str(header).strip()
    }
    missing = [header for header in REQUIRED_HEADERS if header not in mapping]
    if missing:
        raise ValueError(
            f"keepalive tab missing required header(s): {', '.join(missing)}"
        )
    return mapping


def rows_from_values(
    values: Sequence[Sequence[Any]], header_map: Mapping[str, int]
) -> list[KeepaliveRow]:
    rows: list[KeepaliveRow] = []
    for offset, raw_row in enumerate(values[1:], start=2):
        row_values = {
            header: (
                str(raw_row[idx]).strip()
                if idx < len(raw_row) and raw_row[idx] is not None
                else ""
            )
            for header, idx in header_map.items()
        }
        if any(row_values.values()):
            rows.append(KeepaliveRow(sheet_row=offset, values=row_values))
    return rows


def select_keepalive_message(
    thread_message: str, parent_message: str, default_message: str
) -> str:
    for candidate in (thread_message, parent_message, default_message):
        text = (candidate or "").strip()
        if text:
            return text
    return ""


def parent_keepalive_message_for_thread(
    thread: Any, parent_messages: Mapping[int, str]
) -> str:
    parent = getattr(thread, "parent", None)
    parent_id = getattr(parent, "id", None)
    if parent_id is None:
        return ""
    try:
        return parent_messages.get(int(parent_id), "")
    except (TypeError, ValueError):
        return ""


def newest_last_seen(current: str, candidate: str) -> str:
    if not candidate:
        return current
    if not current:
        return candidate
    return max(current, candidate)


def _normalize_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _resolve_any(
    bot: commands.Bot, target_id: int
) -> tuple[Any | None, str | None, str | None]:
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
    if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        return channel, "channel", None
    return channel, None, "not_thread_or_channel"


async def _collect_channel_threads(
    channel: Any, logger: logging.Logger
) -> tuple[Dict[int, discord.Thread], int]:
    errors = 0
    threads: Dict[int, discord.Thread] = {}
    for thread in getattr(channel, "threads", []) or []:
        threads[thread.id] = thread

    async def _pull_archives(
        fetcher: Iterable[discord.Thread] | None, reason: str
    ) -> None:
        nonlocal errors
        if fetcher is None:
            return
        try:
            async for thread in fetcher:
                threads[thread.id] = thread
        except discord.Forbidden:
            label = channel_label(channel.guild, channel.id)
            logger.warning(
                "thread keepalive archived fetch forbidden: channel=%s", label
            )
            errors += 1
        except discord.HTTPException as exc:
            label = channel_label(channel.guild, channel.id)
            logger.warning(
                "thread keepalive archived fetch failed: channel=%s reason=%s error=%s",
                label,
                reason,
                exc,
            )
            errors += 1

    await _pull_archives(
        getattr(channel, "archived_threads", None)
        and channel.archived_threads(limit=None),
        "public",
    )
    private_fetcher = None
    if hasattr(channel, "archived_threads"):
        try:
            private_fetcher = channel.archived_threads(limit=None, private=True)
        except TypeError:
            private_fetcher = None
    if private_fetcher is None and hasattr(channel, "private_archived_threads"):
        private_fetcher = channel.private_archived_threads(limit=None)
    await _pull_archives(private_fetcher, "private")
    return threads, errors


async def _build_message_maps(
    rows: Sequence[KeepaliveRow], bot: commands.Bot
) -> tuple[dict[int, str], dict[int, str], dict[int, str]]:
    parent_messages: dict[int, str] = {}
    explicit_thread_messages: dict[int, str] = {}
    explicit_thread_keepalive_sent_at: dict[int, str] = {}
    for row in rows:
        if not _parse_bool(row.values.get("enabled")):
            continue
        try:
            target_id = int(row.values.get("target_id", ""))
        except ValueError:
            continue

        target_type = row.values.get("target_type", "").strip().lower()
        if target_type == "thread":
            explicit_thread_messages[target_id] = row.values.get(
                "keepalive_message", ""
            )
            explicit_thread_keepalive_sent_at[target_id] = row.values.get(
                "last_keepalive_sent_at_utc", ""
            )
            continue
        if target_type == "channel":
            parent_messages[target_id] = row.values.get("keepalive_message", "")
            continue
        if target_type:
            continue

        _target, detected_type, _resolve_status = await _resolve_any(bot, target_id)
        if detected_type == "thread":
            explicit_thread_messages[target_id] = row.values.get(
                "keepalive_message", ""
            )
            explicit_thread_keepalive_sent_at[target_id] = row.values.get(
                "last_keepalive_sent_at_utc", ""
            )
        elif detected_type == "channel":
            parent_messages[target_id] = row.values.get("keepalive_message", "")

    return parent_messages, explicit_thread_messages, explicit_thread_keepalive_sent_at


async def _get_bot_member(
    thread: discord.Thread, bot: commands.Bot
) -> tuple[discord.Member | None, int]:
    if thread.guild is None or bot.user is None:
        return None, 1
    member = thread.guild.get_member(bot.user.id)
    if member:
        return member, 0
    try:
        member = await thread.guild.fetch_member(bot.user.id)
    except (discord.Forbidden, discord.HTTPException):
        return None, 1
    return member, 0


async def _latest_message(
    thread: discord.Thread, logger: logging.Logger
) -> tuple[Any | None, datetime | None, int]:
    try:
        async for message in thread.history(limit=1):
            return message, _normalize_timestamp(message.created_at), 0
    except discord.Forbidden:
        logger.warning("thread keepalive history forbidden: thread_id=%s", thread.id)
        return None, None, 1
    except discord.HTTPException as exc:
        logger.warning(
            "thread keepalive history failed: thread_id=%s error=%s", thread.id, exc
        )
        return None, None, 1
    if getattr(thread, "created_at", None):
        return None, _normalize_timestamp(thread.created_at), 0
    return None, None, 0


def _is_bot_keepalive_message(
    message_obj: Any | None, *, bot: commands.Bot, keepalive_text: str
) -> bool:
    if message_obj is None or bot.user is None:
        return False
    author = getattr(message_obj, "author", None)
    if getattr(author, "id", None) != getattr(bot.user, "id", None):
        return False
    return (getattr(message_obj, "content", "") or "").strip() == keepalive_text.strip()


async def _delete_previous_keepalive_if_latest(
    thread: discord.Thread,
    latest_message: Any | None,
    *,
    bot: commands.Bot,
    keepalive_text: str,
    logger: logging.Logger,
) -> int:
    if not _is_bot_keepalive_message(
        latest_message, bot=bot, keepalive_text=keepalive_text
    ):
        return 0
    try:
        await latest_message.delete()
    except discord.Forbidden:
        logger.warning(
            "thread keepalive previous message delete forbidden: thread_id=%s message_id=%s",
            thread.id,
            getattr(latest_message, "id", None),
        )
        return 1
    except discord.HTTPException as exc:
        logger.warning(
            "thread keepalive previous message delete failed: thread_id=%s message_id=%s error=%s",
            thread.id,
            getattr(latest_message, "id", None),
            exc,
        )
        return 1
    return 0


async def _last_activity_at(
    thread: discord.Thread, logger: logging.Logger
) -> tuple[datetime | None, int]:
    _message, last_activity, errors = await _latest_message(thread, logger)
    return last_activity, errors


async def _ensure_unarchived(
    thread: discord.Thread, logger: logging.Logger
) -> str | None:
    if not getattr(thread, "archived", False):
        return None
    try:
        await thread.edit(archived=False)
    except discord.Forbidden:
        logger.warning(
            "thread keepalive missing permissions to unarchive: thread_id=%s", thread.id
        )
        return "missing_permissions"
    except discord.HTTPException as exc:
        logger.warning(
            "thread keepalive unarchive failed: thread_id=%s error=%s", thread.id, exc
        )
        return "archived_unarchive_failed"
    return None


async def _process_thread(
    thread: discord.Thread,
    *,
    stale_after_delta: timedelta,
    message: str,
    bot: commands.Bot,
    logger: logging.Logger,
) -> tuple[str, bool, str, int]:
    errors = 0
    if not message:
        logger.warning(
            "thread keepalive missing message config: thread_id=%s", thread.id
        )
        return "missing_message_config", False, "", errors + 1

    member, perm_errors = await _get_bot_member(thread, bot)
    errors += perm_errors
    if member is None:
        return "missing_permissions", False, "", errors
    perms = thread.permissions_for(member)
    if not (
        perms.read_message_history and perms.send_messages and perms.manage_threads
    ):
        return "missing_permissions", False, "", errors + 1

    _message, last_activity, history_errors = await _latest_message(thread, logger)
    errors += history_errors
    if last_activity is None:
        return "fetch_failed", False, "", errors
    if _utc_now() - last_activity < stale_after_delta:
        return "ok_not_stale", False, _format_utc(last_activity), errors

    unarchive_status = await _ensure_unarchived(thread, logger)
    if unarchive_status:
        return unarchive_status, False, _format_utc(last_activity), errors + 1
    if getattr(thread, "archived", False):
        return (
            "archived_unarchive_failed",
            False,
            _format_utc(last_activity),
            errors + 1,
        )

    latest_message, _latest_activity, latest_errors = await _latest_message(
        thread, logger
    )
    errors += latest_errors
    errors += await _delete_previous_keepalive_if_latest(
        thread, latest_message, bot=bot, keepalive_text=message, logger=logger
    )

    try:
        await thread.send(message)
    except discord.Forbidden:
        return "missing_permissions", False, _format_utc(last_activity), errors + 1
    except discord.HTTPException as exc:
        logger.warning(
            "thread keepalive send failed: thread_id=%s error=%s", thread.id, exc
        )
        return "send_failed", False, _format_utc(last_activity), errors + 1
    return "posted", True, _format_utc(last_activity), errors


def _cell_name(row: int, col_zero: int) -> str:
    col = col_zero + 1
    letters = ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"


def _row_update(
    row: KeepaliveRow, header_map: Mapping[str, int], updates: Mapping[str, str]
) -> dict[str, str]:
    safe_updates = {
        key: value for key, value in updates.items() if key in BOT_WRITABLE_HEADERS
    }
    return {
        _cell_name(row.sheet_row, header_map[key]): value
        for key, value in safe_updates.items()
        if key in header_map
    }


async def _flush_updates(worksheet: Any, updates: Mapping[str, str]) -> None:
    if not updates:
        return
    await asyncio.to_thread(
        worksheet.batch_update,
        [{"range": cell, "values": [[value]]} for cell, value in updates.items()],
    )


async def run_keepalive(
    bot: commands.Bot, logger: logging.Logger | None = None
) -> None:
    logger = logger or log
    config = resolve_keepalive_config(logger)
    if config is None or not config.enabled:
        return

    checked_rows = posted = errors = 0
    updates: dict[str, str] = {}
    stale_after_delta = timedelta(hours=config.stale_after_hours)

    try:
        worksheet = await async_core.aget_worksheet(
            recruitment.get_recruitment_sheet_id(), config.tab_name
        )
        values = await asyncio.to_thread(worksheet.get_all_values)
        if not values:
            raise ValueError("keepalive tab is empty")
        header_map = build_header_map(values[0])
        rows = rows_from_values(values, header_map)
    except Exception as exc:
        logger.warning("thread keepalive sheet unavailable or invalid: %s", exc)
        return

    (
        parent_messages,
        explicit_thread_messages,
        explicit_thread_keepalive_sent_at,
    ) = await _build_message_maps(rows, bot)

    seen_threads: set[int] = set()
    processed_threads: dict[int, tuple[str, bool, str, str]] = {}
    for row in rows:
        enabled = _parse_bool(row.values.get("enabled"))
        now_text = _format_utc(_utc_now())
        base_update = {"last_checked_at_utc": now_text}
        if not enabled:
            # Disabled rows are still timestamped so admins can tell the bot saw
            # the row without changing admin-owned cells or target metadata.
            updates.update(
                _row_update(row, header_map, base_update | {"last_status": "disabled"})
            )
            continue
        checked_rows += 1
        try:
            target_id = int(row.values.get("target_id", ""))
        except ValueError:
            errors += 1
            updates.update(
                _row_update(
                    row, header_map, base_update | {"last_status": "invalid_target_id"}
                )
            )
            continue

        explicit_type = row.values.get("target_type", "").strip().lower()
        if explicit_type and explicit_type not in ALLOWED_TARGET_TYPES:
            errors += 1
            updates.update(
                _row_update(
                    row,
                    header_map,
                    base_update | {"last_status": "invalid_target_type"},
                )
            )
            continue

        target, detected_type, resolve_status = await _resolve_any(bot, target_id)
        if target is None or detected_type is None:
            errors += 1
            updates.update(
                _row_update(
                    row,
                    header_map,
                    base_update | {"last_status": resolve_status or "not_found"},
                )
            )
            continue
        if explicit_type and explicit_type != detected_type:
            errors += 1
            updates.update(
                _row_update(
                    row,
                    header_map,
                    base_update | {"last_status": "target_type_mismatch"},
                )
            )
            continue

        effective_type = explicit_type or detected_type
        name_updates = {
            "target_type": effective_type,
            "target_name": getattr(target, "name", "") or "",
            "parent_name": "",
        }
        if effective_type == "thread":
            parent = getattr(target, "parent", None)
            name_updates["parent_name"] = getattr(parent, "name", "") or ""
            parent_message = parent_keepalive_message_for_thread(
                target, parent_messages
            )
            message = select_keepalive_message(
                row.values.get("keepalive_message", ""),
                parent_message,
                config.default_message,
            )
            was_processed = target.id in processed_threads
            if was_processed:
                status, did_post, last_seen, keepalive_sent_at = processed_threads[
                    target.id
                ]
                thread_errors = 0
            else:
                seen_threads.add(target.id)
                status, did_post, last_seen, thread_errors = await _process_thread(
                    target,
                    stale_after_delta=stale_after_delta,
                    message=message,
                    bot=bot,
                    logger=logger,
                )
                keepalive_sent_at = (
                    now_text
                    if did_post
                    else row.values.get("last_keepalive_sent_at_utc", "")
                )
                processed_threads[target.id] = (
                    status,
                    did_post,
                    last_seen,
                    keepalive_sent_at,
                )
            errors += thread_errors
            if did_post and not was_processed:
                posted += 1
            updates.update(
                _row_update(
                    row,
                    header_map,
                    base_update
                    | name_updates
                    | {
                        "last_seen_at_utc": last_seen,
                        "last_keepalive_sent_at_utc": keepalive_sent_at,
                        "last_status": status,
                    },
                )
            )
            continue

        channel_threads, channel_errors = await _collect_channel_threads(target, logger)
        errors += channel_errors
        row_status = "ok_not_stale"
        parent_message = row.values.get("keepalive_message", "")
        channel_last_seen = row.values.get("last_seen_at_utc", "")
        channel_keepalive_sent_at = row.values.get("last_keepalive_sent_at_utc", "")
        for thread in channel_threads.values():
            if thread.id in seen_threads:
                continue
            seen_threads.add(thread.id)
            thread_message = explicit_thread_messages.get(thread.id, "")
            message = select_keepalive_message(
                thread_message, parent_message, config.default_message
            )
            status, did_post, last_seen, thread_errors = await _process_thread(
                thread,
                stale_after_delta=stale_after_delta,
                message=message,
                bot=bot,
                logger=logger,
            )
            errors += thread_errors
            if did_post:
                posted += 1
            channel_last_seen = newest_last_seen(channel_last_seen, last_seen)
            if did_post:
                row_status = "posted"
                channel_keepalive_sent_at = now_text
            elif (
                status not in {"ok_not_stale", "posted"}
                and row_status == "ok_not_stale"
            ):
                row_status = status
            keepalive_sent_at = (
                channel_keepalive_sent_at
                if did_post
                else explicit_thread_keepalive_sent_at.get(
                    thread.id, channel_keepalive_sent_at
                )
            )
            processed_threads[thread.id] = (
                status,
                did_post,
                last_seen,
                keepalive_sent_at,
            )
        updates.update(
            _row_update(
                row,
                header_map,
                base_update
                | name_updates
                | {
                    "last_seen_at_utc": channel_last_seen,
                    "last_keepalive_sent_at_utc": channel_keepalive_sent_at,
                    "last_status": row_status,
                },
            )
        )

    try:
        await _flush_updates(worksheet, updates)
    except Exception as exc:
        errors += 1
        logger.warning("thread keepalive sheet writeback failed: %s", exc)

    summary = f"💙 Thread keepalive — checked_rows={checked_rows} • posted={posted} • stale_after={config.stale_after_hours:g}h • errors={errors}"
    logger.info(summary)
    await runtime_helpers.send_log_message(summary)


__all__ = [
    "BOT_WRITABLE_HEADERS",
    "KeepaliveConfig",
    "REQUIRED_CONFIG_KEYS",
    "REQUIRED_HEADERS",
    "build_header_map",
    "newest_last_seen",
    "parent_keepalive_message_for_thread",
    "_delete_previous_keepalive_if_latest",
    "resolve_keepalive_config",
    "rows_from_values",
    "run_keepalive",
    "select_keepalive_message",
    "_build_message_maps",
    "_row_update",
]
