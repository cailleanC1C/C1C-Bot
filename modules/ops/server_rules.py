"""Sheet-driven Server Rules and FAQ publisher."""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any
from urllib.parse import quote, unquote, urlparse

import discord

from modules.common.discord_utils import resolve_message_target
from modules.common.embeds import (
    SERVER_RULES_FAQ_BLUE,
    SERVER_RULES_FAQ_GREEN,
    SERVER_RULES_FAQ_SLATE,
    SERVER_RULES_FAQ_YELLOW,
    get_embed_colour,
)
from shared.sheets import async_adapter
from shared.sheets import core as sheets_core
from shared.sheets import recruitment as recruitment_sheet
from shared.config import get_recruitment_sheet_id
from shared.theme import colors

LEGACY_MARKER_PREFIX = "\u2063\u200bserverrules:"
HIDDEN_MARKER_PREFIX = "https://c1c.invalid/serverrules/"
HIDDEN_MARKER_LABEL = "\u2063"
RECOVERY_PREFIX = "serverrules-recovery:v1:"
RECOVERY_EMPTY = "-"
MUTATION_LOCK = asyncio.Lock()
MAX_EMBEDS_PER_MESSAGE = 10
REQUIRED_HEADERS = {
    "message_key",
    "section",
    "order",
    "enabled",
    "title",
    "description",
    "colour",
    "thumbnail_url",
    "footer",
    "message_id",
}
OPTIONAL_HEADERS = {"topic_key", "topic_title"}
TRUE_VALUES = {"1", "true", "yes", "y", "on", "enabled", "publish", "published"}
FALSE_VALUES = {"", "0", "false", "no", "n", "off", "disabled"}
MAX_TITLE = 256
MAX_DESCRIPTION = 4096
MAX_FOOTER = 2048
MAX_TOTAL = 6000
MIN_SNOWFLAKE_LEN = 17
MAX_SNOWFLAKE_LEN = 20
MAX_UINT64 = 2**64 - 1
SERVER_RULES_HEX_COLOURS = {
    "#4472c4": SERVER_RULES_FAQ_BLUE,
    "#356854": SERVER_RULES_FAQ_GREEN,
    "#ffd666": SERVER_RULES_FAQ_YELLOW,
    "#607d8b": SERVER_RULES_FAQ_SLATE,
}


class FetchState(Enum):
    FOUND = "found"
    MISSING = "missing"
    UNKNOWN = "unknown"


class LegacyMarkerState(Enum):
    """Migration-only classification for markers emitted by older releases."""

    NONE = "none"
    VALID = "valid"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class LegacyMarkerResult:
    state: LegacyMarkerState
    key: str | None = None


@dataclass(frozen=True)
class RecoveryState:
    keep_id: str
    cleanup_ids: tuple[str, ...]


def _recovery_value(keep_id: str, cleanup_ids: list[str] | tuple[str, ...]) -> str:
    unique = tuple(dict.fromkeys(value for value in cleanup_ids if value != keep_id))
    if not unique:
        return keep_id
    return f"{RECOVERY_PREFIX}{keep_id or RECOVERY_EMPTY}:{','.join(unique)}"


def _parse_recovery(value: str) -> RecoveryState | None:
    if not value.startswith(RECOVERY_PREFIX):
        return None
    payload = value[len(RECOVERY_PREFIX) :]
    keep_raw, separator, cleanup_raw = payload.partition(":")
    if not separator:
        return None
    keep_id = "" if keep_raw == RECOVERY_EMPTY else keep_raw
    cleanup_ids = tuple(item for item in cleanup_raw.split(",") if item)
    all_ids = ((keep_id,) if keep_id else ()) + cleanup_ids
    if not cleanup_ids or any(not valid_snowflake(item) for item in all_ids):
        return None
    if len(set(all_ids)) != len(all_ids):
        return None
    return RecoveryState(keep_id, cleanup_ids)


def _is_recovery_artifact(value: str) -> bool:
    return value.startswith(RECOVERY_PREFIX)


