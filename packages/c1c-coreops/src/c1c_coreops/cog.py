"""CoreOps shared cog and RBAC helpers."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import logging
import os
import re
import sys
import textwrap
import time
from importlib import import_module
from dataclasses import asdict, dataclass, replace
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
from types import ModuleType

import discord
from discord.ext import commands

from config.runtime import (
    get_bot_name,
    get_command_prefix,
    get_env_name,
    get_watchdog_check_sec,
    get_watchdog_disconnect_grace_sec,
    get_watchdog_stall_sec,
)
from shared import logfmt
from shared import socket_heartbeat as hb
from c1c_coreops.render import (
    ChecksheetEmbedData,
    ChecksheetSheetEntry,
    ChecksheetTabEntry,
    DigestEmbedData,
    DigestSheetEntry,
    DigestSheetsClientSummary,
    RefreshEmbedRow,
    build_config_embed,
    build_checksheet_tabs_embed,
    build_digest_embed,
    build_digest_line,
    build_env_embed,
    build_health_embed,
    build_refresh_embed,
)
from shared.cache import telemetry as cache_telemetry
from shared.cache.telemetry import get_snapshot as cache_get_snapshot
from shared.cache.telemetry import humanize_duration as cache_humanize_duration
from shared.cache.telemetry import list_buckets as cache_list_buckets
from c1c_coreops.help import (
    COREOPS_VERSION,
    HelpCommandInfo,
    HelpTier,
    HelpTierSection,
    build_coreops_footer,
    build_help_detail_embed,
    build_help_overview_embeds,
)
from c1c_coreops.helpers import help_metadata, tier
from shared.theme import colors
from shared.redaction import sanitize_embed, sanitize_log, sanitize_text
from shared.obs.events import (
    format_refresh_message,
    refresh_bucket_results,
    refresh_dedupe_key,
    refresh_deduper,
)
from shared.sheets.async_core import (
    aget_worksheet,
    aopen_by_key,
    acall_with_backoff,
    afetch_records,
)
from shared.sheets.recruitment import get_reports_tab_name
from modules.common.config_sheets import SHEET_TARGETS, SheetTarget

from c1c_coreops.config import CoreOpsSettings, load_coreops_settings, normalize_command_text
from c1c_coreops.prefix import detect_admin_bang_command
from c1c_coreops.rbac import (
    admin_only,
    can_view_admin,
    can_view_staff,
    guild_only_denied_msg,
    is_admin_member,
    is_staff_member,
    ops_only,
)

UTC = dt.timezone.utc

logger = logging.getLogger(__name__)


_HELP_DIAGNOSTICS_CACHE: Dict[tuple[str, int | None], float] = {}


def _reset_help_diagnostics_cache() -> None:
    _HELP_DIAGNOSTICS_CACHE.clear()

_NAME_CACHE_TTL_SEC = 600.0
_ID_PATTERN = re.compile(r"\d{5,}")
_ID_KEY_HINTS = ("ID", "ROLE", "CHANNEL", "THREAD", "GUILD")
_ENV_KEY_HINTS = (
    "SHEET",
    "SHEETS",
    "GSPREAD",
    "GOOGLE",
    "SERVICE",
    "RECRUIT",
    "ONBOARD",
    "WELCOME",
    "PROMO",
)
_SHEET_CONFIG_SOURCES: Tuple[Tuple[str, str], ...] = (
    ("Recruitment", "shared.sheets.recruitment"),
    ("Onboarding", "shared.sheets.onboarding"),
)
_sheet_cache_errors_logged: Set[str] = set()
_sheet_cache_load_errors_logged: Set[str] = set()
_digest_section_errors_logged: Set[str] = set()
_FIELD_CHAR_LIMIT = 900
_CODE_BLOCK_OVERHEAD = len("```\n\n```")
_FIELD_CHUNK_SOFT_LIMIT = 1500
_EMBED_TOTAL_CHAR_LIMIT = 6000
_EMBED_FIELD_LIMIT = 25
_MAX_EMBED_LENGTH = 4500
_ZERO_WIDTH_SPACE = "\u200b"
_DIGEST_SHEET_BUCKETS: Tuple[Tuple[str, str], ...] = (
    ("clans", "ClanInfo"),
    ("templates", "Templates"),
    ("clan_tags", "ClanTags"),
)
_TELEMETRY_REQUIRED_KEYS: Tuple[str, ...] = (
    "ttl_sec",
    "last_refresh_at",
    "next_refresh_at",
    "last_result",
    "last_error",
    "retries",
)
_TELEMETRY_FALLBACK_KEYS: Tuple[str, ...] = (
    "name",
    "available",
    "ttl_seconds",
    "ttl_sec",
    "ttl_human",
    "age_seconds",
    "age_sec",
    "age_human",
    "last_refresh_at",
    "next_refresh_at",
    "next_refresh_delta_seconds",
    "next_refresh_human",
    "last_result",
    "last_error",
    "retries",
    "last_latency_ms",
)
_telemetry_missing_fields_logged: Set[str] = set()

_GENERIC_ALIAS_COMMANDS: Tuple[str, ...] = (
    "checksheet",
    "config",
    "digest",
    "env",
    "health",
    "refresh",
    "refresh all",
    "refresh clansinfo",
    "reload",
)


_CONFIG_MODULE: ModuleType | None = None


def _ensure_config_module() -> ModuleType:
    """Import and cache the shared config module when needed."""

    global _CONFIG_MODULE
    if _CONFIG_MODULE is None:
        from shared import config as _config  # noqa: WPS433 (runtime import)

        _CONFIG_MODULE = _config
    return _CONFIG_MODULE


def get_allowed_guild_ids() -> Sequence[int]:
    """Return guild IDs allowed to use CoreOps commands."""

    return _ensure_config_module().get_allowed_guild_ids()


def get_config_snapshot() -> Mapping[str, object]:
    """Return a mapping of configuration values."""

    return _ensure_config_module().get_config_snapshot()


def get_feature_toggles() -> Mapping[str, object]:
    """Return the configured feature toggles."""

    return _ensure_config_module().get_feature_toggles()


def reload_config() -> None:
    """Reload configuration from the environment and Sheets."""

    _ensure_config_module().reload_config()


def redact_value(key: str, value: object) -> str:
    """Redact sensitive values for display."""

    return _ensure_config_module().redact_value(key, value)


def _canonical_cmd_key(cmd: commands.Command[Any, Any, Any]) -> str:
    """Return a normalized command key for allowlist lookups."""

    name = getattr(cmd, "qualified_name", None) or getattr(cmd, "name", "")
    if not isinstance(name, str):
        return ""
    return name.strip().lower()


_ADMIN_BANG_ALLOWLIST: Set[str] = {
    key.strip().lower()
    for key in os.getenv("COREOPS_ADMIN_BANG_ALLOWLIST", "").split(",")
    if key.strip()
}


def _is_bang_eligible(cmd: commands.Command[Any, Any, Any]) -> bool:
    """Return True if the command is eligible for bare bang usage."""

    key = _canonical_cmd_key(cmd)
    return bool(key) and key in _ADMIN_BANG_ALLOWLIST


@dataclass(frozen=True)
class _ChecksheetSheetTarget:
    label: str
    context: str
    sheet_id_key: str
    config_tab_key: str
    sheet_id: str
    config_tab: str


def _config_tab_key_for_target(target: SheetTarget) -> str:
    suffix = "SHEET_ID"
    if target.sheet_id_key.endswith(suffix):
        return f"{target.sheet_id_key[:-len(suffix)]}CONFIG_TAB"
    if target.context:
        return f"{target.context.upper()}_CONFIG_TAB"
    return "CONFIG_TAB"


def resolve_ops_log_channel_id(
    *, bot: commands.Bot | None = None, snapshot: Mapping[str, object] | None = None
) -> int | None:
    """Return the configured ops/log channel ID, if available."""

    mapping: Mapping[str, object] | None
    if isinstance(snapshot, Mapping):
        mapping = snapshot
    else:
        mapping = get_config_snapshot()

    candidate: object | None = None
    if mapping is not None:
        for key in (
            "ops_log_channel_id",
            "OPS_LOG_CHANNEL_ID",
            "log_channel_id",
            "LOG_CHANNEL_ID",
        ):
            value = mapping.get(key)
            if isinstance(value, str):
                trimmed = value.strip()
                if trimmed:
                    candidate = trimmed
                    break
            elif value is not None:
                candidate = value
                break

    if candidate is None and bot is not None:
        bot_config = getattr(bot, "config", None)
        if bot_config is not None:
            candidate = (
                getattr(bot_config, "ops_log_channel_id", None)
                or getattr(bot_config, "log_channel_id", None)
            )

    if isinstance(candidate, str):
        candidate = candidate.strip()

    try:
        return int(candidate)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _column_label(index: int) -> str:
    if index <= 0:
        return ""
    label = ""
    value = index
    while value > 0:
        value, rem = divmod(value - 1, 26)
        label = chr(65 + rem) + label
    return label



class _SheetsDiscoveryClient:
    async def get_worksheet(self, *, sheet_id: str, tab: str):
        return await aget_worksheet(sheet_id, tab)

    async def get_values(self, *, sheet_id: str, range_name: str):
        workbook = await aopen_by_key(sheet_id)
        params = {"majorDimension": "ROWS", "valueRenderOption": "FORMATTED_VALUE"}
        response = await acall_with_backoff(workbook.values_get, range_name, params=params)
        if isinstance(response, Mapping):
            values = response.get("values")
            if isinstance(values, Sequence):
                return list(values)
            return []
        if isinstance(response, Sequence) and not isinstance(response, (str, bytes)):
            return list(response)
        return []


_DISCOVERY_SHEETS_CLIENT = _SheetsDiscoveryClient()


@dataclass(frozen=True)
class _ConfigDiscoveryResult:
    tabs: list[str]
    header_names: tuple[str, str]
    preview_rows: list[list[str]]
    tab_map: Dict[str, str]


async def _discover_tabs_from_config(
    sheets_client,
    sheet_id: str,
    config_tab_name: str | None,
    *,
    debug: bool = False,
) -> _ConfigDiscoveryResult:
    """Returns ordered unique tab names for a sheet based on its Config worksheet.

    Rules:
      • Open worksheet named ``config_tab_name`` or fallback "Config".
      • Read up to 200 rows × 2 cols (A:B) only.
      • Detect header row within first 3 rows (case-insensitive) for KEY / VALUE.
        - If not found, assume A=KEY, B=VALUE.
      • Normalize KEY as upper(). If KEY endswith "_TAB" and VALUE non-empty,
        append VALUE.strip() to result (dedupe preserving order).
      • Fail soft: on any exception, return [] (tabs empty).
    """

    config_tab = config_tab_name or "Config"
    tab_range = f"{config_tab}!A1:B200"
    rows: Sequence[object] = []
    try:
        rows = await sheets_client.get_values(sheet_id=sheet_id, range_name=tab_range)
        logger.info("[checksheet] raw Config rows (%s): %d", tab_range, len(rows))
    except Exception as exc:
        logger.warning("[checksheet] failed to read Config range %s: %s", config_tab, exc)
        return _ConfigDiscoveryResult(
            tabs=[], header_names=("A", "B"), preview_rows=[], tab_map={}
        )

    if debug and rows:
        preview: list[str] = []
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
            for raw in rows[:5]:
                if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
                    preview.append(str(list(raw[:2])))
                else:
                    preview.append(str(raw))
        logger.info("[checksheet] Config preview %s: %s", sheet_id[-4:] if sheet_id else "----", preview)

    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        rows_seq: list[Sequence[object]] = []
    else:
        rows_seq = []
        for row in rows:
            if isinstance(row, Sequence) and not isinstance(row, (str, bytes)):
                rows_seq.append(list(row))
            else:
                rows_seq.append([row])

    preview_rows: list[list[str]] = []
    for raw_row in rows_seq[:5]:
        if not isinstance(raw_row, Sequence) or isinstance(raw_row, (str, bytes)):
            cells = []
        else:
            cells = list(raw_row)
        trimmed: list[str] = []
        for cell in cells[:2]:
            text = str(cell or "").strip()
            if len(text) > 40:
                text = f"{text[:37]}…"
            trimmed.append(text)
        preview_rows.append([sanitize_text(value) for value in trimmed])

    key_idx: int | None = None
    val_idx: int | None = None
    header_row_index: int = -1
    header_names: tuple[str, str] = ("A", "B")

    for r_i in range(min(3, len(rows_seq))):
        raw_row = rows_seq[r_i]
        if not isinstance(raw_row, Sequence) or isinstance(raw_row, (str, bytes)):
            cells: list[str] = []
        else:
            cells = [str(cell or "").strip() for cell in raw_row]
        lowered = [cell.lower() for cell in cells]
        if "key" in lowered and "value" in lowered:
            key_idx = lowered.index("key")
            val_idx = lowered.index("value")
            header_row_index = r_i
            key_name = cells[key_idx] if key_idx < len(cells) else "KEY"
            val_name = cells[val_idx] if val_idx < len(cells) else "VALUE"
            header_names = (
                str(key_name or "KEY").strip().upper() or "KEY",
                str(val_name or "VALUE").strip().upper() or "VALUE",
            )
            break

    if key_idx is None or val_idx is None:
        key_idx, val_idx = 0, 1

    tab_map: Dict[str, str] = {}
    discovered: list[str] = []
    seen: set[str] = set()
    max_index = max(key_idx, val_idx)
    for r_i, raw_row in enumerate(rows_seq):
        if header_row_index >= 0 and r_i <= header_row_index:
            continue
        if not isinstance(raw_row, Sequence) or isinstance(raw_row, (str, bytes)):
            cells = []
        else:
            cells = list(raw_row)
        if len(cells) <= max_index:
            cells.extend([""] * (max_index + 1 - len(cells)))
        key = str(cells[key_idx] or "").strip()
        val = str(cells[val_idx] or "").strip()
        if not key or not val:
            continue
        key_upper = key.upper()
        if key_upper.endswith("_TAB"):
            dedupe = val.lower()
            if not dedupe or dedupe in seen:
                continue
            seen.add(dedupe)
            cleaned_value = val.strip()
            if cleaned_value:
                tab_map.setdefault(key_upper, cleaned_value)
                discovered.append(cleaned_value)

    return _ConfigDiscoveryResult(
        tabs=discovered,
        header_names=header_names,
        preview_rows=preview_rows,
        tab_map=tab_map,
    )


@dataclass(frozen=True)
class _EnvEntry:
    key: str
    normalized: object
    display: str


@dataclass(frozen=True)
class _EnvRenderMeta:
    bot_name: str
    env: str
    version: str
    timestamp: dt.datetime


def _format_bucket_label(name: str) -> str:
    cleaned = name.replace("_", " ").strip()
    if not cleaned:
        return name
    return " ".join(part.capitalize() for part in cleaned.split())


def _chunk_lines(lines: Sequence[str], limit: int) -> List[str]:
    chunks: List[str] = []
    current: List[str] = []
    current_len = 0
    for line in lines:
        text = line.rstrip()
        additional = len(text) + (1 if current else 0)
        if current and current_len + additional > limit:
            chunks.append("\n".join(current))
            current = [text]
            current_len = len(text)
        else:
            current.append(text)
            current_len += additional
    if current:
        chunks.append("\n".join(current))
    return chunks or ["—"]


def _chunk_field_lines(
    label: str,
    lines: Iterable[str],
    *,
    soft_limit: int = _FIELD_CHAR_LIMIT,
) -> List[tuple[str, str]]:
    """Split a large list of lines into multiple field values."""

    return _chunk_field_values(label, lines, limit=soft_limit)


def _split_long_line(line: str, limit: int) -> List[str]:
    if line == "":
        return [""]
    if len(line) <= limit:
        return [line]
    return textwrap.wrap(
        line,
        width=limit,
        break_long_words=True,
        break_on_hyphens=False,
        replace_whitespace=False,
        drop_whitespace=False,
    )


def _chunk_field_values(
    label: str,
    lines: Iterable[str],
    *,
    limit: int = _FIELD_CHAR_LIMIT,
) -> List[tuple[str, str]]:
    body_limit = max(1, limit)
    normalized = [line if line is not None else "—" for line in lines] or ["—"]

    safe_lines: list[str] = []
    for line in normalized:
        safe_lines.extend(_split_long_line(line, body_limit))

    chunks = _chunk_lines(safe_lines, body_limit)
    total_chunks = len(chunks)
    fields: list[tuple[str, str]] = []
    for index, chunk in enumerate(chunks, start=1):
        name = label if total_chunks == 1 else f"{label} ({index}/{total_chunks})"
        value = chunk or "—"
        fields.append((name, value))
    return fields


def _wrap_code_block(text: str) -> str:
    return f"```\n{text}\n```"


def _embed_length(embed: discord.Embed) -> int:
    data = embed.to_dict()
    return len(json.dumps(data, ensure_ascii=False))


def _embed_char_count(embed: discord.Embed) -> int:
    total = 0
    if embed.title:
        total += len(embed.title)
    if embed.description:
        total += len(embed.description)
    if embed.footer and embed.footer.text:
        total += len(embed.footer.text)
    if embed.author and embed.author.name:
        total += len(embed.author.name)
    for field in embed.fields:
        total += len(field.name) + len(field.value)
    return total


def _clone_base_embed(embed: discord.Embed) -> discord.Embed:
    base_kwargs: dict[str, Any] = {
        "colour": embed.colour,
        "title": embed.title,
        "description": embed.description,
    }
    cloned = discord.Embed(**base_kwargs)
    cloned.timestamp = embed.timestamp
    if embed.author and embed.author.name:
        cloned.set_author(
            name=embed.author.name,
            icon_url=getattr(embed.author, "icon_url", discord.Embed.Empty),
        )
    return cloned


def _split_fields_into_embeds(
    base: discord.Embed,
    fields: Sequence[tuple[str, str]],
    *,
    max_chars: int = _EMBED_TOTAL_CHAR_LIMIT,
    max_fields: int = _EMBED_FIELD_LIMIT,
) -> list[discord.Embed]:
    if not fields:
        return [base]

    embeds: list[discord.Embed] = []
    current = _clone_base_embed(base)

    for name, value in fields:
        if (
            current.fields
            and (
                len(current.fields) >= max_fields
                or _embed_char_count(current) + len(name) + len(value) > max_chars
            )
        ):
            embeds.append(current)
            current = _clone_base_embed(base)
        current.add_field(name=name, value=value, inline=False)

    if current.fields:
        embeds.append(current)

    return embeds


def _split_embeds(embeds: list[discord.Embed]) -> list[discord.Embed]:
    """Ensure embeds stay within Discord limits by splitting fields as needed."""

    result: list[discord.Embed] = []
    for embed in embeds:
        if _embed_length(embed) <= _MAX_EMBED_LENGTH:
            result.append(embed)
            continue

        base_kwargs: dict[str, Any] = {
            "colour": embed.colour,
            "title": embed.title,
            "description": embed.description,
        }
        current = discord.Embed(**base_kwargs)
        current.timestamp = embed.timestamp
        if embed.footer and embed.footer.text:
            current.set_footer(text=embed.footer.text)
        if embed.author and embed.author.name:
            current.set_author(
                name=embed.author.name,
                icon_url=getattr(embed.author, "icon_url", discord.Embed.Empty),
            )

        for field in embed.fields:
            current.add_field(name=field.name, value=field.value, inline=field.inline)
            if _embed_length(current) > _MAX_EMBED_LENGTH:
                current.remove_field(len(current.fields) - 1)
                result.append(current)

                current = discord.Embed(**base_kwargs)
                current.timestamp = embed.timestamp
                if embed.footer and embed.footer.text:
                    current.set_footer(text=embed.footer.text)
                if embed.author and embed.author.name:
                    current.set_author(
                        name=embed.author.name,
                        icon_url=getattr(embed.author, "icon_url", discord.Embed.Empty),
                    )
                current.add_field(
                    name=field.name, value=field.value, inline=field.inline
                )

        if current.fields:
            result.append(current)

    return result


def _missing_value_text(entry: Optional[_EnvEntry]) -> str | None:
    if entry is None:
        return "—"
    text = str(entry.display or "").strip()
    if text in {"", "—"}:
        return "—"
    normalized = entry.normalized
    if normalized in (None, ""):
        return text or "—"
    return None


def _is_secret_key(key: str) -> bool:
    upper = key.upper()
    return (
        "TOKEN" in upper
        or "SECRET" in upper
        or "CREDENTIAL" in upper
        or "SERVICE_ACCOUNT" in upper
    )


def _is_sheet_key(key: str) -> bool:
    return key.endswith("_TAB") or "_SHEET_ID" in key


def _trim_resolved_label(label: str) -> str:
    if label.endswith(" (guild)"):
        label = label[:-8]
    if " · " in label:
        label = label.split(" · ", 1)[0]
    return label


def _short_identifier(value: object) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    if len(text) <= 6:
        return text
    return f"…{text[-4:]}"


def _mask_sheet_id(sheet_id: str) -> str:
    text = str(sheet_id or "").strip()
    if not text:
        return "—"
    return f"***{text[-4:]}"


def _format_sheet_log_label(sheet_title: str, sheet_id: str) -> str:
    title = (sheet_title or "Sheet").strip() or "Sheet"
    title = title.replace("\n", " ")
    trimmed_id = (sheet_id or "").strip()
    suffix = f"…{trimmed_id[-4:]}" if trimmed_id else "…----"
    return f"{title}({suffix})"


def _extract_override_keys(meta: Mapping[str, Any]) -> List[str]:
    overrides = meta.get("overrides")
    result: List[str] = []

    def _coerce_sequence(values: Iterable[object]) -> None:
        for item in values:
            text = str(item or "").strip()
            if text:
                result.append(text)

    if isinstance(overrides, Mapping):
        keys = overrides.get("keys")
        if isinstance(keys, Iterable) and not isinstance(keys, (str, bytes)):
            _coerce_sequence(keys)
        else:
            _coerce_sequence(overrides.keys())
    elif isinstance(overrides, (set, tuple, list)):
        _coerce_sequence(overrides)
    elif isinstance(overrides, str):
        text = overrides.strip()
        if text:
            result.append(text)

    if not result:
        extra_keys = meta.get("override_keys")
        if isinstance(extra_keys, (set, tuple, list)):
            _coerce_sequence(extra_keys)

    seen: Set[str] = set()
    unique: List[str] = []
    for item in result:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def _lookup_sheet_hint(meta: Mapping[str, Any] | None, slug: str) -> str | None:
    if not isinstance(meta, Mapping):
        return None

    keys = [slug, slug.lower(), slug.upper(), slug.capitalize()]

    def _extract(section: Mapping[str, Any]) -> str | None:
        for key in keys:
            if key not in section:
                continue
            value = section[key]
            if isinstance(value, Mapping):
                for candidate in ("title", "name", "label", "display"):
                    inner = value.get(candidate)
                    if isinstance(inner, str) and inner.strip():
                        return inner.strip()
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    for candidate_key in (
        "sheets",
        "sheet_titles",
        "sheet_labels",
        "sheet_names",
        "sheet_meta",
    ):
        section = meta.get(candidate_key)
        if isinstance(section, Mapping):
            found = _extract(section)
            if found:
                return found

    return _extract(meta)


def _admin_roles_configured() -> bool:
    """Return True when admin roles are configured (defaults to True)."""

    try:
        from c1c_coreops.rbac import admin_roles_configured  # type: ignore
    except Exception:
        return True
    try:
        return bool(admin_roles_configured())  # type: ignore[misc]
    except Exception:
        return True


def _get_tier(cmd: commands.Command[Any, Any, Any]) -> str:
    extras = getattr(cmd, "extras", None)
    if isinstance(extras, dict):
        level = extras.get("access_tier") or extras.get("tier")
    else:
        level = None
    return str(level) if level else getattr(cmd, "_tier", "user")


def _should_show(cmd: commands.Command[Any, Any, Any]) -> bool:
    # never show internals or the group container
    if cmd.qualified_name == "ops" or cmd.name.startswith("_"):
        return False

    # respect explicit opt-out flag in extras
    ex = getattr(cmd, "extras", None)
    if isinstance(ex, dict) and ex.get("hide_in_help"):
        return False

    # respect command.hidden only if command lacks a CoreOps/admin tier
    if getattr(cmd, "hidden", False):
        # find its tier (preserved via extras/_tier)
        tier = None
        ex = getattr(cmd, "extras", None)
        if isinstance(ex, dict):
            tier = ex.get("tier")
        tier = tier or getattr(cmd, "_tier", None)
        # hidden admin/staff commands stay visible to admins
        if tier not in {"admin", "staff"}:
            return False

    return True


def _admin_check() -> commands.Check[Any]:
    async def predicate(ctx: commands.Context) -> bool:
        if not _admin_roles_configured():
            # Let the command body display the explicit disabled message.
            return True
        return is_admin_member(getattr(ctx, "author", None))

    return commands.check(predicate)


def _staff_check() -> commands.Check[Any]:
    async def predicate(ctx: commands.Context) -> bool:
        author = getattr(ctx, "author", None)
        return bool(is_staff_member(author) or is_admin_member(author))

    return commands.check(predicate)


def _uptime_sec(bot: commands.Bot) -> float:
    started = getattr(bot, "_c1c_started_mono", None)
    return max(0.0, time.monotonic() - started) if started else 0.0


def _latency_sec(bot: commands.Bot) -> Optional[float]:
    try:
        return float(getattr(bot, "latency", None)) if bot.latency is not None else None
    except Exception:
        return None


def _delta_seconds(now: dt.datetime, value: dt.datetime | None) -> Optional[int]:
    if value is None:
        return None
    try:
        delta = value - now
        return int(delta.total_seconds())
    except Exception:
        return None
def _config_meta_from_app() -> dict:
    # Try to read CONFIG_META from app; else fallback
    app = sys.modules.get("app")
    meta = getattr(app, "CONFIG_META", None) if app else None
    return meta or {"source": "runtime-only", "status": "ok", "loaded_at": None, "last_error": None}


def _list_bucket_names() -> Set[str]:
    try:
        return set(cache_list_buckets())
    except Exception:
        return set()


def _coerce_snapshot_dict(name: str, snapshot: object) -> Dict[str, Any]:
    if isinstance(snapshot, Mapping):
        data = dict(snapshot)
    elif snapshot is None:
        data = {}
    else:
        try:
            data = asdict(snapshot)
        except Exception:
            data = {}
            for key in _TELEMETRY_FALLBACK_KEYS:
                if hasattr(snapshot, key):
                    data[key] = getattr(snapshot, key)

    missing = [field for field in _TELEMETRY_REQUIRED_KEYS if field not in data]
    if (not data or missing) and name not in _telemetry_missing_fields_logged:
        _telemetry_missing_fields_logged.add(name)
        logger.info("[telemetry] snapshot missing fields for bucket %s", name)
    return data


def _get_snapshot_dict(name: str) -> Dict[str, Any]:
    try:
        snapshot = cache_get_snapshot(name)
    except Exception:
        snapshot = None
    return _coerce_snapshot_dict(name, snapshot)


def _gather_snapshot_dicts(names: Iterable[str]) -> Dict[str, Dict[str, Any]]:
    snapshots: Dict[str, Dict[str, Any]] = {}
    for name in names:
        snapshots[name] = _get_snapshot_dict(name)
    return snapshots


def _extract_sheet_config_from_snapshot(snapshot: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(snapshot, Mapping):
        return {}

    candidates = (
        snapshot.get("config"),
        snapshot.get("overrides"),
        snapshot.get("value"),
        snapshot.get("data"),
        snapshot.get("payload"),
        snapshot.get("snapshot"),
    )

    for candidate in candidates:
        if isinstance(candidate, Mapping):
            return dict(candidate)
        if isinstance(candidate, str):
            text = candidate.strip()
            if not text:
                continue
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, Mapping):
                return dict(parsed)
    return {}


def _load_sheet_config_from_module(module_name: str) -> Dict[str, Any]:
    try:
        module = import_module(module_name)
    except Exception as exc:  # pragma: no cover - defensive logging
        if module_name not in _sheet_cache_errors_logged:
            _sheet_cache_errors_logged.add(module_name)
            msg, extra = sanitize_log(
                "failed to import sheet config cache",
                extra={"module": module_name},
            )
            logger.warning(msg, extra=extra, exc_info=exc)
        return {}

    loader = getattr(module, "_load_config", None)
    if not callable(loader):
        return {}

    try:
        data = loader()
    except Exception as exc:  # pragma: no cover - defensive logging
        if module_name not in _sheet_cache_load_errors_logged:
            _sheet_cache_load_errors_logged.add(module_name)
            msg, extra = sanitize_log(
                "failed to load sheet config cache",
                extra={"module": module_name},
            )
            logger.warning(msg, extra=extra, exc_info=exc)
        return {}

    if isinstance(data, Mapping):
        return dict(data)
    return {}


def _normalize_snapshot_value(value: object) -> object:
    if isinstance(value, set):
        try:
            return sorted(value)
        except TypeError:
            return list(value)
    if isinstance(value, tuple):
        return list(value)
    return value


def _clip(text: str, limit: int) -> str:
    clean = re.sub(r"\s+", " ", str(text)).strip()
    if not clean:
        return "—"
    if len(clean) <= limit:
        return clean
    return f"{clean[: limit - 1]}…"


def _format_resolved(names: Sequence[str]) -> str:
    if not names:
        return "—"

    seen: List[str] = []
    for name in names:
        label = name or "(not found)"
        if label not in seen:
            seen.append(label)
    return ", ".join(seen) if seen else "—"


def _extract_ids(key: str, value: object) -> List[int]:
    key_upper = str(key).upper()
    if not any(hint in key_upper for hint in _ID_KEY_HINTS):
        return []

    result: List[int] = []
    seen: Set[int] = set()

    def _push(candidate: int) -> None:
        if candidate not in seen:
            seen.add(candidate)
            result.append(candidate)

    def _walk(item: object) -> None:
        if item is None:
            return
        if isinstance(item, bool):
            return
        if isinstance(item, int):
            if item >= 0:
                _push(int(item))
            return
        if isinstance(item, float) and item.is_integer():
            _push(int(item))
            return
        if isinstance(item, str):
            for match in _ID_PATTERN.findall(item):
                try:
                    _push(int(match))
                except (TypeError, ValueError):
                    continue
            return
        if isinstance(item, dict):
            for sub in item.values():
                _walk(sub)
            return
        if isinstance(item, (list, tuple, set)):
            for sub in item:
                _walk(sub)

    _walk(value)
    return result


def _candidate_env_keys(snapshot: Dict[str, Any]) -> List[str]:
    keys = {str(k) for k in snapshot.keys()}
    for key in os.environ.keys():
        if key in keys:
            continue
        if not key.isupper():
            continue
        if any(hint in key for hint in _ENV_KEY_HINTS):
            keys.add(key)
    return sorted(keys)


def _describe_role(role: discord.Role) -> str:
    name = getattr(role, "name", "role")
    guild = getattr(role, "guild", None)
    guild_name = getattr(guild, "name", None)
    if guild_name:
        return f"@{name} · {guild_name}"
    return f"@{name}"


def _describe_channel(channel: discord.abc.GuildChannel | discord.Thread) -> str:
    name = getattr(channel, "name", "channel")
    if isinstance(channel, (discord.TextChannel, discord.Thread)):
        base = f"#{name}"
    elif isinstance(channel, discord.VoiceChannel):
        base = f"🔊 {name}"
    elif getattr(discord, "StageChannel", None) and isinstance(channel, discord.StageChannel):  # type: ignore[attr-defined]
        base = f"🎙️ {name}"
    elif isinstance(channel, discord.CategoryChannel):
        base = f"📂 {name}"
    else:
        base = str(name)
    guild = getattr(channel, "guild", None)
    guild_name = getattr(guild, "name", None)
    if guild_name:
        return f"{base} · {guild_name}"
    return base


class _IdResolver:
    __slots__ = ("_cache", "_failures")

    def __init__(self) -> None:
        self._cache: Dict[int, Tuple[str, float]] = {}
        self._failures: Set[int] = set()

    def resolve_many(self, bot: commands.Bot, ids: Iterable[int]) -> List[str]:
        return [self.resolve(bot, snowflake) for snowflake in ids]

    def resolve(self, bot: commands.Bot, snowflake: int) -> str:
        now = time.monotonic()
        cached = self._cache.get(snowflake)
        if cached and cached[1] > now:
            return cached[0]

        try:
            resolved = self._lookup(bot, snowflake)
        except Exception as exc:  # pragma: no cover - defensive logging
            if snowflake not in self._failures:
                self._failures.add(snowflake)
                msg, extra = sanitize_log(
                    "failed to resolve discord id",
                    extra={"id": snowflake},
                )
                logger.warning(msg, extra=extra, exc_info=exc)
            resolved = "(not found)"

        self._cache[snowflake] = (resolved, now + _NAME_CACHE_TTL_SEC)
        return resolved

    def _lookup(self, bot: commands.Bot, snowflake: int) -> str:
        guild = bot.get_guild(snowflake)
        if guild is not None:
            return f"{guild.name} (guild)"

        channel = bot.get_channel(snowflake)
        if channel is not None:
            return _describe_channel(channel)

        for guild in getattr(bot, "guilds", []):
            role = guild.get_role(snowflake)
            if role is not None:
                return _describe_role(role)

            channel = guild.get_channel(snowflake)
            if channel is not None:
                return _describe_channel(channel)

            getter = getattr(guild, "get_thread", None)
            if callable(getter):
                thread = getter(snowflake)
                if thread is not None:
                    return _describe_channel(thread)

        return "(not found)"

@dataclass(frozen=True)
class _HelpSectionConfig:
    key: str
    label: str
    allowed_function_groups: tuple[str, ...] = ()


@dataclass(frozen=True)
class _HelpAudienceConfig:
    key: str
    title: str
    allowed_function_groups: tuple[str, ...]
    sections: tuple[_HelpSectionConfig, ...]


@dataclass
class _CommandAccessResult:
    can_run: bool
    reason: str | None = None


@dataclass
class _HelpDiagnosticsEntry:
    qualified_name: str
    access_tier: str
    function_group: str
    can_run: bool = False
    displayed: bool = False
    reason: str = "hidden"


@dataclass
class _HelpDiagnosticsReport:
    entries: tuple[_HelpDiagnosticsEntry, ...]
    total: int
    visible: int
    hidden: int


class _HelpDiagnosticsCollector:
    __slots__ = ("_entries", "_order")

    def __init__(self) -> None:
        self._entries: Dict[str, _HelpDiagnosticsEntry] = {}
        self._order: List[str] = []

    def register(self, command: commands.Command[Any, Any, Any]) -> None:
        qualified = getattr(command, "qualified_name", None)
        if not isinstance(qualified, str) or not qualified:
            return
        if qualified in self._entries:
            return
        access_tier = _get_tier(command)
        function_group = getattr(command, "function_group", None)
        if not isinstance(function_group, str) or not function_group.strip():
            extras = getattr(command, "extras", None)
            if isinstance(extras, dict):
                function_group = str(extras.get("function_group") or "").strip()
        normalized_group = function_group if function_group else "MISSING"
        entry = _HelpDiagnosticsEntry(
            qualified_name=qualified,
            access_tier=(access_tier.strip() or "MISSING"),
            function_group=normalized_group or "MISSING",
        )
        self._entries[qualified] = entry
        self._order.append(qualified)

    def entry(self, qualified: str) -> _HelpDiagnosticsEntry | None:
        return self._entries.get(qualified)

    def mark_can_run(self, qualified: str, result: _CommandAccessResult) -> None:
        entry = self.entry(qualified)
        if entry is None:
            return
        entry.can_run = result.can_run
        if not result.can_run and result.reason:
            entry.reason = result.reason

    def mark_filtered(self, qualified: str, reason: str) -> None:
        entry = self.entry(qualified)
        if entry is None:
            return
        entry.displayed = False
        entry.reason = reason

    def mark_not_displayed(self, qualified: str, reason: str | None = None) -> None:
        entry = self.entry(qualified)
        if entry is None:
            return
        entry.displayed = False
        if reason:
            entry.reason = reason

    def mark_displayed(self, qualified: str) -> None:
        entry = self.entry(qualified)
        if entry is None:
            return
        entry.displayed = True
        entry.reason = "ok"

    def mark_manual_display(self, qualified: str) -> None:
        entry = self.entry(qualified)
        if entry is None:
            entry = _HelpDiagnosticsEntry(
                qualified_name=qualified,
                access_tier="MISSING",
                function_group="MISSING",
            )
            self._entries[qualified] = entry
            self._order.append(qualified)
        entry.can_run = True
        entry.displayed = True
        entry.reason = "ok"

    def build_report(self) -> _HelpDiagnosticsReport | None:
        if not self._entries:
            return None
        ordered = sorted(
            (
                self._entries[key]
                for key in self._order
                if key in self._entries
            ),
            key=lambda item: (not item.displayed, item.qualified_name),
        )
        total = len(ordered)
        visible = sum(1 for entry in ordered if entry.displayed)
        hidden = total - visible
        return _HelpDiagnosticsReport(entries=tuple(ordered), total=total, visible=visible, hidden=hidden)


class CoreOpsCog(commands.Cog):
    _HELP_AUDIENCE_CONFIGS: Dict[str, _HelpAudienceConfig] = {
        "admin": _HelpAudienceConfig(
            key="admin",
            title="Admin / Operational",
            allowed_function_groups=("operational",),
            sections=(
                _HelpSectionConfig("config_health", "Config & Health", ("operational",)),
                _HelpSectionConfig("sheets_cache", "Sheets & Cache", ("operational",)),
                _HelpSectionConfig("permissions", "Permissions", ("operational",)),
                _HelpSectionConfig("utilities", "Utilities", ("operational",)),
                _HelpSectionConfig("welcome_templates", "Welcome Templates", ("operational",)),
            ),
        ),
        "staff": _HelpAudienceConfig(
            key="staff",
            title="Staff",
            allowed_function_groups=("recruitment", "milestones", "operational"),
            sections=(
                _HelpSectionConfig("recruitment", "Recruitment", ("recruitment",)),
                _HelpSectionConfig("sheet_tools", "Sheet Tools", ("operational",)),
                _HelpSectionConfig("milestones", "Milestones", ("milestones",)),
            ),
        ),
        "user": _HelpAudienceConfig(
            key="user",
            title="User",
            allowed_function_groups=("recruitment", "milestones", "general"),
            sections=(
                _HelpSectionConfig("recruitment", "Recruitment", ("recruitment",)),
                _HelpSectionConfig("milestones", "Milestones", ("milestones",)),
                _HelpSectionConfig("general", "General", ("general",)),
            ),
        ),
    }

    def __init__(self, bot: commands.Bot):
        _ensure_config_module()
        self.bot = bot
        self._id_resolver = _IdResolver()
        self._settings: CoreOpsSettings = load_coreops_settings()
        self._admin_bang_allowlist: Set[str] = {
            entry.strip().lower() for entry in self._settings.admin_bang_allowlist
        }
        self._removed_generic_commands: tuple[str, ...] = tuple()
        self._tagged_aliases: tuple[str, ...] = tuple()
        self._help_diag_cache: Dict[tuple[str, int | None], float] = _HELP_DIAGNOSTICS_CACHE
        self._apply_tagged_alias_metadata()
        self._apply_generic_alias_policy()
        self._command_metadata_overrides = self._build_command_metadata_overrides()
        self._apply_command_metadata_overrides()

    async def cog_load(self) -> None:
        self._log_coreops_settings()
        self._rehydrate_command_metadata()

    def _apply_tagged_alias_metadata(self) -> None:
        command = getattr(self, "ops", None)
        if not isinstance(command, commands.Group):
            self._tagged_aliases = tuple()
            return
        aliases: list[str] = []
        tag = self._settings.bot_tag
        if self._settings.enable_tagged_aliases and tag and tag != command.name:
            aliases.append(tag)
        setattr(command, "aliases", aliases)
        setattr(command, "_coreops_aliases", tuple(aliases))
        self._tagged_aliases = tuple(aliases)

    def _apply_generic_alias_policy(self) -> None:
        commands_sequence = getattr(self, "__cog_commands__", None)
        if not isinstance(commands_sequence, (list, tuple)):
            return

        commands_list = list(commands_sequence)

        removed_generics: list[str] = []
        if not self._settings.enable_generic_aliases:
            retained: list[commands.Command[Any, Any, Any]] = []
            for command in commands_list:
                if command.qualified_name in _GENERIC_ALIAS_COMMANDS:
                    if _is_bang_eligible(command):
                        retained.append(command)
                        continue

                    removed_generics.append(command.qualified_name)
                    continue
                retained.append(command)
            commands_list = retained

        if removed_generics:
            self._removed_generic_commands = tuple(sorted({*removed_generics}))
        else:
            self._removed_generic_commands = tuple()
        setattr(self, "__cog_commands__", tuple(commands_list))

    def _build_command_metadata_overrides(self) -> dict[str, dict[str, str]]:
        return {
            "ops": {"function_group": "operational", "access_tier": "user"},
            "ops help": {"function_group": "operational", "access_tier": "user"},
            "health": {"function_group": "operational"},
            "ops health": {"function_group": "operational"},
            "checksheet": {"function_group": "operational", "access_tier": "admin"},
            "ops checksheet": {"function_group": "operational", "access_tier": "admin"},
            "digest": {"function_group": "operational"},
            "ops digest": {"function_group": "operational"},
            "env": {"function_group": "operational"},
            "ops env": {"function_group": "operational"},
            "config": {"function_group": "operational"},
            "ops config": {"function_group": "operational", "access_tier": "admin"},
            "ops ping": {"function_group": "operational"},
            "reload": {"function_group": "operational", "access_tier": "admin"},
            "ops reload": {"function_group": "operational"},
            "ops refresh": {"function_group": "operational", "access_tier": "admin"},
            "ops refresh all": {"function_group": "operational", "access_tier": "admin"},
            "ops refresh clansinfo": {"function_group": "operational", "access_tier": "staff"},
            "refresh": {"function_group": "operational"},
            "refresh all": {"function_group": "operational"},
            "refresh clansinfo": {"function_group": "operational"},
        }

    def _apply_command_metadata_overrides(
        self, *, commands_sequence: Iterable[commands.Command[Any, Any, Any]] | None = None
    ) -> None:
        if commands_sequence is None:
            commands_sequence = getattr(self, "__cog_commands__", None)
        if commands_sequence is None:
            return

        overrides = getattr(self, "_command_metadata_overrides", {})
        for command in commands_sequence:
            qualified_name = getattr(command, "qualified_name", None)
            if not isinstance(qualified_name, str):
                continue
            metadata = overrides.get(qualified_name)
            if not metadata:
                self._apply_metadata_attributes(command)
                continue

            extras = getattr(command, "extras", None)
            if not isinstance(extras, dict):
                extras = {}
                setattr(command, "extras", extras)

            for key, value in metadata.items():
                extras[key] = value
                try:
                    setattr(command, key, value)
                except Exception:
                    continue
            self._apply_metadata_attributes(command)

    def _rehydrate_command_metadata(self) -> None:
        commands_iterable = list(self.bot.walk_commands())
        self._apply_command_metadata_overrides(commands_sequence=commands_iterable)
        for command in commands_iterable:
            self._apply_metadata_attributes(command)

    def _apply_metadata_attributes(
        self, command: commands.Command[Any, Any, Any]
    ) -> None:
        extras = getattr(command, "extras", None)
        if not isinstance(extras, dict):
            return

        function_group = extras.get("function_group")
        if isinstance(function_group, str):
            try:
                setattr(command, "function_group", function_group)
            except Exception:
                pass

        access_tier = extras.get("access_tier") or extras.get("tier")
        if isinstance(access_tier, str):
            try:
                setattr(command, "access_tier", access_tier)
            except Exception:
                pass

    def _log_coreops_settings(self) -> None:
        description = self._settings.describe()
        if self._removed_generic_commands:
            description["generic_aliases_removed"] = list(self._removed_generic_commands)
        if self._tagged_aliases:
            description["tagged_aliases"] = list(self._tagged_aliases)
        msg, extra = sanitize_log(
            "coreops settings resolved",
            extra={"settings": description},
        )
        logger.info(msg, extra=extra)

    @commands.group(
        name="ops",
        invoke_without_command=True,
        help="Entry point for CoreOps operational commands and utilities.",
        brief="Entry point for CoreOps operational commands.",
    )
    @guild_only_denied_msg()
    async def ops(self, ctx: commands.Context) -> None:
        """Recruitment toolkit commands for the C1C cluster."""

        if ctx.invoked_subcommand is not None:
            return
        await ctx.send(
            str(
                sanitize_text(
                    "Use @Bot help, @Bot help <command>, or @Bot help <command> <subcommand>."
                )
            )
        )

    async def _health_impl(self, ctx: commands.Context) -> None:
        env = get_env_name()
        bot_name = get_bot_name()
        version = os.getenv("BOT_VERSION", "dev")
        uptime = _uptime_sec(self.bot)
        latency = _latency_sec(self.bot)
        last_age = await hb.age_seconds()
        keepalive = get_watchdog_check_sec()
        stall = get_watchdog_stall_sec()
        dgrace = get_watchdog_disconnect_grace_sec(stall)

        embed = build_health_embed(
            bot_name=bot_name,
            env=env,
            version=version,
            uptime_sec=uptime,
            latency_s=latency,
            last_event_age=last_age,
            keepalive_sec=keepalive,
            stall_after_sec=stall,
            disconnect_grace_sec=dgrace,
        )

        shard_status = "⚠️ shard tracker unavailable"
        try:
            from modules.community.shard_tracker import data as shard_data  # type: ignore

            shard_store = shard_data.ShardSheetStore()
        except Exception as exc:  # pragma: no cover - import errors rare
            reason = (str(exc).strip()) or exc.__class__.__name__
            shard_status = f"⚠️ shard tracker unavailable ({reason})"
        else:
            try:
                shard_config = await shard_store.get_config()
            except shard_data.ShardTrackerConfigError as exc:
                reason = (str(exc).strip()) or exc.__class__.__name__
                shard_status = f"⚠️ {reason}"
            except Exception as exc:  # pragma: no cover - defensive guard
                reason = (str(exc).strip()) or exc.__class__.__name__
                shard_status = f"⚠️ {reason}"
            else:
                shard_status = f"✅ <#{shard_config.channel_id}>"

        embed.add_field(name="Shard tracker", value=shard_status, inline=False)

        now = dt.datetime.now(UTC)

        bucket_names = _list_bucket_names()
        snapshot_cache: Dict[str, Dict[str, Any]] = {}

        def _snapshot_for(name: str) -> Dict[str, Any]:
            if name not in snapshot_cache:
                snapshot_cache[name] = _get_snapshot_dict(name)
            return snapshot_cache[name]

        for bucket in ("clans", "templates", "clan_tags", "onboarding_questions"):
            known_bucket = bucket in bucket_names
            snapshot = _snapshot_for(bucket)

            age_seconds = self._snapshot_int(snapshot, "age_seconds")
            if age_seconds is None:
                age_seconds = self._snapshot_int(snapshot, "age_sec")
            last_refresh_at = self._snapshot_datetime(snapshot, "last_refresh_at")
            if age_seconds is None and last_refresh_at is not None:
                try:
                    age_seconds = max(0, int((now - last_refresh_at).total_seconds()))
                except Exception:
                    age_seconds = None
            age_text = "-" if known_bucket else "n/a"
            if age_seconds is not None:
                human = cache_humanize_duration(age_seconds)
                if human != "n/a":
                    age_text = human

            ttl_seconds = self._snapshot_int(snapshot, "ttl_sec")
            if ttl_seconds is None:
                ttl_seconds = self._snapshot_int(snapshot, "ttl_seconds")
            ttl_text = "-" if known_bucket else "n/a"
            if ttl_seconds is not None:
                human_ttl = cache_humanize_duration(ttl_seconds)
                if human_ttl != "n/a":
                    ttl_text = human_ttl

            next_refresh_at = self._snapshot_datetime(snapshot, "next_refresh_at")
            next_delta = self._snapshot_int(snapshot, "next_refresh_delta_seconds")
            if next_delta is None and next_refresh_at is not None:
                try:
                    next_delta = int((next_refresh_at - now).total_seconds())
                except Exception:
                    next_delta = None

            next_text = "-" if known_bucket else "n/a"
            if next_delta is not None:
                human_next = cache_humanize_duration(abs(next_delta))
                if human_next != "n/a":
                    next_text = f"in {human_next}" if next_delta >= 0 else f"{human_next} overdue"

            embed.add_field(
                name=bucket,
                value=(
                    f"age: {age_text}, "
                    f"TTL: {ttl_text}, "
                    f"next: {next_text}"
                ),
                inline=False,
            )
        await ctx.reply(embed=sanitize_embed(embed))

    def _format_refresh_summary(
        self, result: cache_telemetry.RefreshResult
    ) -> tuple[str, bool]:
        snapshot = result.snapshot
        label = _format_bucket_label(result.name) or result.name

        parts: list[str] = []
        if result.ok:
            duration_ms = result.duration_ms if result.duration_ms is not None else 0
            parts.append(f"refreshed in {duration_ms} ms")
        else:
            error_text = (result.error or "unknown error").strip()
            if len(error_text) > 120:
                error_text = f"{error_text[:117]}…"
            parts.append(f"error: {error_text}")

        if snapshot.ttl_human is not None:
            parts.append(f"ttl {snapshot.ttl_human}")
        if snapshot.age_human is not None:
            parts.append(f"age {snapshot.age_human}")

        next_at = snapshot.next_refresh_at
        if next_at is not None:
            next_utc = next_at.astimezone(UTC)
            parts.append(f"next {next_utc:%H:%M} UTC")
        elif snapshot.next_refresh_delta_seconds is not None and snapshot.next_refresh_human:
            delta = snapshot.next_refresh_delta_seconds
            if delta >= 0:
                parts.append(f"next in {snapshot.next_refresh_human}")
            else:
                parts.append(f"next overdue by {snapshot.next_refresh_human}")

        if not parts:
            parts.append("no telemetry")

        return f"{label} — {' · '.join(parts)}", result.ok

    def _parse_reload_flags(self, flags: Sequence[str]) -> tuple[bool, Optional[str]]:
        reboot = False
        for flag in flags:
            if flag == "--reboot":
                reboot = True
                continue
            return reboot, flag
        return reboot, None

    async def _reload_impl(self, ctx: commands.Context, *, reboot: bool) -> None:
        actor = str(ctx.author)
        actor_display = getattr(ctx.author, "display_name", None) or actor
        actor_id = getattr(ctx.author, "id", None)
        action = "reboot" if reboot else "reload"

        start = time.monotonic()
        try:
            reload_config()
        except Exception as exc:  # pragma: no cover - defensive guard
            msg, extra = sanitize_log(
                f"{logfmt.LOG_EMOJI['lifecycle']} **CoreOps** — config reload failed",
                extra={
                    "actor": actor,
                    "actor_id": int(actor_id) if isinstance(actor_id, int) else actor_id,
                    "action": action,
                },
            )
            logger.exception(msg, extra=extra)
            error_text = (str(exc).strip()) or exc.__class__.__name__
            await ctx.send(str(sanitize_text(f"⚠️ {action} failed — {error_text}")))
            return

        duration_ms = int((time.monotonic() - start) * 1000)
        status = "graceful reboot scheduled" if reboot else "config reloaded"
        message = f"{status} · {duration_ms} ms · by {actor_display}"
        await ctx.send(str(sanitize_text(message)))

        log_msg, extra = sanitize_log(
            f"{logfmt.LOG_EMOJI['lifecycle']} **CoreOps** — config reload completed",
            extra={
                "actor": actor,
                "actor_id": int(actor_id) if isinstance(actor_id, int) else actor_id,
                "action": action,
                "duration_ms": duration_ms,
            },
        )
        logger.info(log_msg, extra=extra)

        async def _shutdown() -> None:
            await self.bot.close()

        if reboot:
            await _shutdown()
            await asyncio.sleep(1)

    async def _refresh_single_impl(
        self, ctx: commands.Context, bucket: str
    ) -> None:
        candidate = bucket.strip()
        if not candidate:
            await self._refresh_root(ctx)
            return

        buckets = cache_telemetry.list_buckets()
        if not buckets:
            await ctx.send(str(sanitize_text("⚠️ No cache buckets registered.")))
            return

        lookup = {name.lower(): name for name in buckets}
        target = lookup.get(candidate.lower())
        if target is None:
            available = ", ".join(buckets)
            await ctx.send(
                str(
                    sanitize_text(
                        f"⚠️ Unknown bucket `{candidate}`. Available: {available}"
                    )
                )
            )
            return

        actor_display = getattr(ctx.author, "display_name", None) or str(ctx.author)
        actor = str(ctx.author)
        actor_id = getattr(ctx.author, "id", None)

        try:
            result = await cache_telemetry.refresh_now(target, actor=actor)
        except asyncio.CancelledError:
            raise

        bucket_results = refresh_bucket_results([result])
        deduper = refresh_deduper()
        key = refresh_dedupe_key(target, None, [result.name or target])
        if deduper.should_emit(key):
            from modules.common import runtime as runtime_helpers  # local import to avoid cycles

            await runtime_helpers.send_log_message(
                format_refresh_message(
                    target,
                    bucket_results,
                    total_s=(result.duration_ms or 0) / 1000.0,
                )
            )

        summary, ok = self._format_refresh_summary(result)
        prefix = "•" if ok else "⚠"
        duration_ms = result.duration_ms if result.duration_ms is not None else 0
        header = f"cache refresh · {target} · {duration_ms} ms · by {actor_display}"
        message = "\n".join([header, f"{prefix} {summary}"])

        await self._send_refresh_response(
            ctx,
            scope=target,
            actor_display=actor_display,
            rows=[self._build_refresh_row(result)],
            total_duration=duration_ms,
            fallback_message=message,
        )

        log_msg, extra = sanitize_log(
            f"{logfmt.LOG_EMOJI['refresh']} **Cache refresh** — completed",
            extra={
                "actor": actor,
                "actor_id": int(actor_id) if isinstance(actor_id, int) else actor_id,
                "buckets": [target],
                "duration_ms": duration_ms,
                "failures": [] if ok else [target],
            },
        )
        logger.info(log_msg, extra=extra)

    @tier("admin")
    @help_metadata(function_group="operational", section="config_health", access_tier="admin")
    @ops.command(
        name="health",
        help="Checks the bot’s internal health status.",
        brief="Checks the bot’s internal health status.",
    )
    @ops_only()
    async def ops_health(self, ctx: commands.Context) -> None:
        await self._health_impl(ctx)

    @tier("admin")
    @help_metadata(function_group="operational", section="config_health", access_tier="admin")
    @commands.command(
        name="health",
        hidden=True,
        help="Checks the bot’s internal health status.",
        brief="Checks the bot’s internal health status.",
    )
    @guild_only_denied_msg()
    @admin_only()
    async def health(self, ctx: commands.Context) -> None:
        await self._health_impl(ctx)

    def _trim_error_text(self, text: str, *, limit: int = 120) -> str:
        cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
        cleaned = cleaned.replace("`", "ʼ")
        if not cleaned:
            return "n/a"
        if len(cleaned) > limit:
            return f"{cleaned[: limit - 1].rstrip()}…"
        return cleaned

    def _select_next_refresh_candidate(
        self, candidates: Sequence[Tuple[Optional[int], Optional[dt.datetime]]], now: dt.datetime
    ) -> Tuple[Optional[int], Optional[dt.datetime]]:
        future = [item for item in candidates if item[0] is not None and item[0] >= 0]
        if future:
            return min(future, key=lambda item: item[0] or 0)
        known = [item for item in candidates if item[0] is not None]
        if known:
            return min(known, key=lambda item: abs(item[0] or 0))
        for delta, at in candidates:
            if at is None:
                continue
            computed = _delta_seconds(now, at)
            return computed, at
        return None, None

    def _log_digest_section_error(self, section: str, exc: BaseException) -> None:
        if section in _digest_section_errors_logged:
            return
        _digest_section_errors_logged.add(section)
        msg, extra = sanitize_log("failed to collect digest section", extra={"section": section})
        logger.warning(msg, extra=extra, exc_info=exc)

    def _snapshot_value(self, snapshot: object, key: str, default: object = None) -> object:
        if snapshot is None:
            return default
        if isinstance(snapshot, Mapping):
            return snapshot.get(key, default)
        return getattr(snapshot, key, default)

    def _snapshot_datetime(self, snapshot: object, key: str) -> Optional[dt.datetime]:
        raw = self._snapshot_value(snapshot, key)
        if isinstance(raw, dt.datetime):
            return raw if raw.tzinfo else raw.replace(tzinfo=UTC)
        if isinstance(raw, str):
            text = raw.strip()
            if not text:
                return None
            if text.endswith("Z"):
                text = f"{text[:-1]}+00:00"
            try:
                parsed = dt.datetime.fromisoformat(text)
            except ValueError:
                return None
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        return None

    def _snapshot_int(self, snapshot: object, key: str) -> Optional[int]:
        raw = self._snapshot_value(snapshot, key)
        if isinstance(raw, bool):
            return int(raw)
        if isinstance(raw, (int, float)):
            try:
                return int(raw)
            except (TypeError, ValueError):
                return None
        return None

    def _snapshot_str(self, snapshot: object, key: str) -> Optional[str]:
        raw = self._snapshot_value(snapshot, key)
        if isinstance(raw, str):
            text = raw.strip()
            return text or None
        return None

    def _collect_sheet_bucket_entries(self) -> Sequence[DigestSheetEntry]:
        entries: List[DigestSheetEntry] = []
        available_names = set()
        available_names = _list_bucket_names()
        snapshot_cache = _gather_snapshot_dicts(available_names)
        now = dt.datetime.now(UTC)

        for bucket_key, display_name in _DIGEST_SHEET_BUCKETS:
            snapshot = snapshot_cache.get(bucket_key)
            if snapshot is None:
                snapshot = _get_snapshot_dict(bucket_key)

            available = False
            if bucket_key in available_names:
                available = bool(self._snapshot_value(snapshot, "available", True))
            else:
                available = bool(self._snapshot_value(snapshot, "available", False))

            age_seconds = self._snapshot_int(snapshot, "age_seconds") if available else None
            last_refresh_at = self._snapshot_datetime(snapshot, "last_refresh_at") if available else None
            if age_seconds is None and last_refresh_at is not None:
                try:
                    age_seconds = max(0, int((now - last_refresh_at).total_seconds()))
                except Exception:
                    age_seconds = None

            next_delta = self._snapshot_int(snapshot, "next_refresh_delta_seconds") if available else None
            next_refresh_at = self._snapshot_datetime(snapshot, "next_refresh_at") if available else None
            if next_delta is None and next_refresh_at is not None:
                try:
                    next_delta = int((next_refresh_at - now).total_seconds())
                except Exception:
                    next_delta = None

            retries = self._snapshot_int(snapshot, "retries")

            last_result = self._snapshot_str(snapshot, "last_result")
            last_error = self._snapshot_str(snapshot, "last_error")

            status = "n/a"
            result_norm = (last_result or "").strip().lower()
            has_error = bool(last_error) or (
                bool(result_norm) and result_norm not in {"ok", "retry_ok"}
            )

            if available:
                status = "fail" if has_error else "ok"
            elif has_error:
                status = "fail"

            error_text: Optional[str] = "—"
            if has_error:
                source = last_error or last_result or "fail"
                error_text = self._trim_error_text(source)

            if not has_error and not available:
                error_text = "—"

            if has_error and error_text and len(error_text) > 120:
                error_text = f"{error_text[:117]}…"

            entries.append(
                DigestSheetEntry(
                    display_name=display_name,
                    status=status,
                    age_seconds=age_seconds,
                    next_refresh_delta_seconds=next_delta,
                    next_refresh_at=next_refresh_at,
                    retries=retries if retries is not None else None,
                    error=error_text,
                    age_estimated=False,
                    next_refresh_estimated=False,
                )
            )

        return entries

    def _log_checksheet_issue(
        self,
        *,
        level: int,
        sheet_id: str,
        sheet_title: str,
        tab_name: Optional[str],
        detail: str,
        context: str,
    ) -> None:
        msg, extra = sanitize_log(
            "checksheet issue",
            extra={
                "sheet_id": sheet_id or "-",
                "sheet_title": sheet_title or "-",
                "tab": tab_name or "-",
                "context": context,
                "detail": detail,
            },
        )
        logger.log(level, msg, extra=extra)

    def _format_headers_preview(self, header_row: Sequence[object]) -> str:
        preview: list[str] = []
        for raw in list(header_row)[:12]:
            text = str(raw or "").strip()
            if not text:
                continue
            preview.append(text.replace("\n", " "))
        return ", ".join(preview) if preview else "—"

    def _format_config_headers_label(self, header_names: tuple[str, str] | None) -> str:
        if not header_names:
            return "n/a"
        cleaned: list[str] = []
        for name in header_names:
            text = str(name or "").strip()
            cleaned.append(text.upper() if text else "—")
        if not cleaned:
            return "n/a"
        return " / ".join(cleaned[:2])

    async def _determine_row_text(
        self,
        *,
        sheet_id: str,
        tab_name: str,
        worksheet,
    ) -> tuple[str, Optional[str]]:
        try:
            records = await afetch_records(sheet_id, tab_name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error_text = self._trim_error_text(exc)
            count = await self._fallback_row_count(worksheet)
            if count is not None:
                return str(count), error_text
            return "n/a", error_text

        if not isinstance(records, Sequence):
            return "0", None

        return str(len(records)), None

    async def _fallback_row_count(self, worksheet) -> Optional[int]:
        getter = getattr(worksheet, "get_row_count", None)
        if callable(getter):
            try:
                value = await acall_with_backoff(getter)
            except asyncio.CancelledError:
                raise
            except Exception:
                value = None
            else:
                if isinstance(value, int):
                    return value

        count_attr = getattr(worksheet, "row_count", None)
        if isinstance(count_attr, int):
            return count_attr
        return None

    async def _inspect_tab(
        self,
        *,
        sheet_id: str,
        sheet_title: str,
        tab_name: str,
    ) -> ChecksheetTabEntry:
        try:
            worksheet = await aget_worksheet(sheet_id, tab_name)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = self._trim_error_text(exc)
            self._log_checksheet_issue(
                level=logging.WARNING,
                sheet_id=sheet_id,
                sheet_title=sheet_title,
                tab_name=tab_name,
                detail=error,
                context="worksheet_open",
            )
            return ChecksheetTabEntry(
                name=tab_name,
                ok=False,
                rows="n/a",
                headers="—",
                error=error,
                first_headers=(),
            )

        try:
            header_row = await acall_with_backoff(worksheet.row_values, 1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = self._trim_error_text(exc)
            self._log_checksheet_issue(
                level=logging.WARNING,
                sheet_id=sheet_id,
                sheet_title=sheet_title,
                tab_name=tab_name,
                detail=error,
                context="headers",
            )
            return ChecksheetTabEntry(
                name=tab_name,
                ok=False,
                rows="n/a",
                headers="—",
                error=error,
                first_headers=(),
            )

        headers_preview = self._format_headers_preview(header_row)
        first_headers = [
            str(raw or "").strip()
            for raw in list(header_row)[:4]
            if str(raw or "").strip()
        ]
        rows_text, row_error = await self._determine_row_text(
            sheet_id=sheet_id,
            tab_name=tab_name,
            worksheet=worksheet,
        )
        if row_error:
            self._log_checksheet_issue(
                level=logging.INFO,
                sheet_id=sheet_id,
                sheet_title=sheet_title,
                tab_name=tab_name,
                detail=row_error,
                context="rows",
            )
        return ChecksheetTabEntry(
            name=tab_name,
            ok=True,
            rows=rows_text,
            headers=headers_preview,
            error=None,
            first_headers=tuple(first_headers),
        )

    async def _inspect_sheet(
        self, target: _ChecksheetSheetTarget, *, debug: bool = False
    ) -> ChecksheetSheetEntry:
        sheet_id = target.sheet_id
        config_tab = target.config_tab
        sheet_title = target.label
        warnings: list[str] = []
        tabs: list[ChecksheetTabEntry] = []

        config_tab_display = config_tab or "Config"
        display_sheet_id = _mask_sheet_id(sheet_id)

        if not sheet_id:
            warning = "missing sheet id"
            self._log_checksheet_issue(
                level=logging.WARNING,
                sheet_id="",
                sheet_title=sheet_title,
                tab_name=None,
                detail=warning,
                context="sheet_id",
            )
            warnings.append(warning)
            return ChecksheetSheetEntry(
                title=sheet_title,
                sheet_id="—",
                tabs=tuple(tabs),
                warnings=tuple(warnings),
                config_tab=config_tab_display,
                config_headers="n/a",
                config_preview_rows=(),
                discovered_tabs=(),
            )

        log_title = sanitize_text(sheet_title or "Sheet")
        log_last4 = sheet_id[-4:] if sheet_id else "----"
        log_config = sanitize_text(config_tab_display)
        logger.info(
            '[checksheet] sheet="%s"(…%s) using config_tab="%s"',
            log_title,
            log_last4 or "----",
            log_config,
        )

        discovery = await _discover_tabs_from_config(
            _DISCOVERY_SHEETS_CLIENT,
            sheet_id=sheet_id,
            config_tab_name=config_tab,
            debug=debug,
        )

        preview_json = json.dumps(discovery.preview_rows, ensure_ascii=False, separators=(",", ":"))
        logger.info("[checksheet] first_rows=%s", preview_json)

        config_headers_label = self._format_config_headers_label(discovery.header_names)
        tab_names = list(discovery.tabs)

        allow_config_fallback = target.context not in {"recruitment", "onboarding", "milestones"}
        sanitized_tabs_for_log = [sanitize_text(name) for name in tab_names]
        joined = ", ".join(sanitized_tabs_for_log)
        if len(joined) > 120:
            joined = f"{joined[:117]}…"

        if tab_names:
            logger.info(
                "[checksheet] discovered %d tabs: %s",
                len(tab_names),
                joined,
            )
        else:
            logger.info("[checksheet] no *_TAB rows found in Config")
            warning = f"⚠️ No tabs listed in '{config_tab_display}'"
            warnings.append(warning)

            if allow_config_fallback:
                tab_names.append(config_tab_display)

        if target.sheet_id_key == "RECRUITMENT_SHEET_ID":
            reports_config = discovery.tab_map.get("REPORTS_TAB")
            reports_default = get_reports_tab_name("Statistics")
            reports_tab = (reports_config or reports_default or "").strip()
            if reports_tab:
                normalized_reports = reports_tab.lower()
                if not any((name or "").strip().lower() == normalized_reports for name in tab_names):
                    tab_names.append(reports_tab)
                    logger.info(
                        "[checksheet] injecting reports tab '%s' for inspection",
                        sanitize_text(reports_tab),
                    )

        try:
            workbook = await aopen_by_key(sheet_id)
            title_candidate = getattr(workbook, "title", None)
            if isinstance(title_candidate, str) and title_candidate.strip():
                sheet_title = title_candidate.strip()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = self._trim_error_text(exc)
            self._log_checksheet_issue(
                level=logging.WARNING,
                sheet_id=sheet_id,
                sheet_title=sheet_title,
                tab_name=None,
                detail=error,
                context="open_sheet",
            )
            warnings.append(f"Failed to open sheet: {error}")
            return ChecksheetSheetEntry(
                title=sheet_title,
                sheet_id=display_sheet_id,
                tabs=tuple(tabs),
                warnings=tuple(warnings),
                config_tab=config_tab_display,
                config_headers=config_headers_label,
                config_preview_rows=tuple(tuple(row) for row in discovery.preview_rows),
                discovered_tabs=tuple(tab_names),
            )

        if not tab_names:
            sheet_label = _format_sheet_log_label(sheet_title, sheet_id)
            logger.info(
                "[checksheet] no *_TAB entries in Config for sheet %s",
                sheet_label,
            )
            warning = f"⚠️ No tabs listed in '{config_tab_display}'"
            if warning not in warnings:
                warnings.append(warning)
            return ChecksheetSheetEntry(
                title=sheet_title,
                sheet_id=display_sheet_id,
                tabs=tuple(tabs),
                warnings=tuple(warnings),
                config_tab=config_tab_display,
                config_headers=config_headers_label,
                config_preview_rows=tuple(tuple(row) for row in discovery.preview_rows),
                discovered_tabs=tuple(tab_names),
            )

        sheet_label = _format_sheet_log_label(sheet_title, sheet_id)
        logger.info(
            "[checksheet] discovered %d tabs from Config for sheet %s",
            len(tab_names),
            sheet_label,
        )

        for tab_name in tab_names:
            tab_entry = await self._inspect_tab(
                sheet_id=sheet_id,
                sheet_title=sheet_title,
                tab_name=tab_name,
            )
            tabs.append(tab_entry)

        return ChecksheetSheetEntry(
            title=sheet_title,
            sheet_id=display_sheet_id,
            tabs=tuple(tabs),
            warnings=tuple(warnings),
            config_tab=config_tab_display,
            config_headers=config_headers_label,
            config_preview_rows=tuple(tuple(row) for row in discovery.preview_rows),
            discovered_tabs=tuple(tab_names),
        )

    def _build_checksheet_targets(self) -> Sequence[_ChecksheetSheetTarget]:
        snapshot = get_config_snapshot()
        targets: list[_ChecksheetSheetTarget] = []
        for target in SHEET_TARGETS:
            sheet_key = target.sheet_id_key
            config_key = _config_tab_key_for_target(target)
            raw_id = snapshot.get(sheet_key)
            sheet_id = str(raw_id).strip() if isinstance(raw_id, str) else str(raw_id or "").strip()
            raw_config = snapshot.get(config_key)
            config_tab = (
                raw_config.strip() if isinstance(raw_config, str) and raw_config.strip() else "Config"
            )
            targets.append(
                _ChecksheetSheetTarget(
                    label=target.label,
                    context=target.context,
                    sheet_id_key=sheet_key,
                    config_tab_key=config_key,
                    sheet_id=sheet_id,
                    config_tab=config_tab,
                )
            )
        return targets

    def _has_debug_flag(self, ctx: commands.Context) -> bool:
        message = getattr(ctx, "message", None)
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            return False
        tokens = content.split()
        if len(tokens) <= 1:
            return False
        return any(token.lower() == "--debug" for token in tokens[1:])

    async def _checksheet_impl(self, ctx: commands.Context, *, debug: bool = False) -> None:
        bot_version = os.getenv("BOT_VERSION", "dev")
        targets = self._build_checksheet_targets()
        results: list[ChecksheetSheetEntry] = []

        for target in targets:
            try:
                result = await self._inspect_sheet(target, debug=debug)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error = self._trim_error_text(exc)
                self._log_checksheet_issue(
                    level=logging.ERROR,
                    sheet_id=target.sheet_id,
                    sheet_title=target.label,
                    tab_name=None,
                    detail=error,
                    context="sheet_unhandled",
                )
                results.append(
                    ChecksheetSheetEntry(
                        title=target.label,
                        sheet_id=target.sheet_id or "—",
                        tabs=(),
                        warnings=(f"Unexpected error: {error}",),
                        config_tab=target.config_tab or "Config",
                        config_headers=None,
                        config_preview_rows=(),
                        discovered_tabs=(),
                    )
                )
            else:
                results.append(result)

        embed = build_checksheet_tabs_embed(
            ChecksheetEmbedData(
                sheets=results,
                bot_version=bot_version,
                coreops_version=COREOPS_VERSION,
                debug=debug,
            )
        )

        await ctx.reply(embed=sanitize_embed(embed))

    @tier("admin")
    @help_metadata(function_group="operational", section="sheet_tools", access_tier="admin")
    @ops.command(
        name="checksheet",
        help="Shows loaded tabs and headers.",
        brief="Shows loaded tabs and headers.",
    )
    @guild_only_denied_msg()
    @ops_only()
    async def ops_checksheet(self, ctx: commands.Context) -> None:
        await self._checksheet_impl(ctx, debug=self._has_debug_flag(ctx))

    @tier("admin")
    @help_metadata(function_group="operational", section="sheet_tools", access_tier="admin")
    @commands.command(
        name="checksheet",
        hidden=True,
        help="Shows loaded tabs and headers.",
        brief="Shows loaded tabs and headers.",
    )
    @guild_only_denied_msg()
    @ops_only()
    async def checksheet(self, ctx: commands.Context) -> None:
        await self._checksheet_impl(ctx, debug=self._has_debug_flag(ctx))

    def _collect_sheets_client_summary(self, now: dt.datetime) -> Optional[DigestSheetsClientSummary]:
        bucket_names = list(_list_bucket_names())

        if not bucket_names:
            return None

        snapshot_cache = _gather_snapshot_dicts(bucket_names)
        latest_refresh_at: Optional[dt.datetime] = None
        latest_latency: Optional[int] = None
        latest_retries: Optional[int] = None
        latest_error: Optional[str] = None
        latest_result: Optional[str] = None
        failure_error: Optional[str] = None

        for name in bucket_names:
            snapshot = snapshot_cache.get(name)
            if snapshot is None:
                snapshot = _get_snapshot_dict(name)

            last_refresh = self._snapshot_datetime(snapshot, "last_refresh_at")
            if (
                isinstance(last_refresh, dt.datetime)
                and (latest_refresh_at is None or last_refresh > latest_refresh_at)
            ):
                latest_refresh_at = last_refresh
                latest_latency = self._snapshot_int(snapshot, "last_latency_ms")
                latest_retries = self._snapshot_int(snapshot, "retries")
                latest_result = self._snapshot_str(snapshot, "last_result")
                latest_error = self._snapshot_str(snapshot, "last_error")

            result_text = self._snapshot_str(snapshot, "last_result")
            result_norm = (result_text or "").strip().lower()
            if (
                result_norm
                and result_norm not in {"ok", "retry_ok"}
                and failure_error is None
            ):
                err_source = self._snapshot_str(snapshot, "last_error") or result_text
                if err_source:
                    failure_error = self._trim_error_text(err_source)

        last_success_age: Optional[int] = None
        if isinstance(latest_refresh_at, dt.datetime):
            try:
                last_success_age = max(0, int((now - latest_refresh_at).total_seconds()))
            except Exception:
                last_success_age = None

        summary_error = failure_error
        if summary_error is None and latest_error:
            summary_error = self._trim_error_text(latest_error)
        if summary_error is None and latest_result:
            summary_error = self._trim_error_text(latest_result)

        return DigestSheetsClientSummary(
            last_success_age=last_success_age,
            latency_ms=latest_latency,
            retries=latest_retries,
            last_error=summary_error,
        )

    async def _digest_impl(self, ctx: commands.Context) -> None:
        env = get_env_name()
        uptime = _uptime_sec(self.bot)
        latency = _latency_sec(self.bot)
        try:
            gateway_age = await hb.age_seconds()
        except Exception:
            gateway_age = None

        now = dt.datetime.now(UTC)
        uptime_seconds = int(uptime) if uptime is not None else None
        sheet_entries = self._collect_sheet_bucket_entries()
        sheets_summary = self._collect_sheets_client_summary(now)
        bot_version = os.getenv("BOT_VERSION", "dev")

        embed_data = DigestEmbedData(
            env=env,
            uptime_seconds=uptime_seconds,
            latency_seconds=latency,
            gateway_age_seconds=int(gateway_age) if gateway_age is not None else None,
            sheets=tuple(sheet_entries),
            sheets_client=sheets_summary,
            bot_version=bot_version,
        )

        fallback_line = build_digest_line(
            env=env,
            uptime_sec=uptime,
            latency_s=latency,
            last_event_age=gateway_age if isinstance(gateway_age, (int, float)) else None,
        )

        try:
            embed = build_digest_embed(data=embed_data)
        except Exception:
            msg, extra = sanitize_log("failed to build digest embed", extra={"command": "digest"})
            logger.exception(msg, extra=extra)
            await ctx.reply(str(sanitize_text(fallback_line)))
            return

        try:
            await ctx.reply(embed=sanitize_embed(embed))
        except Exception:
            msg, extra = sanitize_log("failed to send digest embed", extra={"command": "digest"})
            logger.exception(msg, extra=extra)
            await ctx.reply(str(sanitize_text(fallback_line)))

    @tier("staff")
    @help_metadata(function_group="operational", section="sheet_tools", access_tier="staff")
    @ops.command(
        name="digest",
        help="Displays a quick system summary.",
        brief="Displays a quick system summary.",
    )
    @ops_only()
    async def ops_digest(self, ctx: commands.Context) -> None:
        await self._digest_impl(ctx)

    @tier("admin")
    @help_metadata(function_group="operational", section="sheet_tools", access_tier="admin")
    @commands.command(
        name="digest",
        hidden=True,
        help="Displays a quick system summary.",
        brief="Displays a quick system summary.",
    )
    @guild_only_denied_msg()
    @admin_only()
    async def digest(self, ctx: commands.Context) -> None:
        await self._digest_impl(ctx)

    async def _env_overview_impl(self, ctx: commands.Context) -> None:
        bot_name = get_bot_name()
        env = get_env_name()
        version = os.getenv("BOT_VERSION", "dev")
        entries = self._collect_env_entries()
        sheet_sections = self._collect_sheet_sections()
        now = dt.datetime.now(UTC)
        render_meta = _EnvRenderMeta(
            bot_name=bot_name,
            env=env,
            version=version,
            timestamp=now,
        )
        embeds, warnings, warning_keys = self._build_env_embeds(
            entries=entries,
            sheet_sections=sheet_sections,
            render_meta=render_meta,
        )

        if not embeds:
            await ctx.reply("No environment data available.", mention_author=False)
            return

        if warning_keys:
            warning_summary = ",".join(sorted(warning_keys))
            logger.warning("⚠ env:missing_config keys=%s", warning_summary)

        await self._reply_with_env_embeds(
            ctx, [sanitize_embed(embed) for embed in embeds]
        )

    async def _env_section_impl(self, ctx: commands.Context, section: str) -> None:
        bot_name = get_bot_name()
        env = get_env_name()
        version = os.getenv("BOT_VERSION", "dev")
        entries = self._collect_env_entries()
        sheet_sections = self._collect_sheet_sections()
        now = dt.datetime.now(UTC)
        render_meta = _EnvRenderMeta(
            bot_name=bot_name,
            env=env,
            version=version,
            timestamp=now,
        )

        embeds = self._build_env_section_embeds(
            section=section,
            entries=entries,
            sheet_sections=sheet_sections,
            render_meta=render_meta,
        )

        if not embeds:
            await ctx.reply("No environment data available.", mention_author=False)
            return

        await self._reply_with_env_embeds(
            ctx, [sanitize_embed(embed) for embed in embeds]
        )

    async def _reply_with_env_embeds(
        self, ctx: commands.Context, embeds: Sequence[discord.Embed]
    ) -> None:
        if not embeds:
            return

        batches = [list(embeds[i : i + 10]) for i in range(0, len(embeds), 10)]
        for index, batch in enumerate(batches):
            try:
                if index == 0:
                    await ctx.reply(
                        embeds=batch,
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    await ctx.send(
                        embeds=batch,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
            except (TypeError, discord.HTTPException):
                for embed in batch:
                    if index == 0:
                        await ctx.reply(
                            embed=embed,
                            mention_author=False,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )
                        index = 1
                    else:
                        await ctx.send(
                            embed=embed,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )

    @tier("admin")
    @help_metadata(function_group="operational", section="config_health", access_tier="admin")
    @ops.group(
        name="env",
        invoke_without_command=True,
        help="Shows environment info for this bot.",
        brief="Shows environment info for this bot.",
    )
    @guild_only_denied_msg()
    @admin_only()
    async def ops_env(self, ctx: commands.Context) -> None:
        await self._env_overview_impl(ctx)

    @tier("admin")
    @help_metadata(function_group="operational", section="config_health", access_tier="admin")
    @ops_env.command(name="channels", help="Shows channel-related environment settings.")
    @admin_only()
    async def ops_env_channels(self, ctx: commands.Context) -> None:
        await self._env_section_impl(ctx, "channels")

    @tier("admin")
    @help_metadata(function_group="operational", section="config_health", access_tier="admin")
    @ops_env.command(name="roles", help="Shows role-related environment settings.")
    @admin_only()
    async def ops_env_roles(self, ctx: commands.Context) -> None:
        await self._env_section_impl(ctx, "roles")

    @tier("admin")
    @help_metadata(function_group="operational", section="config_health", access_tier="admin")
    @ops_env.command(name="sheets", help="Shows sheet-related environment settings.")
    @admin_only()
    async def ops_env_sheets(self, ctx: commands.Context) -> None:
        await self._env_section_impl(ctx, "sheets")

    @tier("admin")
    @help_metadata(function_group="operational", section="config_health", access_tier="admin")
    @ops_env.command(
        name="config",
        aliases=("runtime",),
        help="Shows runtime configuration settings.",
    )
    @admin_only()
    async def ops_env_config(self, ctx: commands.Context) -> None:
        await self._env_section_impl(ctx, "config")

    @tier("admin")
    @help_metadata(function_group="operational", section="config_health", access_tier="admin")
    @commands.group(
        name="env",
        invoke_without_command=True,
        hidden=True,
        help="Shows environment info for this bot.",
        brief="Shows environment info for this bot.",
    )
    @guild_only_denied_msg()
    @admin_only()
    async def env(self, ctx: commands.Context) -> None:
        await self._env_overview_impl(ctx)

    @tier("admin")
    @help_metadata(function_group="operational", section="config_health", access_tier="admin")
    @env.command(name="channels", hidden=True, help="Shows channel-related environment settings.")
    @admin_only()
    async def env_channels(self, ctx: commands.Context) -> None:
        await self._env_section_impl(ctx, "channels")

    @tier("admin")
    @help_metadata(function_group="operational", section="config_health", access_tier="admin")
    @env.command(name="roles", hidden=True, help="Shows role-related environment settings.")
    @admin_only()
    async def env_roles(self, ctx: commands.Context) -> None:
        await self._env_section_impl(ctx, "roles")

    @tier("admin")
    @help_metadata(function_group="operational", section="config_health", access_tier="admin")
    @env.command(name="sheets", hidden=True, help="Shows sheet-related environment settings.")
    @admin_only()
    async def env_sheets(self, ctx: commands.Context) -> None:
        await self._env_section_impl(ctx, "sheets")

    @tier("admin")
    @help_metadata(function_group="operational", section="config_health", access_tier="admin")
    @env.command(
        name="config",
        aliases=("runtime",),
        hidden=True,
        help="Shows runtime configuration settings.",
    )
    @admin_only()
    async def env_config(self, ctx: commands.Context) -> None:
        await self._env_section_impl(ctx, "config")

    def _build_env_base_embed(
        self,
        *,
        meta: _EnvRenderMeta,
        title: str,
        description: str | None = None,
    ) -> discord.Embed:
        cfg_meta = _config_meta_from_app()
        embed = build_env_embed(
            bot_name=meta.bot_name,
            env=meta.env,
            version=meta.version,
            cfg_meta=cfg_meta,
        )
        embed.clear_fields()
        embed.title = title
        if description is not None:
            embed.description = description
        embed.timestamp = meta.timestamp
        embed.set_footer(
            text=build_coreops_footer(
                bot_version=meta.version, notes=" • source: ENV + Sheet Config"
            )
        )
        return embed

    def render_env_embed(
        self,
        page_title: str,
        sections: Sequence[tuple[str, Sequence[str]]],
        footer_meta: _EnvRenderMeta,
        *,
        description: str | None = None,
    ) -> list[discord.Embed]:
        fields: list[tuple[str, str]] = []
        body_limit = max(1, _FIELD_CHAR_LIMIT - _CODE_BLOCK_OVERHEAD)
        for name, lines in sections:
            for field_name, field_value in _chunk_field_values(
                name, lines or ["—"], limit=body_limit
            ):
                fields.append((field_name, _wrap_code_block(field_value)))

        base = self._build_env_base_embed(
            meta=footer_meta,
            title=page_title,
            description=description,
        )
        embeds = _split_fields_into_embeds(base, fields)
        if len(embeds) > 1:
            total_pages = len(embeds)
            for page, embed in enumerate(embeds, start=1):
                embed.title = f"{page_title} · Page {page}/{total_pages}"
                embed.set_footer(
                    text=build_coreops_footer(
                        bot_version=footer_meta.version,
                        notes=" • source: ENV + Sheet Config",
                    )
                )
        return embeds

    def _build_env_embeds(
        self,
        *,
        entries: Dict[str, _EnvEntry],
        sheet_sections: List[Tuple[str, List[Tuple[str, str, str]]]],
        render_meta: _EnvRenderMeta,
    ) -> tuple[list[discord.Embed], list[str], set[str]]:
        warnings: list[str] = []
        warning_keys: set[str] = set()
        used_keys: set[str] = set()

        def _format_group(
            keys: Sequence[str], *, treat_ids: bool = True
        ) -> List[str]:
            lines: List[str] = []
            for key in keys:
                used_keys.add(key)
                entry = entries.get(key)
                lines.extend(
                    self._format_entry_lines(
                        key, entry, warnings, warning_keys, treat_ids=treat_ids
                    )
                )
            return lines

        overview_sections: list[tuple[str, list[str]]] = []
        overview_sections.append(
            (
                "Core Identity",
                _format_group(("BOT_NAME", "BOT_VERSION", "ENV_NAME"), treat_ids=False),
            )
        )
        toggles = get_feature_toggles()
        toggle_lines: list[str] = []
        if toggles:
            for name in sorted(toggles):
                value = "ON" if toggles[name] else "OFF"
                toggle_lines.extend(self._format_key_block(name, [(value, "value")]))
        else:
            toggle_lines.append("—")
        overview_sections.append(("Feature Flags", toggle_lines))

        channel_sections = {"Channels": [], "Threads": [], "Other": []}

        def _channel_bucket(key: str) -> str:
            if key == "PANEL_THREAD_MODE":
                return "Other"
            if "THREAD" in key:
                return "Threads"
            if "CHANNEL" in key or key == "GUILD_IDS":
                return "Channels"
            return "Other"

        ordered_channels = [
            "GUILD_IDS",
            "LOG_CHANNEL_ID",
            "WELCOME_CHANNEL_ID",
            "WELCOME_GENERAL_CHANNEL_ID",
            "NOTIFY_CHANNEL_ID",
            "PROMO_CHANNEL_ID",
            "RECRUITERS_CHANNEL_ID",
            "RECRUITERS_THREAD_ID",
            "REPORT_RECRUITERS_DEST_ID",
            "PANEL_FIXED_THREAD_ID",
            "PANEL_THREAD_MODE",
            "ROLEMAP_CHANNEL_ID",
            "SERVER_MAP_CHANNEL_ID",
        ]
        seen_channels: set[str] = set()
        for key in ordered_channels:
            seen_channels.add(key)
            bucket = _channel_bucket(key)
            channel_sections[bucket].extend(
                self._format_entry_lines(
                    key,
                    entries.get(key),
                    warnings,
                    warning_keys,
                    treat_ids=key != "PANEL_THREAD_MODE",
                )
            )
            used_keys.add(key)

        dynamic_channels = [
            key
            for key in entries.keys()
            if key not in seen_channels
            and not _is_secret_key(key)
            and any(token in key for token in ("CHANNEL", "THREAD", "GUILD"))
        ]
        for key in sorted(dynamic_channels):
            bucket = _channel_bucket(key)
            channel_sections[bucket].extend(
                self._format_entry_lines(
                    key,
                    entries.get(key),
                    warnings,
                    warning_keys,
                    treat_ids=key != "PANEL_THREAD_MODE",
                )
            )
            used_keys.add(key)

        role_sections: list[str] = []
        ordered_roles = [
            "ADMIN_ROLE_IDS",
            "STAFF_ROLE_IDS",
            "LEAD_ROLE_IDS",
            "RECRUITER_ROLE_IDS",
            "NOTIFY_PING_ROLE_ID",
        ]
        role_sections.extend(_format_group(ordered_roles))

        dynamic_roles = [
            key
            for key in entries.keys()
            if "ROLE" in key
            and key not in used_keys
            and not _is_secret_key(key)
            and not _is_sheet_key(key)
        ]
        for key in sorted(dynamic_roles):
            used_keys.add(key)
            role_sections.extend(
                self._format_entry_lines(key, entries.get(key), warnings, warning_keys)
            )

        sheets_lines: list[str] = []
        tab_lines: list[str] = []
        config_lines: list[str] = []

        for key, entry in entries.items():
            if key in used_keys or _is_secret_key(key):
                continue
            if _is_sheet_key(key):
                if key.endswith("_TAB"):
                    tab_lines.extend(
                        self._format_entry_lines(
                            key, entry, warnings, warning_keys, treat_ids=False
                        )
                    )
                else:
                    sheets_lines.extend(
                        self._format_entry_lines(
                            key, entry, warnings, warning_keys, treat_ids=False
                        )
                    )
                used_keys.add(key)
                continue

            config_lines.extend(
                self._format_entry_lines(
                    key, entry, warnings, warning_keys, treat_ids=False
                )
            )
            used_keys.add(key)

        if sheet_sections:
            for label, rows in sheet_sections:
                for row_key, value, resolved in rows:
                    value_text = f"{value}"
                    label = resolved if resolved and resolved != "—" else "value"
                    config_lines.extend(
                        self._format_key_block(
                            f"{label} override: {row_key}",
                            [(value_text, label)],
                        )
                    )

        meta = _config_meta_from_app()
        source = str(meta.get("source", "runtime"))
        status = str(meta.get("status", "ok"))
        loader_values = [(source, status)]
        loaded_at = meta.get("loaded_at")
        if loaded_at:
            loader_values.append((str(loaded_at), "loaded_at"))
        last_error = meta.get("last_error")
        if last_error:
            loader_values.append((str(last_error), "last_error"))
        config_lines.extend(self._format_key_block("Config Loader", loader_values))

        if warnings:
            warning_lines = warnings[:10]
            remaining = len(warnings) - len(warning_lines)
            if remaining > 0:
                warning_lines.append(
                    f"… and {remaining} more missing / not-found entries."
                )
            overview_sections.append(("Warnings", warning_lines))

        overview_title = f"{render_meta.bot_name} · env: {render_meta.env} · Overview"
        channels_title = f"{render_meta.bot_name} · env: {render_meta.env} · Channels"
        roles_title = f"{render_meta.bot_name} · env: {render_meta.env} · Roles"
        sheets_title = f"{render_meta.bot_name} · env: {render_meta.env} · Sheets & Config"

        overview_embeds = self.render_env_embed(
            overview_title, overview_sections, render_meta
        )
        channels_embeds = self.render_env_embed(
            channels_title,
            [
                ("Channels", channel_sections["Channels"] or ["—"]),
                ("Threads", channel_sections["Threads"] or ["—"]),
                ("Other", channel_sections["Other"] or ["—"]),
            ],
            render_meta,
        )
        roles_embeds = self.render_env_embed(
            roles_title, [("Roles", role_sections or ["—"])], render_meta
        )
        sheets_embeds = self.render_env_embed(
            sheets_title,
            [
                ("Sheets", sheets_lines or ["—"]),
                ("Tabs", tab_lines or ["—"]),
                ("Config", config_lines or ["—"]),
                ("Secrets (masked)", self._format_secrets(entries)),
            ],
            render_meta,
        )

        embeds = [*overview_embeds, *channels_embeds, *roles_embeds, *sheets_embeds]
        return embeds, warnings, warning_keys

    def _build_env_section_embeds(
        self,
        *,
        section: str,
        entries: Dict[str, _EnvEntry],
        sheet_sections: List[Tuple[str, List[Tuple[str, str, str]]]],
        render_meta: _EnvRenderMeta,
    ) -> list[discord.Embed]:
        normalized = section.strip().lower()
        section_title = normalized.capitalize()

        if normalized in {"channels", "roles", "sheets"}:
            if normalized == "channels":
                channel_sections = {"Channels": [], "Threads": [], "Other": []}

                def _channel_bucket(key: str) -> str:
                    if key == "PANEL_THREAD_MODE":
                        return "Other"
                    if "THREAD" in key:
                        return "Threads"
                    if "CHANNEL" in key or key == "GUILD_IDS":
                        return "Channels"
                    return "Other"

                ordered_channels = [
                    "GUILD_IDS",
                    "LOG_CHANNEL_ID",
                    "WELCOME_CHANNEL_ID",
                    "WELCOME_GENERAL_CHANNEL_ID",
                    "NOTIFY_CHANNEL_ID",
                    "PROMO_CHANNEL_ID",
                    "RECRUITERS_CHANNEL_ID",
                    "RECRUITERS_THREAD_ID",
                    "REPORT_RECRUITERS_DEST_ID",
                    "PANEL_FIXED_THREAD_ID",
                    "PANEL_THREAD_MODE",
                    "ROLEMAP_CHANNEL_ID",
                    "SERVER_MAP_CHANNEL_ID",
                ]
                seen_channels: set[str] = set()
                for key in ordered_channels:
                    seen_channels.add(key)
                    bucket = _channel_bucket(key)
                    channel_sections[bucket].extend(
                        self._format_channel_entry(key, entries.get(key))
                    )

                dynamic_channels = [
                    key
                    for key in entries.keys()
                    if key not in seen_channels
                    and not _is_secret_key(key)
                    and any(token in key for token in ("CHANNEL", "THREAD", "GUILD"))
                ]
                for key in sorted(dynamic_channels):
                    bucket = _channel_bucket(key)
                    channel_sections[bucket].extend(
                        self._format_channel_entry(key, entries.get(key))
                    )

                sections = [
                    ("Channels", channel_sections["Channels"] or ["—"]),
                    ("Threads", channel_sections["Threads"] or ["—"]),
                    ("Other", channel_sections["Other"] or ["—"]),
                ]
            elif normalized == "roles":
                sections = [("Roles", self._format_roles(entries))]
            else:
                sheets_lines: list[str] = []
                tab_lines: list[str] = []
                config_lines: list[str] = []

                for key, entry in entries.items():
                    if _is_secret_key(key):
                        continue
                    if _is_sheet_key(key):
                        if key.endswith("_TAB"):
                            tab_lines.extend(
                                self._format_simple_line(key, entry)
                            )
                        else:
                            sheets_lines.extend(
                                self._format_simple_line(key, entry)
                            )

                if sheet_sections:
                    for label, rows in sheet_sections:
                        for row_key, value, resolved in rows:
                            value_text = f"{value}"
                            label = resolved if resolved and resolved != "—" else "value"
                            config_lines.extend(
                                self._format_key_block(
                                    f"{label} override: {row_key}",
                                    [(value_text, label)],
                                )
                            )

                toggles = get_feature_toggles()
                if toggles:
                    for name in sorted(toggles):
                        value = "ON" if toggles[name] else "OFF"
                        config_lines.extend(
                            self._format_key_block(name, [(value, "value")])
                        )

                meta = _config_meta_from_app()
                source = str(meta.get("source", "runtime"))
                status = str(meta.get("status", "ok"))
                loader_values = [(source, status)]
                loaded_at = meta.get("loaded_at")
                if loaded_at:
                    loader_values.append((str(loaded_at), "loaded_at"))
                last_error = meta.get("last_error")
                if last_error:
                    loader_values.append((str(last_error), "last_error"))
                config_lines.extend(self._format_key_block("Config Loader", loader_values))

                sections = [
                    ("Sheets", sheets_lines or ["—"]),
                    ("Tabs", tab_lines or ["—"]),
                    ("Config", config_lines or ["—"]),
                    ("Secrets (masked)", self._format_secrets(entries)),
                ]

            return self.render_env_embed(
                f"{render_meta.bot_name} · env: {render_meta.env} · {section_title}",
                sections,
                render_meta,
            )

        else:
            section_title = "Config"

            core_lines = self._format_core_identity(entries)
            feature_lines = self._format_features(entries)
            cache_lines = self._format_cache_refresh(entries)
            watchdog_lines = self._format_watchdog(entries)
            render_lines = self._format_render(entries)

            used_keys: set[str] = set()
            used_keys.update({"BOT_NAME", "BOT_VERSION", "ENV_NAME"})
            used_keys.update({"STRICT_PROBE", "PANEL_THREAD_MODE", "SEARCH_RESULTS_SOFT_CAP"})
            used_keys.update(
                key
                for key in entries.keys()
                if key.startswith("ENABLE_")
                or key.endswith("_ENABLED")
                or key in {"STRICT_PROBE", "PANEL_THREAD_MODE"}
            )
            used_keys.update(
                {
                    "CLAN_TAGS_CACHE_TTL_SEC",
                    "REFRESH_TIMES",
                    "REPORT_DAILY_POST_TIME",
                    "CLEANUP_INTERVAL_HOURS",
                    "KEEPALIVE_INTERVAL_HOURS",
                }
            )
            used_keys.update(
                key for key in entries.keys() if "TTL" in key or "REFRESH" in key
            )
            used_keys.update(
                {
                    "WATCHDOG_CHECK_SEC",
                    "WATCHDOG_STALL_SEC",
                    "WATCHDOG_DISCONNECT_GRACE_SEC",
                    "TIMEZONE",
                    "PORT",
                    "LOG_LEVEL",
                }
            )
            used_keys.update(
                key
                for key in entries.keys()
                if key.startswith("WATCHDOG_") or key in {"TIMEZONE", "PORT", "LOG_LEVEL"}
            )
            used_keys.update(
                key for key in entries.keys() if key.startswith("RENDER_")
            )
            used_keys.update(
                key
                for key in entries.keys()
                if any(token in key for token in ("CHANNEL", "THREAD", "GUILD"))
                or key in {"PANEL_THREAD_MODE"}
            )
            used_keys.update(
                key for key in entries.keys() if "ROLE" in key and not _is_sheet_key(key)
            )
            used_keys.update(key for key in entries.keys() if _is_sheet_key(key))

            other_keys = [
                key
                for key in entries.keys()
                if key not in used_keys and not _is_secret_key(key)
            ]
            other_lines = [
                line
                for key in sorted(other_keys)
                for line in self._format_simple_line(key, entries.get(key))
            ] or ["—"]
            sections = [
                ("Core Identity", core_lines),
                ("Feature Flags", feature_lines),
                ("Cache / Refresh", cache_lines),
                ("Watchdog / Runtime", watchdog_lines),
                ("Render", render_lines),
                ("Other", other_lines),
            ]

        return self.render_env_embed(
            f"{render_meta.bot_name} · env: {render_meta.env} · {section_title}",
            sections,
            render_meta,
        )

    @tier("user")
    @ops.command(
        name="help",
        usage="[command]",
        extras={"hide_in_help": True},
        help="Shows the permission-aware help menu with dynamic visibility.",
        brief="Shows the permission-aware help menu.",
    )
    async def ops_help(
        self, ctx: commands.Context, *, query: str | None = None
    ) -> None:
        await self.render_help(ctx, query=query)

    async def render_help(
        self, ctx: commands.Context, *, query: str | None = None
    ) -> None:
        await self._render_help(ctx, query=query)

    @tier("user")
    @help_metadata(function_group="operational", section="general", access_tier="user")
    @ops.command(
        name="ping",
        extras={"hide_in_help": True},
        help="Routes to the mention-based ping command for quick availability checks.",
        brief="Routes to the mention-based ping command.",
    )
    async def ops_ping(self, ctx: commands.Context) -> None:
        command = self.bot.get_command("ping")
        if command is None:
            await ctx.send(str(sanitize_text("Ping command unavailable.")))
            return
        await ctx.invoke(command)

    async def _render_help(
        self, ctx: commands.Context, *, query: str | None
    ) -> None:
        prefix = get_command_prefix()
        bot_version = os.getenv("BOT_VERSION", "dev")
        bot_name = get_bot_name()
        lookup = query.strip() if isinstance(query, str) else ""

        diagnostics: _HelpDiagnosticsCollector | None = None
        if self._help_diagnostics_enabled():
            diagnostics = self._create_help_diagnostics_collector()

        if not lookup:
            tiers = await self._gather_overview_tiers(ctx, diagnostics=diagnostics)
            if not tiers:
                await ctx.reply(str(sanitize_text("No commands available.")))
                await self._maybe_emit_help_diagnostics(ctx, diagnostics)
                return
            embeds = build_help_overview_embeds(
                prefix=prefix,
                overview_title="C1C-Recruitment — help",
                overview_description=self._help_bot_description(bot_name=bot_name),
                tiers=tiers,
                bot_version=bot_version,
                notes=" • For details: @Bot help",
                show_empty_sections=self._show_empty_sections(),
            )
            sanitized = [sanitize_embed(embed) for embed in embeds]
            await self._reply_with_help_embeds(ctx, sanitized)
            await self._maybe_emit_help_diagnostics(ctx, diagnostics)
            return

        normalized_lookup = " ".join(lookup.lower().split())
        command = self.bot.get_command(normalized_lookup)
        if command is None and not normalized_lookup.startswith("ops "):
            command = self.bot.get_command(f"ops {normalized_lookup}")
        if command is None:
            await ctx.reply(str(sanitize_text(f"Unknown command `{lookup}`.")))
            if diagnostics is not None:
                await self._gather_overview_tiers(ctx, diagnostics=diagnostics)
            await self._maybe_emit_help_diagnostics(ctx, diagnostics)
            return

        if not await self._can_display_command(command, ctx):
            await ctx.reply(str(sanitize_text("You do not have access to that command.")))
            if diagnostics is not None:
                await self._gather_overview_tiers(ctx, diagnostics=diagnostics)
            await self._maybe_emit_help_diagnostics(ctx, diagnostics)
            return

        command_info = self._build_help_info(command)
        embed = build_help_detail_embed(
            prefix=prefix,
            command=command_info,
            bot_version=bot_version,
            bot_name=bot_name,
        )
        await self._reply_with_help_embeds(ctx, [sanitize_embed(embed)])
        if diagnostics is not None:
            await self._gather_overview_tiers(ctx, diagnostics=diagnostics)
        await self._maybe_emit_help_diagnostics(ctx, diagnostics)

    async def _reply_with_help_embeds(
        self, ctx: commands.Context, embeds: Sequence[discord.Embed]
    ) -> None:
        if not embeds:
            return

        try:
            await ctx.reply(embeds=list(embeds))
            return
        except TypeError:
            pass
        except discord.HTTPException:
            logger.exception(
                "failed to send consolidated help embeds; falling back to sequential send",
                exc_info=True,
            )

        first, *rest = embeds
        try:
            await ctx.reply(embed=first)
        except (TypeError, discord.HTTPException):
            await ctx.send(embed=first)

        for embed in rest:
            try:
                await ctx.send(embed=embed)
            except discord.HTTPException:
                logger.exception("failed to send help embed", exc_info=True)
                break

    async def _config_impl(self, ctx: commands.Context) -> None:
        snapshot = get_config_snapshot()
        meta_raw = _config_meta_from_app()

        env = get_env_name()
        bot_version = str(snapshot.get("BOT_VERSION") or os.getenv("BOT_VERSION", "dev"))

        connected_items: List[str] = []
        for guild in getattr(self.bot, "guilds", []):
            name = getattr(guild, "name", None) or f"Guild {getattr(guild, 'id', 'n/a')}"
            if env:
                connected_items.append(f"{name} [{env}]")
            else:
                connected_items.append(name)

        allow_ids = sorted(get_allowed_guild_ids())
        allow_entries: List[Dict[str, object]] = []
        for snowflake in allow_ids:
            resolved = _trim_resolved_label(self._id_resolver.resolve(self.bot, snowflake))
            if resolved and resolved != "(not found)":
                display = resolved
                resolved_name: Optional[str] = resolved
            else:
                display = f"Guild {snowflake} (unresolved)"
                resolved_name = None
            allow_entries.append({
                "id": snowflake,
                "display": display,
                "resolved": resolved_name,
            })

        allow_summary = next(
            (str(entry["resolved"]) for entry in allow_entries if entry.get("resolved")),
            None,
        )
        if not allow_summary and allow_entries:
            allow_summary = str(allow_entries[0]["display"])

        meta: Dict[str, Any]
        if isinstance(meta_raw, dict):
            meta = dict(meta_raw)
        else:
            meta = {}

        def _sheet_entry(target: SheetTarget, *, fallback_index: int) -> Dict[str, object]:
            slug = target.context or target.label.lower()
            label = target.label
            key = target.sheet_id_key
            raw_value = snapshot.get(key) if isinstance(snapshot, Mapping) else None
            if isinstance(raw_value, str):
                sheet_id = raw_value.strip()
            elif raw_value is None:
                sheet_id = ""
            else:
                sheet_id = str(raw_value).strip()

            ok = bool(sheet_id)
            entry: Dict[str, object] = {
                "label": label,
                "ok": ok,
            }
            if ok:
                hint = _lookup_sheet_hint(meta_raw if isinstance(meta_raw, Mapping) else None, slug)
                if hint:
                    entry["hint"] = hint
                else:
                    entry["hint"] = f"Sheet #{fallback_index}"
                    short_id = _short_identifier(sheet_id)
                    if short_id:
                        entry["short_id"] = short_id
            else:
                entry["status"] = "Missing"
            return entry

        sheet_entries = [
            _sheet_entry(target, fallback_index=index)
            for index, target in enumerate(SHEET_TARGETS, start=1)
        ]

        snapshot_mapping: Mapping[str, object] | None
        if isinstance(snapshot, Mapping):
            snapshot_mapping = snapshot
        else:
            snapshot_mapping = None

        ops_chan_id = resolve_ops_log_channel_id(bot=self.bot, snapshot=snapshot_mapping)

        ops_line = "⚠️ Missing"
        if ops_chan_id:
            try:
                channel = self.bot.get_channel(int(ops_chan_id))
                if channel:
                    channel_name = getattr(channel, "name", None) or "unknown"
                    ops_line = f"✅ #{channel_name}"
                else:
                    tail = str(ops_chan_id)[-4:]
                    ops_line = f"✅ configured (…{tail})"
            except Exception:
                ops_line = "✅ configured"

        ops_detail: Optional[str] = ops_line if ops_chan_id else None

        override_keys = _extract_override_keys(meta) if meta else []

        overview = {
            "env": env,
            "connected": {
                "count": len(connected_items),
                "items": connected_items,
            },
            "allow": {
                "count": len(allow_entries),
                "items": [entry["display"] for entry in allow_entries],
                "summary": allow_summary,
            },
            "sheets": sheet_entries,
            "ops": {
                "configured": bool(ops_chan_id),
                "detail": ops_detail,
            },
            "source": {
                "loaded_from": meta.get("source", "Environment variables"),
                "overrides": override_keys,
            },
        }

        meta["overview"] = overview

        embed = build_config_embed(
            snapshot,
            meta,
            bot_version=bot_version,
            coreops_version=COREOPS_VERSION,
        )

        try:
            fields = list(getattr(embed, "fields", []))
            for idx, field in enumerate(fields):
                name = str(getattr(field, "name", ""))
                if name.strip().lower() == "ops channel":
                    embed.remove_field(idx)
                    break
        except Exception:
            pass

        embed.add_field(name="Ops channel", value=ops_line, inline=False)

        await ctx.reply(embed=sanitize_embed(embed))

    @tier("admin")
    @help_metadata(function_group="operational", section="sheet_tools", access_tier="admin")
    @ops.command(
        name="config",
        help="Shows current configuration values.",
        brief="Shows current configuration values.",
    )
    @guild_only_denied_msg()
    @ops_only()
    async def ops_config(self, ctx: commands.Context) -> None:
        await self._config_impl(ctx)

    @tier("admin")
    @help_metadata(function_group="operational", section="sheet_tools", access_tier="admin")
    @commands.command(
        name="config",
        hidden=True,
        help="Shows current configuration values.",
        brief="Shows current configuration values.",
    )
    @guild_only_denied_msg()
    @admin_only()
    async def config_summary(self, ctx: commands.Context) -> None:
        await self._config_impl(ctx)

    async def _refresh_root(self, ctx: commands.Context) -> None:
        author = getattr(ctx, "author", None)
        if not _admin_roles_configured() and not (
            is_admin_member(author) or is_staff_member(author)
        ):
            await ctx.send(
                str(sanitize_text("⚠️ Admin roles not configured — refresh commands disabled."))
            )
            return
        await ctx.send(
            str(sanitize_text("Available: `!refresh all`, `!refresh clansinfo`"))
        )

    @tier("admin")
    @help_metadata(function_group="operational", section="utilities", access_tier="admin")
    @commands.command(
        name="reload",
        hidden=True,
        help="Reloads runtime configs and command modules.",
        brief="Reloads runtime configs and command modules.",
    )
    @guild_only_denied_msg()
    @admin_only()
    async def reload(self, ctx: commands.Context, *flags: str) -> None:
        reboot, unknown = self._parse_reload_flags(flags)
        if unknown is not None:
            await ctx.send(
                str(sanitize_text(f"⚠️ Unknown flag: {unknown}"))
            )
            return

        await self._reload_impl(ctx, reboot=reboot)

    @tier("admin")
    @help_metadata(function_group="operational", section="utilities", access_tier="admin")
    @ops.command(
        name="reload",
        help="Reloads runtime configs and command modules.",
        brief="Reloads runtime configs and command modules.",
    )
    @guild_only_denied_msg()
    @ops_only()
    async def ops_reload(self, ctx: commands.Context, *flags: str) -> None:
        reboot, unknown = self._parse_reload_flags(flags)
        if unknown is not None:
            await ctx.send(
                str(sanitize_text(f"⚠️ Unknown flag: {unknown}"))
            )
            return

        await self._reload_impl(ctx, reboot=reboot)

    @tier("admin")
    @help_metadata(function_group="operational", section="sheet_tools", access_tier="admin")
    @commands.group(
        name="refresh",
        invoke_without_command=True,
        hidden=True,
        help="Refreshes a single data bucket from Google Sheets.",
        brief="Refreshes a single data bucket from Google Sheets.",
    )
    @guild_only_denied_msg()
    @admin_only()
    async def refresh(
        self, ctx: commands.Context, *, bucket: Optional[str] = None
    ) -> None:
        """Admin group: manual cache refresh."""

        if bucket and bucket.strip():
            await self._refresh_single_impl(ctx, bucket)
            return
        await self._refresh_root(ctx)

    @tier("admin")
    @help_metadata(function_group="operational", section="sheet_tools", access_tier="admin")
    @ops.group(
        name="refresh",
        invoke_without_command=True,
        help="Refreshes a single data bucket from Google Sheets.",
        brief="Refreshes a single data bucket from Google Sheets.",
    )
    @guild_only_denied_msg()
    @ops_only()
    async def ops_refresh(
        self, ctx: commands.Context, *, bucket: Optional[str] = None
    ) -> None:
        if bucket and bucket.strip():
            await self._refresh_single_impl(ctx, bucket)
            return
        await self._refresh_root(ctx)

    async def _refresh_all_impl(self, ctx: commands.Context) -> None:
        buckets = cache_telemetry.list_buckets()
        if not buckets:
            await ctx.send(str(sanitize_text("⚠️ No cache buckets registered.")))
            return

        actor_display = getattr(ctx.author, "display_name", None) or str(ctx.author)
        actor = str(ctx.author)
        actor_id = getattr(ctx.author, "id", None)

        overall_start = time.monotonic()
        summaries: list[str] = []
        failures: list[str] = []
        embed_rows: list[RefreshEmbedRow] = []
        refresh_results: list[cache_telemetry.RefreshResult] = []

        for name in buckets:
            try:
                result = await cache_telemetry.refresh_now(name, actor=actor)
            except asyncio.CancelledError:
                raise

            summary, ok = self._format_refresh_summary(result)
            prefix = "•" if ok else "⚠"
            summaries.append(f"{prefix} {summary}")
            if not ok:
                failures.append(name)
            embed_rows.append(self._build_refresh_row(result))
            refresh_results.append(result)

        total_duration = int((time.monotonic() - overall_start) * 1000)
        header = (
            f"cache refresh · {len(buckets)} bucket(s) · {total_duration} ms · by {actor_display}"
        )

        message = "\n".join([header, *summaries])
        await self._send_refresh_response(
            ctx,
            scope="all",
            actor_display=actor_display,
            rows=embed_rows,
            total_duration=total_duration,
            fallback_message=message,
        )

        if refresh_results:
            bucket_names = [result.name or name for result, name in zip(refresh_results, buckets)]
            deduper = refresh_deduper()
            key = refresh_dedupe_key("all", None, bucket_names)
            if deduper.should_emit(key):
                from modules.common import runtime as runtime_helpers  # local import to avoid cycles

                bucket_results = refresh_bucket_results(refresh_results)
                await runtime_helpers.send_log_message(
                    format_refresh_message(
                        "all",
                        bucket_results,
                        total_s=total_duration / 1000.0,
                    )
                )

        log_msg, extra = sanitize_log(
            f"{logfmt.LOG_EMOJI['refresh']} **Cache refresh** — completed",
            extra={
                "actor": actor,
                "actor_id": int(actor_id) if isinstance(actor_id, int) else actor_id,
                "buckets": buckets,
                "duration_ms": total_duration,
                "failures": failures,
            },
        )
        logger.info(log_msg, extra=extra)

    @tier("admin")
    @help_metadata(function_group="operational", section="sheet_tools", access_tier="admin")
    @refresh.command(
        name="all",
        help="Reloads all data from Sheets.",
        brief="Reloads all data from Sheets.",
    )
    @guild_only_denied_msg()
    @admin_only()
    @commands.cooldown(1, 30.0, commands.BucketType.guild)
    async def refresh_all(self, ctx: commands.Context) -> None:
        """Admin: clear & warm all registered Sheets caches."""

        await self._refresh_all_impl(ctx)

    @tier("admin")
    @help_metadata(function_group="operational", section="sheet_tools", access_tier="admin")
    @ops_refresh.command(
        name="all",
        help="Reloads all data from Sheets.",
        brief="Reloads all data from Sheets.",
    )
    @guild_only_denied_msg()
    @ops_only()
    @commands.cooldown(1, 30.0, commands.BucketType.guild)
    async def ops_refresh_all(self, ctx: commands.Context) -> None:
        await self._refresh_all_impl(ctx)

    def _build_refresh_row(
        self, result: cache_telemetry.RefreshResult
    ) -> RefreshEmbedRow:
        snapshot = result.snapshot
        label = _format_bucket_label(result.name) or result.name or "-"
        duration_ms = result.duration_ms if result.duration_ms is not None else 0
        duration_text = f"{duration_ms} ms"

        raw_result = (snapshot.last_result or ("ok" if result.ok else "fail")).strip()
        display_result = raw_result.replace("_", " ") if raw_result else "-"

        normalized = raw_result.lower()
        retries = "1" if normalized in {"retry_ok", "fail"} else "0"

        error_text = result.error or snapshot.last_error or "-"
        cleaned_error = " ".join(str(error_text).split()) if error_text else "-"
        if len(cleaned_error) > 70:
            cleaned_error = f"{cleaned_error[:67]}…"

        ttl_display = "?"
        if snapshot.ttl_expired is True:
            ttl_display = "yes"
        elif snapshot.ttl_expired is False:
            ttl_display = "no"

        count_display = "-"
        if snapshot.item_count is not None:
            count_display = str(snapshot.item_count)

        return RefreshEmbedRow(
            bucket=label,
            duration=duration_text,
            result=display_result or "-",
            retries=retries,
            ttl_expired=ttl_display,
            count=count_display,
            error=cleaned_error or "-",
        )

    async def _send_refresh_response(
        self,
        ctx: commands.Context,
        *,
        scope: str,
        actor_display: str,
        rows: Sequence[RefreshEmbedRow],
        total_duration: int,
        fallback_message: str,
    ) -> None:
        bot_version = os.getenv("BOT_VERSION", "dev")
        now_utc = dt.datetime.now(UTC)

        embed = None
        try:
            embed = build_refresh_embed(
                scope=scope,
                actor_display=actor_display,
                trigger="manual",
                rows=rows,
                total_ms=total_duration,
                bot_version=bot_version,
                coreops_version=COREOPS_VERSION,
                now_utc=now_utc,
            )
        except Exception:
            embed = None

        sent = False
        if embed is not None:
            try:
                await ctx.send(embed=sanitize_embed(embed))
            except Exception:
                sent = False
            else:
                sent = True

        if not sent:
            await ctx.send(str(sanitize_text(fallback_message)))

    async def _refresh_clansinfo_impl(self, ctx: commands.Context) -> None:
        snapshot = cache_telemetry.get_snapshot("clans")
        if not snapshot.available:
            await ctx.send(str(sanitize_text("⚠️ No clansinfo cache registered.")))
            return

        age_seconds = snapshot.age_seconds if snapshot.age_seconds is not None else 10**9
        if age_seconds < 60 * 60:
            mins = age_seconds // 60
            nxt = ""
            if snapshot.next_refresh_delta_seconds is not None and snapshot.next_refresh_human:
                delta = snapshot.next_refresh_delta_seconds
                if delta >= 0:
                    nxt = f" Next auto-refresh in {snapshot.next_refresh_human}"
                else:
                    nxt = f" Next auto-refresh overdue by {snapshot.next_refresh_human}"
            await ctx.send(str(sanitize_text(f"✅ Clans cache fresh ({mins}m old).{nxt}")))
            return

        await ctx.send(str(sanitize_text("Refreshing clans (background).")))
        asyncio.create_task(
            cache_telemetry.refresh_now("clans", actor=str(ctx.author))
        )

    @tier("admin")
    @help_metadata(function_group="operational", section="sheet_tools", access_tier="admin")
    @refresh.command(
        name="clansinfo",
        help="Updates the clan info list.",
        brief="Updates the clan info list.",
    )
    @guild_only_denied_msg()
    @admin_only()
    async def refresh_clansinfo(self, ctx: commands.Context) -> None:
        """Staff/Admin: refresh 'clans' cache if age ≥ 60 min."""

        await self._refresh_clansinfo_impl(ctx)

    @tier("staff")
    @help_metadata(function_group="operational", section="sheet_tools", access_tier="staff")
    @ops_refresh.command(
        name="clansinfo",
        help="Updates the clan info list.",
        brief="Updates the clan info list.",
    )
    @guild_only_denied_msg()
    @ops_only()
    async def ops_refresh_clansinfo(self, ctx: commands.Context) -> None:
        await self._refresh_clansinfo_impl(ctx)

    async def _gather_overview_tiers(
        self,
        ctx: commands.Context,
        *,
        diagnostics: _HelpDiagnosticsCollector | None = None,
    ) -> list[HelpTier]:
        ordered_audiences = [
            key
            for key in ("admin", "staff", "user")
            if key in self._HELP_AUDIENCE_CONFIGS
        ]

        if not ordered_audiences:
            return []

        admin_allowlist = self._resolve_admin_allowlist()

        commands_iter: list[commands.Command[Any, Any, Any]] = []
        access_results: Dict[str, _CommandAccessResult] = {}
        for command in self.bot.walk_commands():
            qualified = getattr(command, "qualified_name", None)
            if diagnostics is not None and isinstance(qualified, str) and qualified:
                diagnostics.register(command)

            result = await self._evaluate_command_access(command, ctx)
            if isinstance(qualified, str) and qualified:
                access_results[qualified] = result
                if diagnostics is not None:
                    diagnostics.mark_can_run(qualified, result)

            if not _should_show(command):
                if diagnostics is not None and isinstance(qualified, str) and qualified:
                    diagnostics.mark_filtered(qualified, "hidden")
                continue
            if not self._include_in_overview(command):
                if diagnostics is not None and isinstance(qualified, str) and qualified:
                    diagnostics.mark_filtered(qualified, "not in overview")
                continue
            if self._is_duplicate_admin_alias(command):
                if diagnostics is not None and isinstance(qualified, str) and qualified:
                    diagnostics.mark_filtered(qualified, "duplicate alias")
                continue
            commands_iter.append(command)

        commands_iter.sort(key=lambda cmd: cmd.qualified_name)

        seen: set[str] = set()
        infos: list[HelpCommandInfo] = []
        for command in commands_iter:
            qualified = command.qualified_name
            if qualified in seen:
                if diagnostics is not None:
                    diagnostics.mark_filtered(qualified, "duplicate entry")
                continue
            seen.add(qualified)

            result = access_results.get(qualified)
            if result is None:
                result = await self._evaluate_command_access(command, ctx)
                if diagnostics is not None:
                    diagnostics.mark_can_run(qualified, result)

            if not result.can_run:
                if diagnostics is not None:
                    diagnostics.mark_not_displayed(
                        qualified, result.reason or "not runnable"
                    )
                continue

            info = self._build_help_info(command)
            info = await self._apply_help_overrides(
                ctx, command, info, admin_allowlist
            )

            tier_key = (info.access_tier or "user").strip().lower()
            if tier_key not in ordered_audiences:
                if diagnostics is not None:
                    diagnostics.mark_not_displayed(qualified, "audience mismatch")
                continue

            audience = self._HELP_AUDIENCE_CONFIGS[tier_key]
            function_group = (info.function_group or "general").strip().lower()
            if (
                audience.allowed_function_groups
                and function_group not in audience.allowed_function_groups
            ):
                if diagnostics is not None:
                    diagnostics.mark_not_displayed(qualified, "hidden")
                continue

            section_key = (
                info.section or info.function_group or "general"
            ).strip().lower()
            section = self._match_section_config(
                audience, section_key=section_key, function_group=function_group
            )
            if section is None:
                if diagnostics is not None:
                    diagnostics.mark_not_displayed(qualified, "empty section")
                continue

            infos.append(info)
            if diagnostics is not None:
                diagnostics.mark_displayed(qualified)

        manual_infos = await self._manual_overview_entries(seen, ctx)
        if manual_infos:
            infos.extend(manual_infos)
            seen.update(item.qualified_name for item in manual_infos)
            if diagnostics is not None:
                for item in manual_infos:
                    diagnostics.mark_manual_display(item.qualified_name)

        if not infos:
            return []

        buckets: dict[str, dict[str, list[HelpCommandInfo]]] = {
            key: {} for key in ordered_audiences
        }

        allow_empty_tiers = self._show_empty_sections()
        author = getattr(ctx, "author", None)

        def _audience_is_visible(key: str) -> bool:
            if key == "admin":
                return can_view_admin(author)
            if key == "staff":
                return can_view_staff(author)
            return True

        for info in infos:
            tier_key = (info.access_tier or "user").strip().lower()
            if tier_key not in buckets:
                continue

            audience = self._HELP_AUDIENCE_CONFIGS[tier_key]
            function_group = (info.function_group or "general").strip().lower()
            if (
                audience.allowed_function_groups
                and function_group not in audience.allowed_function_groups
            ):
                continue

            section_key = (info.section or info.function_group or "general").strip().lower()
            section = self._match_section_config(
                audience, section_key=section_key, function_group=function_group
            )
            if section is None:
                continue

            buckets.setdefault(tier_key, {}).setdefault(section.key, []).append(info)

        tiers: list[HelpTier] = []
        for key in ordered_audiences:
            config = self._HELP_AUDIENCE_CONFIGS[key]
            section_bucket = buckets.get(key, {})
            tier_sections: list[HelpTierSection] = []
            for section in config.sections:
                commands = tuple(
                    sorted(section_bucket.get(section.key, ()), key=self._help_sort_key)
                )
                tier_sections.append(
                    HelpTierSection(label=section.label, commands=commands)
                )

            has_commands = any(section.commands for section in tier_sections)
            if not has_commands and (not allow_empty_tiers or not _audience_is_visible(key)):
                continue
            tiers.append(HelpTier(title=config.title, sections=tuple(tier_sections)))
        return tiers

    def _is_duplicate_admin_alias(
        self, command: commands.Command[Any, Any, Any]
    ) -> bool:
        if not getattr(command, "hidden", False):
            return False
        if command.parent is not None:
            return False

        qualified = command.qualified_name.strip().lower()
        if not qualified:
            return False

        ops_variant = self.bot.get_command(f"ops {qualified}")
        return ops_variant is not None

    async def _apply_help_overrides(
        self,
        ctx: commands.Context,
        command: commands.Command[Any, Any, Any],
        info: HelpCommandInfo,
        admin_allowlist: Set[str],
    ) -> HelpCommandInfo:
        current_tier = (info.access_tier or "user").strip().lower()
        display_tier = self._resolve_help_tier(command, current_tier)
        usage_override = info.usage_override
        section_override = info.section

        qualified = command.qualified_name
        if display_tier == "admin" and qualified.startswith("ops "):
            base_name = qualified.split(" ", 1)[1].strip()
            normalized = normalize_command_text(base_name)
            usage_override = f"!ops {base_name}" if base_name else "!ops"

            if normalized and normalized in admin_allowlist:
                bare_command = self.bot.get_command(base_name)
                if bare_command is not None and bare_command is not command:
                    if await self._can_display_command(bare_command, ctx):
                        usage_override = f"!{base_name}"

        if display_tier == "admin":
            section_key = (info.section or "").strip().lower()
            if section_key == "sheet_tools":
                section_override = "sheets_cache"

        if (
            usage_override != info.usage_override
            or display_tier != current_tier
            or section_override != info.section
        ):
            return replace(
                info,
                usage_override=usage_override,
                access_tier=display_tier,
                section=section_override,
            )
        return info

    def _resolve_help_tier(
        self, command: commands.Command[Any, Any, Any], current: str
    ) -> str:
        normalized = current if current in {"admin", "staff", "user"} else "user"

        extras = getattr(command, "extras", None)
        base_tier: str | None = None
        if isinstance(extras, dict):
            base_tier = str(extras.get("tier") or extras.get("_tier") or "").strip().lower()
        if not base_tier:
            base_tier = str(getattr(command, "_tier", "")).strip().lower()

        if base_tier == "admin":
            return "admin"
        if base_tier == "staff" and normalized not in {"admin", "staff"}:
            return "staff"

        return normalized

    def _resolve_admin_allowlist(self) -> Set[str]:
        try:
            settings = load_coreops_settings()
        except Exception:
            return set(self._admin_bang_allowlist)

        allowlist = {
            normalize_command_text(entry)
            for entry in settings.admin_bang_allowlist
            if normalize_command_text(entry)
        }
        if allowlist:
            self._admin_bang_allowlist = allowlist
        return set(self._admin_bang_allowlist)

    def _match_section_config(
        self,
        audience: _HelpAudienceConfig,
        *,
        section_key: str,
        function_group: str,
    ) -> _HelpSectionConfig | None:
        normalized_section = section_key.strip().lower()
        normalized_group = function_group.strip().lower()

        for section in audience.sections:
            if section.key != normalized_section:
                continue
            if section.allowed_function_groups and normalized_group:
                if normalized_group not in section.allowed_function_groups:
                    return None
            return section

        if not normalized_group:
            return None

        for section in audience.sections:
            if section.allowed_function_groups and normalized_group:
                if normalized_group not in section.allowed_function_groups:
                    continue
            if section.key == normalized_group:
                return section

        return None

    async def _manual_overview_entries(
        self, seen: Set[str], ctx: commands.Context
    ) -> list[HelpCommandInfo]:
        entries: list[HelpCommandInfo] = []

        author = getattr(ctx, "author", None)

        def _is_allowed(tier: str | None) -> bool:
            normalized = (tier or "user").strip().lower()
            if normalized == "admin":
                return can_view_admin(author)
            if normalized == "staff":
                return can_view_staff(author)
            return True

        for command_name, usage in ("ops help", "@Bot help"), ("ops ping", "@Bot ping"):
            if command_name in seen:
                continue
            command = self.bot.get_command(command_name)
            if command is None:
                continue
            if not await self._can_display_command(command, ctx, log_failures=True):
                continue
            info = self._build_help_info(command)
            if not _is_allowed(info.access_tier):
                continue
            entries.append(
                replace(
                    info,
                    section="general",
                    function_group="general",
                    usage_override=usage,
                )
            )

        return entries

    def _help_sort_key(self, info: HelpCommandInfo) -> tuple[str, str]:
        usage = (info.usage_override or info.qualified_name or "").lower()
        return (usage, info.qualified_name.lower())

    def _include_in_overview(self, command: commands.Command[Any, Any, Any]) -> bool:
        if command.parent is None:
            return True

        top = command
        while top.parent is not None:
            top = top.parent
        return top.qualified_name in {"ops", "perm"}

    async def _gather_subcommand_infos(
        self, command: commands.Command[Any, Any, Any], ctx: commands.Context
    ) -> list[HelpCommandInfo]:
        if not isinstance(command, commands.Group):
            return []

        infos: list[HelpCommandInfo] = []
        seen: set[str] = set()
        for subcommand in command.commands:
            if not _should_show(subcommand):
                continue
            # Guard against duplicate references when aliases are registered.
            base_name = subcommand.qualified_name
            if base_name in seen:
                continue
            seen.add(base_name)
            if not await self._can_display_command(
                subcommand, ctx, log_failures=True
            ):
                continue
            infos.append(self._build_help_info(subcommand))

        infos.sort(key=lambda item: item.qualified_name)
        return infos

    def _audience_denial_reason(self, audience: str, required: str) -> str:
        if required == "admin":
            if audience == "staff":
                return "not runnable for staff"
            return "not runnable for user"
        if required == "staff":
            return "not runnable for user"
        return "not runnable"

    async def _evaluate_command_access(
        self, command: commands.Command[Any, Any, Any], ctx: commands.Context
    ) -> _CommandAccessResult:
        if not command.enabled:
            return _CommandAccessResult(can_run=False, reason="disabled")

        audience = self._current_help_audience(ctx)
        author = getattr(ctx, "author", None)
        tier = _get_tier(command).strip().lower()
        if tier == "admin" and not can_view_admin(author):
            return _CommandAccessResult(
                can_run=False,
                reason=self._audience_denial_reason(audience, "admin"),
            )
        if tier == "staff" and not can_view_staff(author):
            return _CommandAccessResult(
                can_run=False,
                reason=self._audience_denial_reason(audience, "staff"),
            )

        sentinel = object()
        previous = getattr(ctx, "_coreops_suppress_denials", sentinel)
        setattr(ctx, "_coreops_suppress_denials", True)
        try:
            await command.can_run(ctx)
        except commands.CheckFailure:
            return _CommandAccessResult(can_run=False, reason="not runnable")
        except commands.CommandError:
            return _CommandAccessResult(can_run=False, reason="not runnable")
        except Exception:  # pragma: no cover - defensive guard
            logger.exception("failed help gate for command", exc_info=True)
            return _CommandAccessResult(can_run=False, reason="error")
        finally:
            if previous is sentinel:
                try:
                    delattr(ctx, "_coreops_suppress_denials")
                except AttributeError:
                    pass
            else:
                setattr(ctx, "_coreops_suppress_denials", previous)

        return _CommandAccessResult(can_run=True, reason=None)

    async def _can_display_command(
        self,
        command: commands.Command[Any, Any, Any],
        ctx: commands.Context,
        *,
        log_failures: bool = False,
    ) -> bool:
        result = await self._evaluate_command_access(command, ctx)
        if log_failures and not result.can_run:
            qualified = getattr(command, "qualified_name", None) or getattr(
                command, "name", "<unknown>"
            )
            reason = result.reason or "unknown"
            logger.debug(
                "help gating suppressed command %s (%s)",
                qualified,
                reason,
            )
        return result.can_run

    def _build_help_info(self, command: commands.Command[Any, Any, Any]) -> HelpCommandInfo:
        signature = command.signature or ""
        fallback = command.short_doc or command.help or command.brief or ""
        detailed = (command.help or fallback or "").strip()
        short = fallback.strip()
        if not short:
            short = detailed.splitlines()[0].strip() if detailed else ""
        aliases = tuple(sorted(alias.strip() for alias in command.aliases if alias.strip()))

        extras = getattr(command, "extras", None)
        if isinstance(extras, dict):
            access_tier = str(extras.get("access_tier") or extras.get("tier") or "user")
            function_group = str(extras.get("function_group") or "general")
            section = extras.get("help_section")
            usage_override = extras.get("help_usage")
            raw_flags = extras.get("help_flags")
        else:
            access_tier = str(getattr(command, "_tier", "user"))
            function_group = "general"
            section = None
            usage_override = None
            raw_flags = None

        flags: tuple[str, ...]
        if isinstance(raw_flags, (list, tuple, set)):
            flags = tuple(str(flag).strip() for flag in raw_flags if str(flag).strip())
        elif isinstance(raw_flags, str) and raw_flags.strip():
            flags = (raw_flags.strip(),)
        else:
            flags = tuple()

        return HelpCommandInfo(
            qualified_name=command.qualified_name,
            signature=signature,
            short=short,
            detailed=detailed,
            aliases=aliases,
            access_tier=access_tier,
            function_group=function_group,
            section=str(section).strip() if isinstance(section, str) and section.strip() else None,
            usage_override=str(usage_override).strip() if isinstance(usage_override, str) and usage_override.strip() else None,
            flags=flags,
        )

    def _help_bot_description(self, *, bot_name: str) -> str:
        return (
            "  \n "
            "**C1C-Recruitment keeps the doors open and the hearths warm.**  \n"
            "It’s how we find new clanmates, help old friends move up, and keep every hall filled with good company.\n\n"
            "**Members** can peek at which clans have room, check what’s needed to join or dig into details about any clan across the cluster.  \n\n"
            "**Recruiters** use it to spot open slots, match new arrivals and drop welcome notes so nobody gets lost on day one.  \n\n"
            "_All handled right here on Discord — fast, friendly, and stitched together with that usual C1C chaos and care._ \n\n"
            "**To learn what a command does, type like this:**  \n"
            "`@Bot help ping` → shows info for `@Bot ping`"
        )

    def _show_empty_sections(self) -> bool:
        value = os.getenv("SHOW_EMPTY_SECTIONS", "")
        normalized = value.strip().lower()
        return normalized in {"1", "true", "yes", "on"}

    def _help_diagnostics_enabled(self) -> bool:
        value = os.getenv("HELP_DIAGNOSTICS", "")
        normalized = value.strip().lower()
        return normalized in {"1", "true", "yes", "on"}

    def _help_diagnostics_ttl(self) -> float:
        raw = os.getenv("HELP_DIAGNOSTICS_TTL_SEC", "").strip()
        if not raw:
            return 60.0
        try:
            ttl = float(raw)
        except (TypeError, ValueError):
            return 60.0
        return max(0.0, ttl)

    def _current_help_audience(self, ctx: commands.Context) -> str:
        author = getattr(ctx, "author", None)
        if can_view_admin(author):
            return "admin"
        if can_view_staff(author):
            return "staff"
        return "user"

    def _create_help_diagnostics_collector(self) -> _HelpDiagnosticsCollector:
        return _HelpDiagnosticsCollector()

    def _sanitize_log_identity(self, value: object) -> str:
        if value is None:
            return "unknown"
        return str(sanitize_text(value))

    def _format_help_diagnostics_message(
        self,
        ctx: commands.Context,
        *,
        report: _HelpDiagnosticsReport,
        audience: str,
    ) -> str | None:
        author = getattr(ctx, "author", None)
        user_name = self._sanitize_log_identity(
            getattr(author, "display_name", None)
            or getattr(author, "name", None)
            or "unknown"
        )
        user_id = getattr(author, "id", "?")

        guild = getattr(ctx, "guild", None)
        guild_name = self._sanitize_log_identity(
            getattr(guild, "name", None) or "DM"
        )
        guild_id = getattr(guild, "id", "?")

        header = (
            f"[Help Diagnostics] audience={audience} • "
            f"guild={guild_name}({guild_id}) • user={user_name}({user_id})"
        )
        summary = (
            f"visible {report.visible} / discovered {report.total} "
            f"(hidden {report.hidden})"
        )

        entries = list(report.entries)
        display_entries = entries[:30]
        extra_count = len(entries) - len(display_entries)

        def _build(rows: list[_HelpDiagnosticsEntry], extra: int) -> str:
            table_rows = [
                "qualname | access_tier | function_group | can_run | displayed | reason"
            ]
            for entry in rows:
                access_tier = entry.access_tier or "MISSING"
                function_group = entry.function_group or "MISSING"
                can_run = "true" if entry.can_run else "false"
                displayed = "yes" if entry.displayed else "no"
                reason = entry.reason or "hidden"
                table_rows.append(
                    f"{entry.qualified_name} | {access_tier} | {function_group} | "
                    f"{can_run} | {displayed} | {reason}"
                )

            block = "\n".join(table_rows)
            base = (
                f"{header}\n{summary}\n\n"
                f"```markdown\n{block}\n```"
            )
            if extra > 0:
                base = f"{base}\n(+{extra} more)"
            return str(sanitize_text(base))

        sanitized = _build(display_entries, extra_count)
        while len(sanitized) > 1800 and display_entries:
            display_entries.pop()
            extra_count += 1
            sanitized = _build(display_entries, extra_count)

        return sanitized

    async def _resolve_help_log_target(
        self, ctx: commands.Context
    ) -> tuple[discord.abc.Messageable | None, bool]:
        try:
            channel_id = resolve_ops_log_channel_id(bot=self.bot)
        except Exception:
            channel_id = None
        if channel_id:
            try:
                snowflake = int(channel_id)
            except (TypeError, ValueError):
                snowflake = None
            if snowflake is not None:
                channel = self.bot.get_channel(snowflake)
                if channel is not None and hasattr(channel, "send"):
                    return channel, False

        author = getattr(ctx, "author", None)
        if can_view_admin(author):
            send = getattr(author, "send", None)
            if callable(send):
                return author, True

        return None, False

    async def _maybe_emit_help_diagnostics(
        self,
        ctx: commands.Context,
        collector: _HelpDiagnosticsCollector | None,
    ) -> None:
        if collector is None:
            return

        report = collector.build_report()
        if report is None or not report.entries:
            return

        audience = self._current_help_audience(ctx)
        guild = getattr(ctx, "guild", None)
        guild_id = getattr(guild, "id", None)
        try:
            guild_key = int(guild_id) if guild_id is not None else None
        except (TypeError, ValueError):
            guild_key = None

        ttl = self._help_diagnostics_ttl()
        now = time.monotonic()
        cache_key = (audience, guild_key)
        if ttl > 0:
            expires = self._help_diag_cache.get(cache_key)
            if expires and expires > now:
                return

        message = self._format_help_diagnostics_message(
            ctx, report=report, audience=audience
        )
        if not message:
            return

        target, _ = await self._resolve_help_log_target(ctx)
        if target is None:
            return

        try:
            await target.send(message)
        except Exception:
            logger.exception("failed to post help diagnostics", exc_info=True)
            return

        if ttl > 0:
            self._help_diag_cache[cache_key] = now + ttl

    def _add_embed_group(
        self, embed: discord.Embed, name: str, lines: Sequence[str]
    ) -> None:
        text_lines = list(lines)
        if not text_lines:
            text_lines = ["—"]
        elif all(line == "" for line in text_lines):
            text_lines = ["—"]

        chunks = _chunk_lines(text_lines, _FIELD_CHAR_LIMIT)
        for index, chunk in enumerate(chunks):
            label = name if index == 0 else f"{name} (cont.)"
            embed.add_field(name=label, value=f"```{chunk}```", inline=False)

    def _collect_env_entries(self) -> Dict[str, _EnvEntry]:
        snapshot = get_config_snapshot()
        entries: Dict[str, _EnvEntry] = {}
        for key in _candidate_env_keys(snapshot):
            if key in entries:
                continue
            raw_value: object
            if key in os.environ:
                raw_value = os.environ.get(key)
            else:
                raw_value = snapshot.get(key)

            normalized = _normalize_snapshot_value(raw_value)
            display_value = str(redact_value(key, normalized))
            entries[key] = _EnvEntry(key=key, normalized=normalized, display=display_value)
        return entries

    def _format_key_block(
        self, key: str, values: Sequence[tuple[str, str | None] | str] | None
    ) -> List[str]:
        lines = [key]
        if not values:
            return lines + ["  — (unset)"]
        for value in values:
            if isinstance(value, tuple):
                raw_value, raw_label = value
            else:
                raw_value, raw_label = value, None
            text = str(raw_value).strip()
            if not text or text == "—":
                lines.append("  — (unset)")
                continue
            label = str(raw_label).strip() if raw_label is not None else ""
            suffix = f" → {label}" if label and label != "value" else ""
            lines.append(f"  {text}{suffix}")
        return lines

    def _format_simple_line(self, key: str, entry: Optional[_EnvEntry]) -> List[str]:
        value = entry.display if entry else "—"
        return self._format_key_block(key, [(value, "value")])

    def _format_entry_lines(
        self,
        key: str,
        entry: Optional[_EnvEntry],
        warnings: List[str],
        warning_keys: Set[str],
        *,
        treat_ids: bool = True,
    ) -> List[str]:
        missing = _missing_value_text(entry)
        if missing is not None:
            warnings.append(f"⚠ {key} has no configured value (\"{missing}\")")
            warning_keys.add(key)
            return self._format_simple_line(key, entry)

        if treat_ids and entry is not None:
            ids = self._extract_visible_ids(key, entry.normalized)
            if ids:
                lines, not_found = self._format_id_lines_with_missing(key, ids)
                if not_found:
                    warnings.append(
                        "⚠ "
                        f"{key} contains an ID that does not exist in this guild (not found)"
                    )
                    warning_keys.add(key)
                return lines

        return self._format_simple_line(key, entry)

    def _format_core_identity(self, entries: Dict[str, _EnvEntry]) -> List[str]:
        keys = ("BOT_NAME", "BOT_VERSION", "ENV_NAME")
        lines: List[str] = []
        for key in keys:
            lines.extend(self._format_simple_line(key, entries.get(key)))
        return lines

    def _format_guild_channels(self, entries: Dict[str, _EnvEntry]) -> List[str]:
        ordered = [
            "GUILD_IDS",
            "LOG_CHANNEL_ID",
            "WELCOME_CHANNEL_ID",
            "WELCOME_GENERAL_CHANNEL_ID",
            "NOTIFY_CHANNEL_ID",
            "PROMO_CHANNEL_ID",
            "RECRUITERS_THREAD_ID",
            "REPORT_RECRUITERS_DEST_ID",
            "PANEL_FIXED_THREAD_ID",
            "PANEL_THREAD_MODE",
        ]
        lines: List[str] = []
        seen: Set[str] = set()
        for key in ordered:
            seen.add(key)
            lines.extend(self._format_channel_entry(key, entries.get(key)))

        dynamic = [
            key
            for key in entries.keys()
            if key not in seen
            and not _is_secret_key(key)
            and any(token in key for token in ("CHANNEL", "THREAD", "GUILD"))
        ]
        for key in sorted(dynamic):
            seen.add(key)
            lines.extend(self._format_channel_entry(key, entries.get(key)))

        return lines or ["—"]

    def _format_channel_entry(self, key: str, entry: Optional[_EnvEntry]) -> List[str]:
        if key == "PANEL_THREAD_MODE":
            return self._format_simple_line(key, entry)
        if entry is None:
            return self._format_key_block(key, None)
        ids = self._extract_visible_ids(key, entry.normalized)
        if not ids:
            return self._format_simple_line(key, entry)
        return self._format_id_lines(key, ids)

    def _format_roles(self, entries: Dict[str, _EnvEntry]) -> List[str]:
        ordered = [
            "ADMIN_ROLE_IDS",
            "STAFF_ROLE_IDS",
            "LEAD_ROLE_IDS",
            "RECRUITER_ROLE_IDS",
            "NOTIFY_PING_ROLE_ID",
        ]
        lines: List[str] = []
        seen: Set[str] = set()
        for key in ordered:
            seen.add(key)
            lines.extend(self._format_role_entry(key, entries.get(key)))

        dynamic = [
            key
            for key in entries.keys()
            if key not in seen
            and not _is_secret_key(key)
            and "ROLE" in key
            and not _is_sheet_key(key)
        ]
        for key in sorted(dynamic):
            lines.extend(self._format_role_entry(key, entries.get(key)))

        return lines or ["—"]

    def _format_role_entry(self, key: str, entry: Optional[_EnvEntry]) -> List[str]:
        if entry is None:
            return self._format_key_block(key, None)
        ids = self._extract_visible_ids(key, entry.normalized)
        if not ids:
            return self._format_simple_line(key, entry)
        return self._format_id_lines(key, ids)

    def _format_features(self, entries: Dict[str, _EnvEntry]) -> List[str]:
        ordered = [
            "STRICT_PROBE",
            "PANEL_THREAD_MODE",
            "SEARCH_RESULTS_SOFT_CAP",
        ]
        lines = [
            line
            for key in ordered
            for line in self._format_simple_line(key, entries.get(key))
        ]
        seen = set(ordered)
        dynamic = [
            key
            for key in entries.keys()
            if key not in seen
            and not _is_secret_key(key)
            and (
                key.startswith("ENABLE_")
                or key.endswith("_ENABLED")
                or key in {"STRICT_PROBE", "PANEL_THREAD_MODE"}
            )
        ]
        for key in sorted(dynamic):
            lines.extend(self._format_simple_line(key, entries.get(key)))
        return lines or ["—"]

    def _format_cache_refresh(self, entries: Dict[str, _EnvEntry]) -> List[str]:
        ordered = [
            "CLAN_TAGS_CACHE_TTL_SEC",
            "REFRESH_TIMES",
            "REPORT_DAILY_POST_TIME",
            "CLEANUP_INTERVAL_HOURS",
            "KEEPALIVE_INTERVAL_HOURS",
        ]
        lines = [
            line
            for key in ordered
            for line in self._format_simple_line(key, entries.get(key))
        ]
        seen = set(ordered)
        dynamic = [
            key
            for key in entries.keys()
            if key not in seen
            and not _is_secret_key(key)
            and ("TTL" in key or "REFRESH" in key)
        ]
        for key in sorted(dynamic):
            lines.extend(self._format_simple_line(key, entries.get(key)))
        return lines or ["—"]

    def _format_watchdog(self, entries: Dict[str, _EnvEntry]) -> List[str]:
        ordered = [
            "WATCHDOG_CHECK_SEC",
            "WATCHDOG_STALL_SEC",
            "WATCHDOG_DISCONNECT_GRACE_SEC",
            "TIMEZONE",
            "PORT",
            "LOG_LEVEL",
        ]
        lines = [
            line
            for key in ordered
            for line in self._format_simple_line(key, entries.get(key))
        ]
        seen = set(ordered)
        dynamic = [
            key
            for key in entries.keys()
            if key not in seen
            and not _is_secret_key(key)
            and (key.startswith("WATCHDOG_") or key in {"TIMEZONE", "PORT", "LOG_LEVEL"})
        ]
        for key in sorted(dynamic):
            lines.extend(self._format_simple_line(key, entries.get(key)))
        return lines or ["—"]

    def _format_render(self, entries: Dict[str, _EnvEntry]) -> List[str]:
        keys = [key for key in entries.keys() if key.startswith("RENDER_")]
        if not keys:
            return ["—"]
        lines: List[str] = []
        for key in sorted(keys):
            lines.extend(self._format_simple_line(key, entries.get(key)))
        return lines or ["—"]

    def _format_secrets(self, entries: Dict[str, _EnvEntry]) -> List[str]:
        secrets = [key for key in entries.keys() if _is_secret_key(key)]
        if not secrets:
            return ["—"]
        lines: List[str] = []
        for key in sorted(secrets):
            entry = entries.get(key)
            if entry is None:
                lines.extend(self._format_key_block(key, None))
                continue
            value = entry.display
            if value == "—":
                lines.extend(self._format_key_block(key, None))
            else:
                lines.extend(self._format_key_block(key, [(value, "masked")]))
        return lines

    def _format_id_lines(self, key: str, ids: Sequence[int]) -> List[str]:
        cleaned: List[int] = []
        seen: Set[int] = set()
        for value in ids:
            try:
                snowflake = int(value)
            except (TypeError, ValueError):
                continue
            if snowflake < 0 or snowflake in seen:
                continue
            seen.add(snowflake)
            cleaned.append(snowflake)

        if not cleaned:
            return self._format_key_block(key, None)

        values: List[tuple[str, str | None]] = []
        for snowflake in cleaned:
            resolved = _trim_resolved_label(self._id_resolver.resolve(self.bot, snowflake))
            label = resolved if resolved and resolved != "—" else "unresolved"
            values.append((str(snowflake), label))
        return self._format_key_block(key, values)

    def _format_id_lines_with_missing(
        self, key: str, ids: Sequence[int]
    ) -> tuple[List[str], bool]:
        lines = self._format_id_lines(key, ids)
        not_found = any("(not found)" in line for line in lines)
        return lines, not_found

    def _extract_visible_ids(self, key: str, value: object) -> List[int]:
        ids = _extract_ids(key, value)
        return list(ids)

    def _collect_sheet_sections(self) -> List[Tuple[str, List[Tuple[str, str, str]]]]:
        sections: List[Tuple[str, List[Tuple[str, str, str]]]] = []
        bucket_names = _list_bucket_names()
        snapshot_cache = _gather_snapshot_dicts(bucket_names)

        for label, module_name in _SHEET_CONFIG_SOURCES:
            config_map: Dict[str, Any] = {}

            module_hint = module_name.split(".")[-1].lower()
            if module_hint:
                candidates = [
                    name for name in bucket_names if module_hint in name.lower()
                ]
            else:
                candidates = list(bucket_names)

            for candidate in candidates:
                snapshot = snapshot_cache.get(candidate)
                if snapshot is None:
                    snapshot = _get_snapshot_dict(candidate)
                    snapshot_cache[candidate] = snapshot
                config_map = _extract_sheet_config_from_snapshot(snapshot)
                if config_map:
                    break

            if not config_map:
                config_map = _load_sheet_config_from_module(module_name)

            if not config_map:
                continue

            rows: List[Tuple[str, str, str]] = []
            for key in sorted(config_map.keys()):
                display_key = str(key).upper()
                normalized = _normalize_snapshot_value(config_map[key])
                display_value = redact_value(display_key, normalized)
                resolved = self._resolve_ids(_extract_ids(display_key, normalized))
                rows.append((display_key, display_value, resolved))

            if rows:
                sections.append((label, rows))

        return sections

    def _resolve_ids(self, ids: Sequence[int]) -> str:
        if not ids:
            return "—"
        names = self._id_resolver.resolve_many(self.bot, ids)
        return _format_resolved(names)

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CheckFailure):
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    """discord.py extension hook to register the CoreOps cog."""

    await bot.add_cog(CoreOpsCog(bot))


__all__ = [
    "UTC",
    "CoreOpsCog",
    "_admin_check",
    "_admin_roles_configured",
    "_staff_check",
    "detect_admin_bang_command",
    "setup",
]
