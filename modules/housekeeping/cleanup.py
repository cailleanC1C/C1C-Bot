from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Mapping, Sequence

import discord
from discord.ext import commands

from modules.common import feature_flags
from modules.common import runtime as runtime_helpers
from shared.sheets import async_core
from shared.sheets import recruitment

log = logging.getLogger("c1c.housekeeping.cleanup")

CONFIG_ENABLED = "HOUSEKEEPING_CLEANUP_ENABLED"
CONFIG_TAB = "HOUSEKEEPING_CLEANUP_TAB"
CONFIG_RUN_EVERY_HOURS = "HOUSEKEEPING_CLEANUP_RUN_EVERY_HOURS"
CONFIG_DRY_RUN = "HOUSEKEEPING_CLEANUP_DRY_RUN"
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
SUPPORTED_TARGET_TYPES = {"thread"}
ALLOWED_CLEANUP_MODES = {
    "all_non_pinned",
    "bot_messages_only",
    "commands_only",
    "bot_messages_and_commands",
}


@dataclass(frozen=True)
class CleanupConfig:
    enabled: bool
    tab_name: str
    run_every_hours: float
    dry_run: bool


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


def resolve_cleanup_config(logger: logging.Logger | None = None) -> CleanupConfig | None:
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

    raw = {key: recruitment.get_config_value(key, None) for key in REQUIRED_CONFIG_KEYS}
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
    return CleanupConfig(True, str(raw[CONFIG_TAB]).strip(), run_every, dry_run)


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
    if isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        return channel, "channel", None
    return channel, None, "invalid_target_type"


def _command_prefixes(bot: commands.Bot) -> tuple[str, ...]:
    prefix = getattr(bot, "command_prefix", None)
    if isinstance(prefix, str):
        return (prefix,)
    if isinstance(prefix, Iterable):
        return tuple(str(item) for item in prefix if str(item))
    return ()


def _matches_mode(message: discord.Message, mode: str, bot: commands.Bot) -> bool:
    if getattr(message, "pinned", False):
        return False
    author = getattr(message, "author", None)
    bot_user = getattr(bot, "user", None)
    is_bot_message = bool(bot_user is not None and getattr(author, "id", None) == getattr(bot_user, "id", None))
    content = getattr(message, "content", "") or ""
    is_command = any(content.startswith(prefix) for prefix in _command_prefixes(bot))
    if mode == "all_non_pinned":
        return True
    if mode == "bot_messages_only":
        return is_bot_message
    if mode == "commands_only":
        return is_command
    if mode == "bot_messages_and_commands":
        return is_bot_message or is_command
    return False


async def _delete_messages(messages: Sequence[discord.Message], logger: logging.Logger) -> CleanupResult:
    deleted = errors = 0
    for message in messages:
        try:
            await message.delete(reason="housekeeping cleanup")
        except discord.NotFound:
            continue
        except discord.Forbidden:
            errors += 1
            return CleanupResult("missing_permissions", deleted, len(messages), 0, errors)
        except discord.HTTPException as exc:
            logger.warning("cleanup delete failed: target_id=%s error=%s", getattr(getattr(message, "channel", None), "id", None), exc)
            errors += 1
        else:
            deleted += 1
    if errors and deleted:
        return CleanupResult("partial_delete_failed", deleted, len(messages), 0, errors)
    if errors:
        return CleanupResult("delete_failed", deleted, len(messages), 0, errors)
    return CleanupResult("deleted" if deleted else "ok_no_matches", deleted, len(messages), 0, 0)


async def _scan_thread(thread: discord.Thread, *, min_age_hours: float, mode: str, dry_run: bool, bot: commands.Bot, logger: logging.Logger) -> CleanupResult:
    candidates: list[discord.Message] = []
    skipped = 0
    cutoff = _utc_now() - timedelta(hours=min_age_hours)
    try:
        async for message in thread.history(limit=None, oldest_first=True):
            created = _normalize_timestamp(getattr(message, "created_at", None))
            if getattr(message, "pinned", False) or created is None or created > cutoff or not _matches_mode(message, mode, bot):
                skipped += 1
                continue
            candidates.append(message)
    except discord.Forbidden:
        return CleanupResult("missing_permissions", 0, len(candidates), skipped, 1)
    except discord.HTTPException as exc:
        logger.warning("cleanup history fetch failed: target_id=%s error=%s", getattr(thread, "id", None), exc)
        return CleanupResult("fetch_failed", 0, len(candidates), skipped, 1)
    if dry_run:
        return CleanupResult("dry_run_ok", 0, len(candidates), skipped, 0)
    result = await _delete_messages(candidates, logger)
    result.candidates = len(candidates)
    result.skipped = skipped
    return result


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
    await asyncio.to_thread(worksheet.batch_update, [{"range": cell, "values": [[value]]} for cell, value in updates.items()])