@dataclass
class Row:
    row_number: int
    values: list[Any]
    data: dict[str, str]
    enabled: bool
    order: float | None = None
    topic_key: str = ""
    topic_title: str = ""
    embed: discord.Embed | None = None

    @property
    def key(self) -> str:
        return self.data.get("message_key", "").strip()

    @property
    def message_id(self) -> str:
        return self.data.get("message_id", "").strip()


@dataclass
class PendingRollback:
    row_number: int
    keep_id: str
    messages: list[Any]


_FAILSAFE_PENDING: list[PendingRollback] = []


@dataclass
class MessageGroup:
    key: str
    rows: list[Row]
    embeds: list[discord.Embed]
    stored_message_id: str = ""
    payload_embeds: list[discord.Embed] = field(default_factory=list)

    @property
    def first_row(self) -> Row:
        return self.rows[0]


@dataclass
class Summary:
    created: int = 0
    refreshed: int = 0
    removed: int = 0
    skipped: int = 0
    failed: int = 0
    failures: dict[str, list[str]] = field(default_factory=dict)

    def fail(self, key: str, reason: str) -> None:
        self.failed += 1
        self.failures.setdefault(key or "row", []).append(reason)


def _extract_legacy_marker(message: Any) -> tuple[bool, str | None]:
    content = getattr(message, "content", "") or ""
    if LEGACY_MARKER_PREFIX not in content:
        return False, None
    if not content.startswith(LEGACY_MARKER_PREFIX):
        return True, None
    rest = content[len(LEGACY_MARKER_PREFIX) :]
    for suffix in ("\u2060\u2063", "\n", " "):
        rest = rest.split(suffix, 1)[0]
    return True, rest or None


def _extract_hidden_marker(message: Any) -> tuple[bool, str | None]:
    embeds = getattr(message, "embeds", None) or []
    if not embeds:
        return False, None
    description = getattr(embeds[0], "description", None) or ""
    needle = f"[{HIDDEN_MARKER_LABEL}]({HIDDEN_MARKER_PREFIX}"
    pos = description.rfind(needle)
    if pos < 0:
        return (HIDDEN_MARKER_PREFIX in description), None
    start = pos + len(needle)
    end = description.find(")", start)
    if end < 0:
        return True, None
    encoded = description[start:end]
    decoded = unquote(encoded)
    if not decoded or quote(decoded, safe="") != encoded:
        return True, None
    return True, decoded


def _legacy_marker(message: Any) -> LegacyMarkerResult:
    """Parse old marker formats solely to migrate or clean existing posts.

    Marker artifacts are deliberately distinguished from markerless messages so
    corrupt or conflicting legacy data can never use the trusted stored-ID path.
    """

    visible_present, visible_key = _extract_legacy_marker(message)
    hidden_present, hidden_key = _extract_hidden_marker(message)
    if not visible_present and not hidden_present:
        return LegacyMarkerResult(LegacyMarkerState.NONE)
    if (visible_present and visible_key is None) or (
        hidden_present and hidden_key is None
    ):
        return LegacyMarkerResult(LegacyMarkerState.MALFORMED)
    keys = {key for key in (visible_key, hidden_key) if key is not None}
    if len(keys) != 1:
        return LegacyMarkerResult(LegacyMarkerState.MALFORMED)
    return LegacyMarkerResult(LegacyMarkerState.VALID, keys.pop())


def is_feature_message(message: Any, keys: set[str] | None = None) -> bool:
    marker = _legacy_marker(message)
    if marker.state is not LegacyMarkerState.VALID:
        return False
    return keys is None or marker.key in keys


def _embed_text_len(embed: discord.Embed) -> int:
    total = len(getattr(embed, "title", None) or "") + len(
        getattr(embed, "description", None) or ""
    )
    footer = getattr(embed, "footer", None)
    total += len(getattr(footer, "text", None) or "")
    for embed_field in getattr(embed, "fields", []) or []:
        total += len(getattr(embed_field, "name", "") or "") + len(
            getattr(embed_field, "value", "") or ""
        )
    return total


def _copy_embed(embed: discord.Embed) -> discord.Embed:
    return discord.Embed.from_dict(embed.to_dict())


