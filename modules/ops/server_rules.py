"""Sheet-driven Server Rules and FAQ publisher."""

from __future__ import annotations

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
class MessageGroup:
    key: str
    rows: list[Row]
    embeds: list[discord.Embed]
    stored_message_id: str = ""

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


def marker_for(message_key: str) -> str:
    return f"{LEGACY_MARKER_PREFIX}{message_key}\u2060\u2063"


def hidden_marker_for(message_key: str) -> str:
    return (
        f"[{HIDDEN_MARKER_LABEL}]({HIDDEN_MARKER_PREFIX}{quote(message_key, safe='')})"
    )


def _extract_legacy_marker(message: Any) -> str | None:
    content = getattr(message, "content", "") or ""
    if not content.startswith(LEGACY_MARKER_PREFIX):
        return None
    rest = content[len(LEGACY_MARKER_PREFIX) :]
    for suffix in ("\u2060\u2063", "\n", " "):
        rest = rest.split(suffix, 1)[0]
    return rest or None


def _extract_hidden_marker(message: Any) -> str | None:
    embeds = getattr(message, "embeds", None) or []
    if not embeds:
        return None
    description = getattr(embeds[0], "description", None) or ""
    needle = f"[{HIDDEN_MARKER_LABEL}]({HIDDEN_MARKER_PREFIX}"
    pos = description.rfind(needle)
    if pos < 0:
        return None
    start = pos + len(needle)
    end = description.find(")", start)
    if end < 0:
        return None
    encoded = description[start:end]
    decoded = unquote(encoded)
    if not decoded or quote(decoded, safe="") != encoded:
        return None
    return decoded


def _managed_message_key(message: Any) -> str | None:
    return _extract_hidden_marker(message) or _extract_legacy_marker(message)


def is_feature_message(message: Any, keys: set[str] | None = None) -> bool:
    key = _managed_message_key(message)
    if key is None:
        return False
    return keys is None or key in keys


def _is_managed_message(
    message: Any, bot_id: int | None = None, expected_key: str | None = None
) -> bool:
    if bot_id is not None and not _is_bot_authored(message, bot_id):
        return False
    key = _managed_message_key(message)
    if key is None:
        return False
    return expected_key is None or key == expected_key


def _embed_text_len(embed: discord.Embed) -> int:
    total = len(getattr(embed, "title", None) or "") + len(
        getattr(embed, "description", None) or ""
    )
    footer = getattr(embed, "footer", None)
    total += len(getattr(footer, "text", None) or "")
    for field in getattr(embed, "fields", []) or []:
        total += len(getattr(field, "name", "") or "") + len(
            getattr(field, "value", "") or ""
        )
    return total


def _copy_embed(embed: discord.Embed) -> discord.Embed:
    return discord.Embed.from_dict(embed.to_dict())


def _embeds_with_marker(group: MessageGroup) -> list[discord.Embed]:
    embeds = [_copy_embed(embed) for embed in group.embeds]
    embeds[0].description = (
        f"{embeds[0].description or ''}{hidden_marker_for(group.key)}"
    )
    return embeds


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
        if row.message_id and not valid_snowflake(row.message_id):
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
    errors: list[tuple[str, str]] = []
    for group in groups:
        if len(group.embeds) > MAX_EMBEDS_PER_MESSAGE:
            errors.append((group.key, "message group exceeds Discord's 10 embed limit"))
        ids = {row.message_id for row in group.rows if row.message_id}
        if len(ids) > 1:
            errors.append(
                (group.key, "message group has multiple different stored message IDs")
            )
        group.stored_message_id = next(iter(ids), "")
        marked = _embeds_with_marker(group) if group.embeds else []
        if sum(_embed_text_len(embed) for embed in marked) > MAX_TOTAL:
            errors.append(
                (
                    group.key,
                    "message group embed text exceeds Discord 6000 character limit",
                )
            )
    return groups, errors


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