async def run_cleanup(bot: commands.Bot, logger: logging.Logger | None = None, *, startup_validation: bool = False) -> None:
    logger = logger or log
    config = resolve_cleanup_config(logger)
    if config is None or not config.enabled:
        return

    checked_rows = deleted_total = candidate_total = skipped_total = errors = 0
    updates: dict[str, str] = {}
    try:
        worksheet = await async_core.aget_worksheet(recruitment.get_recruitment_sheet_id(), config.tab_name)
        values = await asyncio.to_thread(worksheet.get_all_values)
        if not values:
            raise ValueError("cleanup tab is empty")
        header_map = build_header_map(values[0])
        rows = rows_from_values(values, header_map)
    except Exception as exc:
        logger.warning("cleanup sheet unavailable or invalid: %s", exc)
        return

    effective_dry_run = config.dry_run or startup_validation
    for row in rows:
        now_text = _format_utc(_utc_now())
        base_update = {"last_checked_at_utc": now_text, "last_deleted_count": "0", "last_candidate_count": "0", "last_skipped_count": "0"}
        enabled = _parse_bool(row.values.get("enabled"))
        if enabled is None:
            errors += 1
            updates.update(_row_update(row, header_map, base_update | {"last_status": "invalid_enabled"}))
            continue
        if not enabled:
            updates.update(_row_update(row, header_map, base_update | {"last_status": "disabled"}))
            continue
        checked_rows += 1
        try:
            target_id = int(row.values.get("target_id", ""))
        except ValueError:
            errors += 1
            updates.update(_row_update(row, header_map, base_update | {"last_status": "invalid_target_id"}))
            continue
        explicit_type = row.values.get("target_type", "").strip().lower()
        if explicit_type and explicit_type not in ALLOWED_TARGET_TYPES:
            errors += 1
            updates.update(_row_update(row, header_map, base_update | {"last_status": "invalid_target_type"}))
            continue
        mode = row.values.get("cleanup_mode", "").strip().lower()
        if mode not in ALLOWED_CLEANUP_MODES:
            errors += 1
            updates.update(_row_update(row, header_map, base_update | {"last_status": "invalid_cleanup_mode"}))
            continue
        min_age = _parse_nonnegative_hours(row.values.get("min_age_hours"))
        if min_age is None:
            errors += 1
            updates.update(_row_update(row, header_map, base_update | {"last_status": "invalid_min_age_hours"}))
            continue
        target, detected_type, resolve_status = await _resolve_any(bot, target_id)
        if target is None or detected_type is None:
            errors += 1
            updates.update(_row_update(row, header_map, base_update | {"last_status": resolve_status or "not_found"}))
            continue
        if explicit_type and explicit_type != detected_type:
            errors += 1
            updates.update(_row_update(row, header_map, base_update | {"last_status": "target_type_mismatch"}))
            continue
        effective_type = explicit_type or detected_type
        name_updates = {"target_type": effective_type, "target_name": getattr(target, "name", "") or "", "parent_name": ""}
        if effective_type == "thread":
            parent = getattr(target, "parent", None)
            name_updates["parent_name"] = getattr(parent, "name", "") or ""
        if effective_type not in SUPPORTED_TARGET_TYPES:
            errors += 1
            updates.update(_row_update(row, header_map, base_update | name_updates | {"last_status": "unsupported_target_type"}))
            continue
        result = await _scan_thread(target, min_age_hours=min_age, mode=mode, dry_run=effective_dry_run, bot=bot, logger=logger)
        deleted_total += result.deleted
        candidate_total += result.candidates
        skipped_total += result.skipped
        errors += result.errors
        updates.update(_row_update(row, header_map, base_update | name_updates | {
            "last_deleted_count": str(0 if effective_dry_run else result.deleted),
            "last_candidate_count": str(result.candidates),
            "last_skipped_count": str(result.skipped),
            "last_status": result.status,
        }))

    try:
        await _flush_updates(worksheet, updates)
    except Exception as exc:
        errors += 1
        logger.warning("cleanup sheet writeback failed: %s", exc)
    summary = f"cleanup run complete: checked_rows={checked_rows} dry_run={str(effective_dry_run).lower()} deleted={deleted_total} candidates={candidate_total} skipped={skipped_total} errors={errors}"
    logger.info(summary)
    await runtime_helpers.send_log_message(f"🧹 {summary}")


__all__ = [
    "BOT_WRITABLE_HEADERS", "CleanupConfig", "REQUIRED_CONFIG_KEYS", "REQUIRED_HEADERS",
    "build_header_map", "resolve_cleanup_config", "rows_from_values", "run_cleanup", "_matches_mode", "_row_update",
]