def _clean_embed_payload(group: MessageGroup) -> list[discord.Embed]:
    """Copy rendered Sheet embeds without adding management metadata."""

    return [_copy_embed(embed) for embed in group.embeds]


def _validate_embed_payload(embeds: list[discord.Embed]) -> list[str]:
    errors: list[str] = []
    if len(embeds) > MAX_EMBEDS_PER_MESSAGE:
        errors.append("message group exceeds Discord's 10 embed limit")
    total = 0
    for embed in embeds:
        title = getattr(embed, "title", None) or ""
        description = getattr(embed, "description", None) or ""
        footer = getattr(getattr(embed, "footer", None), "text", None) or ""
        if len(title) > MAX_TITLE:
            errors.append("title exceeds Discord limit")
        if len(description) > MAX_DESCRIPTION:
            errors.append("description exceeds Discord limit")
        if len(footer) > MAX_FOOTER:
            errors.append("footer exceeds Discord limit")
        total += _embed_text_len(embed)
    if total > MAX_TOTAL:
        errors.append("message group embed text exceeds Discord 6000 character limit")
    return errors


def _mirralith_sheet_id() -> str:
    sheet_id = get_recruitment_sheet_id().strip()
    if not sheet_id:
        raise RuntimeError("RECRUITMENT_SHEET_ID not set for Mirralith spreadsheet")
    return sheet_id


def _a1_col(index: int) -> str:
    result = ""
    while index >= 0:
        index, rem = divmod(index, 26)
        result = chr(65 + rem) + result
        index -= 1
    return result


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


PLACEHOLDER_FIELDS = {
    "message_key",
    "section",
    "order",
    "title",
    "description",
    "colour",
    "thumbnail_url",
    "footer",
    "message_id",
    "topic_key",
    "topic_title",
    "review_status",
    "review_notes",
}


def _is_empty_placeholder(data: dict[str, str]) -> bool:
    return parse_enabled(data.get("enabled")) is False and all(
        not data.get(field, "").strip() for field in PLACEHOLDER_FIELDS
    )


def parse_enabled(value: Any) -> bool | None:
    text = _text(value).lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return None


def parse_colour(value: Any) -> discord.Colour | None:
    text = _text(value).lower()
    if not text or text in {"community", "c1c_blue", "blue"}:
        return get_embed_colour("community")
    if text in {"recruitment", "green"}:
        return get_embed_colour("recruitment")
    if text == "admin":
        return get_embed_colour("admin")
    if text == "theme_c1c_blue":
        return colors.c1c_blue
    if text == "theme_admin":
        return colors.admin
    if text in SERVER_RULES_HEX_COLOURS:
        return SERVER_RULES_HEX_COLOURS[text]
    return None


def valid_snowflake(value: str) -> bool:
    if not value or not value.isdecimal():
        return False
    if not MIN_SNOWFLAKE_LEN <= len(value) <= MAX_SNOWFLAKE_LEN:
        return False
    number = int(value)
    return 0 < number <= MAX_UINT64


def _valid_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def build_embed(row: Row) -> tuple[discord.Embed | None, list[str]]:
    errors: list[str] = []
    title = row.data.get("title", "").strip()
    description = row.data.get("description", "").strip()
    footer = row.data.get("footer", "").strip()
    thumbnail = row.data.get("thumbnail_url", "").strip()
    colour = parse_colour(row.data.get("colour", ""))
    if colour is None:
        errors.append("colour must be an approved palette name")
    if not title and not description:
        errors.append("embed needs a title or description")
    if len(title) > MAX_TITLE:
        errors.append("title exceeds Discord limit")
    if len(description) > MAX_DESCRIPTION:
        errors.append("description exceeds Discord limit")
    if len(footer) > MAX_FOOTER:
        errors.append("footer exceeds Discord limit")
    if thumbnail and not _valid_url(thumbnail):
        errors.append("thumbnail_url must be http or https")
    if len(title) + len(description) + len(footer) > MAX_TOTAL:
        errors.append("embed total text exceeds Discord limit")
    if errors:
        return None, errors
    embed = discord.Embed()
    embed.title = title or None
    embed.description = description or None
    embed.colour = colour
    if footer:
        embed.set_footer(text=footer)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    return embed, []