async def _restore_ids_batch(
    tab: str, header_map: dict[str, int], snapshot: dict[int, str]
) -> None:
    rows = [Row(row_number, [], {}, False) for row_number in snapshot]
    await _write_ids_batch(
        tab, header_map, [(row, value) for row, value in zip(rows, snapshot.values())]
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


def _is_deletable_feature_message(
    message: Any, bot_id: int | None, keys: set[str] | None = None
) -> bool:
    return _is_bot_authored(message, bot_id) and is_feature_message(message, keys)


async def _iter_feature_messages(target: Any, bot_id: int | None) -> list[Any]:
    history = getattr(target, "history", None)
    if not callable(history):
        return []
    found: list[Any] = []
    async for message in history(limit=500):
        if _is_deletable_feature_message(message, bot_id):
            found.append(message)
    return found


async def _delete_old_stored_messages(
    target: Any,
    rows: list[Row],
    new_ids: set[str],
    bot_id: int | None,
    summary: Summary,
) -> set[str]:
    seen: set[str] = set()
    for row in rows:
        old_id = row.message_id
        if not old_id or old_id in new_ids or old_id in seen:
            continue
        seen.add(old_id)
        state, msg, reason = await _fetch(target, old_id)
        if state is FetchState.MISSING:
            continue
        if state is FetchState.UNKNOWN:
            summary.fail(row.key, reason)
            continue
        if msg is None or not _is_deletable_feature_message(msg, bot_id, {row.key}):
            continue
        try:
            await msg.delete()
            summary.removed += 1
        except Exception:
            summary.fail(row.key, "failed to remove old managed message after rebuild")
    return seen


async def _delete_new(messages: list[Any]) -> None:
    for sent in messages:
        try:
            await sent.delete()
        except Exception:
            pass


async def publish(bot: discord.Client) -> tuple[Summary, Any | None]:
    target, tab, header_map, rows, errors = await preflight(bot)
    if errors:
        return errors, target
    assert target is not None
    summary = Summary()
    snapshot = {row.row_number: row.message_id for row in rows}
    groups, _ = build_groups(rows)
    new_pairs: list[tuple[MessageGroup, Any]] = []
    try:
        for group in groups:
            new_pairs.append(
                (group, await target.send(embeds=_embeds_with_marker(group)))
            )
    except Exception:
        summary.fail(group.key, "Discord send failed during rebuild")
        await _delete_new([msg for _group, msg in new_pairs])
        return summary, target
    updates: list[tuple[Row, str]] = []
    for group, msg in new_pairs:
        updates.append((group.first_row, str(msg.id)))
        updates.extend((row, "") for row in group.rows[1:] if row.message_id)
    updates.extend((row, "") for row in rows if not row.enabled and row.message_id)
    try:
        await _write_ids_batch(tab, header_map, updates)
        summary.created = len(new_pairs)
    except Exception:
        summary.fail(
            "sheet",
            "message_id update failed during rebuild; original IDs were restored where possible",
        )
        try:
            await _restore_ids_batch(tab, header_map, snapshot)
        except Exception:
            summary.fail("sheet", "failed to restore original message_id values")
        await _delete_new([msg for _row, msg in new_pairs])
        return summary, target
    for row in rows:
        if not row.enabled and row.message_id:
            summary.removed += 0
    new_ids = {str(getattr(msg, "id", "")) for _group, msg in new_pairs}
    bot_id = getattr(getattr(bot, "user", None), "id", None)
    seen_old_ids = await _delete_old_stored_messages(
        target, rows, new_ids, bot_id, summary
    )
    try:
        old_messages = await _iter_feature_messages(target, bot_id)
    except Exception:
        summary.fail("old messages", "failed to scan channel history after rebuild")
    else:
        for msg in old_messages:
            msg_id = str(getattr(msg, "id", ""))
            if msg_id in new_ids or msg_id in seen_old_ids:
                continue
            try:
                await msg.delete()
                summary.removed += 1
            except Exception:
                summary.fail(
                    "old messages", "failed to remove old managed message after rebuild"
                )
    return summary, target


async def refresh(bot: discord.Client) -> tuple[Summary, Any | None]:
    target, tab, header_map, rows, errors = await preflight(bot)
    if errors:
        return errors, target
    assert target is not None
    summary = Summary()
    groups, _ = build_groups(rows)
    grouped_rows = {id(row) for group in groups for row in group.rows}
    for group in groups:
        row = group.first_row
        try:
            state, msg, reason = await _fetch(target, group.stored_message_id)
            if True:
                if state is FetchState.UNKNOWN:
                    summary.fail(row.key, reason)
                    continue
                if (
                    state is FetchState.FOUND
                    and msg is not None
                    and _is_managed_message(
                        msg, getattr(getattr(bot, "user", None), "id", None), group.key
                    )
                ):
                    try:
                        await msg.edit(content=None, embeds=_embeds_with_marker(group))
                    except Exception:
                        summary.fail(row.key, "failed to edit stored message")
                    else:
                        summary.refreshed += 1
                    continue
                summary.skipped += 1
            else:
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
                elif msg is not None and _is_deletable_feature_message(
                    msg, getattr(getattr(bot, "user", None), "id", None), {row.key}
                ):
                    try:
                        await msg.delete()
                        await _write_message_id(tab, header_map, row, "")
                    except Exception:
                        summary.fail(
                            row.key,
                            "failed to delete disabled message or clear message_id",
                        )
                    else:
                        summary.removed += 1
                else:
                    summary.fail(
                        row.key, "stored message is not managed by server rules"
                    )
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
            elif msg is not None and _is_deletable_feature_message(
                msg, getattr(getattr(bot, "user", None), "id", None), {row.key}
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