async def load_rows() -> tuple[str, dict[str, int], list[Row], list[str]]:
    tab = await recruitment_sheet.get_config_value_async(
        "SERVER_RULES_FAQ_TAB", None, force=True
    )
    if not tab:
        return "", {}, [], ["Config key SERVER_RULES_FAQ_TAB is missing"]
    matrix = await sheets_core.afetch_values(_mirralith_sheet_id(), tab)
    if not matrix:
        return tab, {}, [], ["sheet tab has no header row"]
    headers = [_text(value).lower() for value in matrix[0]]
    header_map = {header: idx for idx, header in enumerate(headers) if header}
    missing = sorted(REQUIRED_HEADERS - set(header_map))
    if missing:
        return tab, header_map, [], ["missing headers: " + ", ".join(missing)]
    rows: list[Row] = []
    for offset, values in enumerate(matrix[1:], start=2):
        data = {
            name: _text(values[idx]) if idx < len(values) else ""
            for name, idx in header_map.items()
        }
        if _is_empty_placeholder(data):
            continue
        row = Row(
            offset, list(values), data, parse_enabled(data.get("enabled")) is True
        )
        row.topic_key = data.get("topic_key", "").strip()
        row.topic_title = data.get("topic_title", "").strip()
        rows.append(row)
    return tab, header_map, rows, []


async def preflight(
    bot: discord.Client,
) -> tuple[Any | None, str, dict[str, int], list[Row], Summary | None]:
    summary = Summary()
    tab, header_map, rows, load_errors = await load_rows()
    if load_errors:
        for err in load_errors:
            summary.fail("config", err)
        return None, tab, header_map, rows, summary
    dest_raw = await recruitment_sheet.get_config_value_async(
        "SERVER_RULES_FAQ_CHANNEL_ID", None, force=True
    )
    try:
        dest_id = int(str(dest_raw or "").strip())
        target = await resolve_message_target(bot, dest_id)
    except Exception:
        summary.fail(
            "config",
            "SERVER_RULES_FAQ_CHANNEL_ID must resolve to a text channel or thread",
        )
        target = None
    enabled_orders: dict[float, str] = {}
    for row in rows:
        key = row.key
        label = key or f"row {row.row_number}"
        if not key:
            summary.fail(label, "message_key is required")
        if parse_enabled(row.data.get("enabled")) is None:
            summary.fail(label, "enabled value is not recognised")
        if row.message_id and not (
            valid_snowflake(row.message_id) or _parse_recovery(row.message_id)
        ):
            summary.fail(label, "message_id must be blank or a valid Discord snowflake")
        if row.enabled:
            order_text = row.data.get("order", "")
            try:
                order = float(order_text)
            except ValueError:
                summary.fail(label, "enabled rows require numeric order")
            else:
                if not math.isfinite(order):
                    summary.fail(label, "enabled rows require finite order")
                elif order in enabled_orders:
                    summary.fail(
                        label, f"enabled order duplicates {enabled_orders[order]}"
                    )
                else:
                    row.order = order
                    enabled_orders[order] = label
            row.embed, errors = build_embed(row)
            for err in errors:
                summary.fail(label, err)
    if not summary.failures:
        _groups, group_errors = build_groups(rows)
        for key, err in group_errors:
            summary.fail(key, err)
    return target, tab, header_map, rows, summary if summary.failures else None


def build_groups(rows: list[Row]) -> tuple[list[MessageGroup], list[tuple[str, str]]]:
    ordered = sorted(
        [r for r in rows if r.enabled], key=lambda r: (r.order, r.row_number)
    )
    topic_errors = _validate_topic_runs(ordered)
    grouped: dict[str, list[Row]] = {}
    for row in ordered:
        grouped.setdefault(row.key, []).append(row)
    groups = [
        MessageGroup(
            key, group_rows, [r.embed for r in group_rows if r.embed is not None]
        )
        for key, group_rows in grouped.items()
    ]
    groups.sort(key=lambda group: (group.rows[0].order, group.rows[0].row_number))
    errors: list[tuple[str, str]] = topic_errors
    for group in groups:
        ids = {
            row.message_id
            for row in group.rows
            if row.message_id and not _is_recovery_artifact(row.message_id)
        }
        if len(ids) > 1:
            errors.append(
                (group.key, "message group has multiple different stored message IDs")
            )
        group.stored_message_id = next(iter(ids), "")
        group.payload_embeds = _clean_embed_payload(group) if group.embeds else []
        errors.extend(
            (group.key, err) for err in _validate_embed_payload(group.payload_embeds)
        )
    return groups, errors


def _validate_topic_runs(rows: list[Row]) -> list[tuple[str, str]]:
    errors: list[tuple[str, str]] = []
    seen_closed: set[str] = set()
    active_topic = ""
    for row in rows:
        topic = row.topic_key
        if not topic:
            continue
        if topic == active_topic:
            continue
        if active_topic:
            seen_closed.add(active_topic)
        if topic in seen_closed:
            errors.append((topic, "topic_key rows must be consecutive by order"))
        active_topic = topic
    return errors


async def _worksheet(tab: str) -> Any:
    return await sheets_core.aget_worksheet(_mirralith_sheet_id(), tab)


def _message_id_range(header_map: dict[str, int], row_number: int) -> str:
    return f"{_a1_col(header_map['message_id'])}{row_number}"


async def _write_message_id(
    tab: str, header_map: dict[str, int], row: Row, message_id: str
) -> None:
    ws = await _worksheet(tab)
    await async_adapter.aworksheet_values_update(
        ws, _message_id_range(header_map, row.row_number), [[message_id]]
    )


def _batch_payload(
    header_map: dict[str, int], updates: list[tuple[Row, str]]
) -> list[dict[str, Any]]:
    return [
        {
            "range": _message_id_range(header_map, row.row_number),
            "values": [[message_id]],
        }
        for row, message_id in updates
    ]


async def _write_ids_batch(
    tab: str, header_map: dict[str, int], updates: list[tuple[Row, str]]
) -> None:
    if not updates:
        return
    ws = await _worksheet(tab)
    await sheets_core.acall_with_backoff(
        ws.batch_update, _batch_payload(header_map, updates)
    )


async def _fetch(target: Any, message_id: str) -> tuple[FetchState, Any | None, str]:
    if not message_id or not valid_snowflake(message_id):
        return FetchState.MISSING, None, "message_id is blank or invalid"
    try:
        return FetchState.FOUND, await target.fetch_message(int(message_id)), ""
    except discord.NotFound:
        return FetchState.MISSING, None, "stored message was not found"
    except discord.Forbidden:
        return FetchState.UNKNOWN, None, "missing permission to fetch stored message"
    except (TimeoutError, OSError):
        return FetchState.UNKNOWN, None, "temporary error while fetching stored message"
    except discord.HTTPException:
        return FetchState.UNKNOWN, None, "Discord error while fetching stored message"
    except Exception:
        return (
            FetchState.UNKNOWN,
            None,
            "unexpected error while fetching stored message",
        )


def _is_bot_authored(message: Any, bot_id: int | None) -> bool:
    return (
        bot_id is not None
        and getattr(getattr(message, "author", None), "id", None) == bot_id
    )


def _is_valid_stored_message(
    message: Any,
    *,
    stored_message_id: str,
    target: Any,
    bot_id: int | None,
    permitted_legacy_keys: set[str],
) -> bool:
    """Verify an exact stored post, accepting legacy markers only for migration."""

    if str(getattr(message, "id", "")) != stored_message_id:
        return False
    if getattr(getattr(message, "channel", None), "id", None) != getattr(
        target, "id", None
    ):
        return False
    if not _is_bot_authored(message, bot_id):
        return False
    embeds = list(getattr(message, "embeds", None) or [])
    if not 1 <= len(embeds) <= MAX_EMBEDS_PER_MESSAGE:
        return False
    if _validate_embed_payload(embeds):
        return False
    marker = _legacy_marker(message)
    if marker.state is LegacyMarkerState.MALFORMED:
        return False
    if marker.state is LegacyMarkerState.VALID:
        return marker.key in permitted_legacy_keys
    return not (getattr(message, "content", "") or "")


def _is_deletable_feature_message(
    message: Any, bot_id: int | None, keys: set[str] | None = None
) -> bool:
    return _is_bot_authored(message, bot_id) and is_feature_message(message, keys)


def _cleanup_keys_for_row(row: Row) -> set[str]:
    keys = {row.key}
    if row.topic_key:
        keys.add(row.topic_key)
    return keys


async def _iter_feature_messages(target: Any, bot_id: int | None) -> list[Any]:
    history = getattr(target, "history", None)
    if not callable(history):
        return []
    found: list[Any] = []
    async for message in history(limit=500):
        if _is_deletable_feature_message(message, bot_id):
            found.append(message)
    return found


async def _delete_new_survivors(messages: list[Any]) -> list[Any]:
    survivors: list[Any] = []
    for sent in messages:
        try:
            await sent.delete()
        except discord.NotFound:
            continue
        except Exception:
            survivors.append(sent)
    return survivors


async def _delete_new(messages: list[Any]) -> bool:
    return not await _delete_new_survivors(messages)


async def _persist_rollback_journal(
    tab: str,
    header_map: dict[str, int],
    pairs: list[tuple[MessageGroup, Any]],
) -> list[tuple[Row, str]]:
    updates = [
        (
            group.first_row,
            _recovery_value(group.stored_message_id, [str(message.id)]),
        )
        for group, message in pairs
    ]
    await _write_ids_batch(tab, header_map, updates)
    for row, value in updates:
        row.data["message_id"] = value
    return updates


async def _rollback_replacements(
    target: Any,
    tab: str,
    header_map: dict[str, int],
    rows: list[Row],
    pairs: list[tuple[MessageGroup, Any]],
    bot_id: int | None,
    summary: Summary,
) -> None:
    """Delete or durably journal only replacements created by this mutation."""

    if not pairs:
        return
    try:
        await _persist_rollback_journal(tab, header_map, pairs)
    except Exception:
        summary.fail("sheet", "failed to journal replacement rollback")
    else:
        await _recover_pending(target, tab, header_map, rows, bot_id, summary)
        return

    survivors = await _delete_new_survivors([message for _group, message in pairs])
    if not survivors:
        return
    survivor_ids = {str(message.id) for message in survivors}
    survivor_pairs = [
        (group, message) for group, message in pairs if str(message.id) in survivor_ids
    ]
    try:
        await _persist_rollback_journal(tab, header_map, survivor_pairs)
    except Exception:
        summary.fail("sheet", "failed to journal replacement rollback survivors")
        for group, message in survivor_pairs:
            _FAILSAFE_PENDING.append(
                PendingRollback(
                    group.first_row.row_number,
                    group.stored_message_id,
                    [message],
                )
            )


async def _resolve_failsafe_pending(
    target: Any,
    tab: str,
    header_map: dict[str, int],
    rows: list[Row],
    summary: Summary,
) -> bool:
    """Block mutations until in-memory rollback survivors are deleted or journalled."""

    if not _FAILSAFE_PENDING:
        return True
    by_number = {row.row_number: row for row in rows}
    unresolved: list[PendingRollback] = []
    for pending in list(_FAILSAFE_PENDING):
        survivors = await _delete_new_survivors(pending.messages)
        if not survivors:
            continue
        row = by_number.get(pending.row_number)
        if row is None:
            pending.messages = survivors
            unresolved.append(pending)
            continue
        value = _recovery_value(
            pending.keep_id, [str(message.id) for message in survivors]
        )
        try:
            await _write_ids_batch(tab, header_map, [(row, value)])
        except Exception:
            pending.messages = survivors
            unresolved.append(pending)
        else:
            row.data["message_id"] = value
    _FAILSAFE_PENDING[:] = unresolved
    if unresolved:
        summary.fail("rollback", "server-rules rollback survivors remain pending")
        return False
    return True


async def _recover_pending(
    target: Any,
    tab: str,
    header_map: dict[str, int],
    rows: list[Row],
    bot_id: int | None,
    summary: Summary,
) -> bool:
    """Resume exact-ID cleanup recorded in message_id cells."""

    updates: list[tuple[Row, str]] = []
    complete = True
    for row in rows:
        recovery = _parse_recovery(row.message_id)
        if recovery is None:
            continue
        remaining: list[str] = []
        for message_id in recovery.cleanup_ids:
            state, message, reason = await _fetch(target, message_id)
            if state is FetchState.MISSING:
                continue
            if state is FetchState.UNKNOWN:
                summary.fail(row.key, reason)
                remaining.append(message_id)
                complete = False
                continue
            if message is None or not _is_valid_stored_message(
                message,
                stored_message_id=message_id,
                target=target,
                bot_id=bot_id,
                permitted_legacy_keys=_cleanup_keys_for_row(row),
            ):
                marker = _legacy_marker(message) if message is not None else None
                if not (
                    message is not None
                    and _is_bot_authored(message, bot_id)
                    and marker is not None
                    and marker.state is LegacyMarkerState.VALID
                ):
                    summary.fail(
                        row.key, "recovery message failed stored-ID verification"
                    )
                    remaining.append(message_id)
                    complete = False
                continue
            try:
                await message.delete()
            except Exception:
                summary.fail(row.key, "failed to delete stored recovery message")
                remaining.append(message_id)
                complete = False
            else:
                summary.removed += 1
        value = (
            _recovery_value(recovery.keep_id, remaining)
            if remaining
            else recovery.keep_id
        )
        updates.append((row, value))
    if updates:
        try:
            await _write_ids_batch(tab, header_map, updates)
        except Exception:
            summary.fail("sheet", "failed to persist server-rules recovery progress")
            return False
        for row, value in updates:
            row.data["message_id"] = value
    return complete


async def _publish(bot: discord.Client) -> tuple[Summary, Any | None]:
    target, tab, header_map, rows, errors = await preflight(bot)
    if errors:
        return errors, target
    assert target is not None
    summary = Summary()
    bot_id = getattr(getattr(bot, "user", None), "id", None)
    if not await _resolve_failsafe_pending(target, tab, header_map, rows, summary):
        return summary, target
    if not await _recover_pending(target, tab, header_map, rows, bot_id, summary):
        return summary, target
    groups, _ = build_groups(rows)
    new_pairs: list[tuple[MessageGroup, Any]] = []
    try:
        for group in groups:
            new_pairs.append(
                (group, await target.send(content=None, embeds=group.payload_embeds))
            )
    except Exception:
        summary.fail(group.key, "Discord send failed during rebuild")
        await _rollback_replacements(
            target, tab, header_map, rows, new_pairs, bot_id, summary
        )
        return summary, target
    updates: list[tuple[Row, str]] = []
    for group, msg in new_pairs:
        old_ids = [row.message_id for row in group.rows if row.message_id]
        updates.append(
            (
                group.first_row,
                _recovery_value(str(msg.id), old_ids),
            )
        )
        updates.extend((row, "") for row in group.rows[1:] if row.message_id)
    updates.extend(
        (row, _recovery_value("", [row.message_id]))
        for row in rows
        if not row.enabled and row.message_id
    )
    try:
        await _write_ids_batch(tab, header_map, updates)
    except Exception:
        summary.fail(
            "sheet",
            "failed to journal replacement messages during rebuild",
        )
        await _rollback_replacements(
            target, tab, header_map, rows, new_pairs, bot_id, summary
        )
        return summary, target
    for row, value in updates:
        row.data["message_id"] = value
    if not await _recover_pending(target, tab, header_map, rows, bot_id, summary):
        return summary, target
    summary.created = len(new_pairs)
    new_ids = {str(getattr(msg, "id", "")) for _group, msg in new_pairs}
    seen_old_ids: set[str] = set()
    try:
        old_messages = await _iter_feature_messages(target, bot_id)
    except Exception:
        summary.fail("old messages", "failed to scan channel history after rebuild")
    else:
        for msg in old_messages:
            msg_id = str(getattr(msg, "id", ""))
            if (
                msg_id in new_ids
                or msg_id in seen_old_ids
                or getattr(msg, "deleted", False)
            ):
                continue
            try:
                await msg.delete()
                summary.removed += 1
            except Exception:
                summary.fail(
                    "old messages", "failed to remove old managed message after rebuild"
                )
    return summary, target


async def publish(bot: discord.Client) -> tuple[Summary, Any | None]:
    async with MUTATION_LOCK:
        return await _publish(bot)


async def _refresh(bot: discord.Client) -> tuple[Summary, Any | None]:
    target, tab, header_map, rows, errors = await preflight(bot)
    if errors:
        return errors, target
    assert target is not None
    summary = Summary()
    bot_id = getattr(getattr(bot, "user", None), "id", None)
    if not await _resolve_failsafe_pending(target, tab, header_map, rows, summary):
        return summary, target
    if not await _recover_pending(target, tab, header_map, rows, bot_id, summary):
        return summary, target
    groups, _ = build_groups(rows)
    grouped_rows = {id(row) for group in groups for row in group.rows}
    for group in groups:
        row = group.first_row
        try:
            state, msg, reason = await _fetch(target, group.stored_message_id)
            if state is FetchState.UNKNOWN:
                summary.fail(row.key, reason)
                continue
            if (
                state is FetchState.FOUND
                and msg is not None
                and _is_valid_stored_message(
                    msg,
                    stored_message_id=group.stored_message_id,
                    target=target,
                    bot_id=getattr(getattr(bot, "user", None), "id", None),
                    permitted_legacy_keys={group.key}
                    | {row.topic_key for row in group.rows if row.topic_key},
                )
            ):
                try:
                    await msg.edit(content=None, embeds=group.payload_embeds)
                except Exception:
                    summary.fail(row.key, "failed to edit stored message")
                else:
                    summary.refreshed += 1
                continue
            summary.skipped += 1
        except Exception:
            summary.fail(row.key, "unexpected row processing failure")
    for row in rows:
        if id(row) in grouped_rows:
            continue
        try:
            state, msg, reason = await _fetch(target, row.message_id)
            if not row.message_id:
                summary.skipped += 1
            elif state is FetchState.UNKNOWN:
                summary.fail(row.key, reason)
            elif state is FetchState.MISSING:
                try:
                    await _write_message_id(tab, header_map, row, "")
                except Exception:
                    summary.fail(
                        row.key,
                        "stored message was missing but message_id could not be cleared",
                    )
                else:
                    summary.skipped += 1
            elif msg is not None and _is_valid_stored_message(
                msg,
                stored_message_id=row.message_id,
                target=target,
                bot_id=getattr(getattr(bot, "user", None), "id", None),
                permitted_legacy_keys=_cleanup_keys_for_row(row),
            ):
                try:
                    await msg.delete()
                    await _write_message_id(tab, header_map, row, "")
                except Exception:
                    summary.fail(
                        row.key, "failed to delete disabled message or clear message_id"
                    )
                else:
                    summary.removed += 1
            else:
                summary.fail(row.key, "stored message is not managed by server rules")
        except Exception:
            summary.fail(row.key, "unexpected row processing failure")
    return summary, target


async def refresh(bot: discord.Client) -> tuple[Summary, Any | None]:
    async with MUTATION_LOCK:
        return await _refresh(bot)


def result_embed(action: str, summary: Summary) -> discord.Embed:
    embed = discord.Embed(
        title=f"Server rules {action}", colour=get_embed_colour("admin")
    )
    embed.add_field(name="Created", value=str(summary.created), inline=True)
    embed.add_field(name="Refreshed", value=str(summary.refreshed), inline=True)
    embed.add_field(name="Removed", value=str(summary.removed), inline=True)
    embed.add_field(name="Skipped", value=str(summary.skipped), inline=True)
    embed.add_field(name="Failed", value=str(summary.failed), inline=True)
    if summary.failures:
        details = [
            f"{key}: {'; '.join(reasons)}" for key, reasons in summary.failures.items()
        ]
        embed.add_field(
            name="Failure details", value="\n".join(details)[:1024], inline=False
        )
    return embed
