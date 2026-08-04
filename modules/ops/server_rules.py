"""Sheet-driven Server Rules and FAQ publisher."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import discord

from modules.common.discord_utils import resolve_message_target
from modules.common.embeds import get_embed_colour
from shared.sheets import async_adapter
from shared.sheets import core as sheets_core
from shared.sheets import recruitment as recruitment_sheet
from shared.theme import colors

FEATURE = "serverrules"
MARKER_PREFIX = "\u2063\u200bserverrules:"
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
TRUE_VALUES = {"1", "true", "yes", "y", "on", "enabled", "publish", "published"}
FALSE_VALUES = {"", "0", "false", "no", "n", "off", "disabled"}
MAX_TITLE = 256
MAX_DESCRIPTION = 4096
MAX_FOOTER = 2048
MAX_TOTAL = 6000


@dataclass
class Row:
    row_number: int
    values: list[Any]
    data: dict[str, str]
    enabled: bool
    order: float | None = None
    embed: discord.Embed | None = None

    @property
    def key(self) -> str:
        return self.data.get("message_key", "").strip()

    @property
    def message_id(self) -> str:
        return self.data.get("message_id", "").strip()


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
    return f"{MARKER_PREFIX}{message_key}\u2060\u2063"


def is_feature_message(message: Any, keys: set[str] | None = None) -> bool:
    content = getattr(message, "content", "") or ""
    if not content.startswith(MARKER_PREFIX):
        return False
    if keys is None:
        return True
    return any(content.startswith(marker_for(key)) for key in keys)


def _a1_col(index: int) -> str:
    result = ""
    while index >= 0:
        index, rem = divmod(index, 26)
        result = chr(65 + rem) + result
        index -= 1
    return result


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def parse_enabled(value: Any) -> bool | None:
    text = _text(value).lower()
    if text in TRUE_VALUES:
        return True
    if text in FALSE_VALUES:
        return False
    return None


def parse_colour(value: Any) -> discord.Colour | None:
    text = _text(value)
    if not text:
        return get_embed_colour("community")
    lowered = text.lower()
    if lowered in {"community", "blue", "c1c_blue"}:
        return colors.c1c_blue
    if lowered == "admin":
        return colors.admin
    if text.startswith("#"):
        text = text[1:]
    if text.lower().startswith("0x"):
        text = text[2:]
    try:
        return discord.Colour(int(text, 16))
    except ValueError:
        return None


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
        errors.append("colour is not parseable")
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
    embed = discord.Embed(
        title=title or None, description=description or None, colour=colour
    )
    if footer:
        embed.set_footer(text=footer)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    return embed, []


async def load_rows() -> (
    tuple[str, list[list[Any]], dict[str, int], list[Row], list[str]]
):
    tab = await recruitment_sheet.get_config_value_async(
        "SERVER_RULES_FAQ_TAB", None, force=True
    )
    if not tab:
        return "", [], {}, [], ["Config key SERVER_RULES_FAQ_TAB is missing"]
    sheet_id = recruitment_sheet.get_recruitment_sheet_id()
    matrix = await sheets_core.afetch_values(sheet_id, tab)
    if not matrix:
        return tab, matrix, {}, [], ["sheet tab has no header row"]
    headers = [_text(value).lower() for value in matrix[0]]
    header_map = {header: idx for idx, header in enumerate(headers) if header}
    missing = sorted(REQUIRED_HEADERS - set(header_map))
    if missing:
        return tab, matrix, header_map, [], ["missing headers: " + ", ".join(missing)]
    rows: list[Row] = []
    for offset, values in enumerate(matrix[1:], start=2):
        data = {
            name: _text(values[idx]) if idx < len(values) else ""
            for name, idx in header_map.items()
        }
        enabled = parse_enabled(data.get("enabled"))
        rows.append(Row(offset, list(values), data, bool(enabled)))
    return tab, matrix, header_map, rows, []


async def preflight(
    bot: discord.Client,
) -> tuple[Any | None, str, dict[str, int], list[Row], Summary | None]:
    summary = Summary()
    tab, _matrix, header_map, rows, load_errors = await load_rows()
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
    seen: set[str] = set()
    for row in rows:
        key = row.key
        if not key:
            summary.fail(f"row {row.row_number}", "message_key is required")
        elif key in seen:
            summary.fail(key, "message_key must be unique")
        seen.add(key)
        enabled_raw = row.data.get("enabled")
        if parse_enabled(enabled_raw) is None:
            summary.fail(key, "enabled value is not recognised")
        try:
            row.order = float(row.data.get("order", ""))
        except ValueError:
            summary.fail(key, "order must be numeric")
        if row.message_id and not row.message_id.isdigit():
            summary.fail(key, "message_id must be blank or a Discord snowflake")
        if row.enabled:
            row.embed, errors = build_embed(row)
            for err in errors:
                summary.fail(key, err)
    return target, tab, header_map, rows, summary if summary.failures else None


async def _write_message_id(
    tab: str, header_map: dict[str, int], row: Row, message_id: str
) -> None:
    sheet_id = recruitment_sheet.get_recruitment_sheet_id()
    ws = await sheets_core.aget_worksheet(sheet_id, tab)
    col = _a1_col(header_map["message_id"])
    await async_adapter.aworksheet_values_update(
        ws, f"{col}{row.row_number}", [[message_id]]
    )


async def _fetch(target: Any, message_id: str) -> Any | None:
    if not message_id or not message_id.isdigit():
        return None
    try:
        return await target.fetch_message(int(message_id))
    except Exception:
        return None


async def _iter_feature_messages(
    target: Any, bot_id: int | None, keys: set[str] | None = None
) -> list[Any]:
    history = getattr(target, "history", None)
    if not callable(history):
        return []
    found: list[Any] = []
    async for message in history(limit=500):
        if (
            bot_id is not None
            and getattr(getattr(message, "author", None), "id", None) != bot_id
        ):
            continue
        if is_feature_message(message, keys):
            found.append(message)
    return found


async def publish(bot: discord.Client) -> tuple[Summary, Any | None]:
    target, tab, header_map, rows, errors = await preflight(bot)
    if errors:
        return errors, target
    assert target is not None
    summary = Summary()
    enabled_rows = sorted(
        [r for r in rows if r.enabled], key=lambda r: (r.order, r.key)
    )
    old_ids = {r.message_id for r in rows if r.message_id}
    new_pairs: list[tuple[Row, Any]] = []
    try:
        for row in enabled_rows:
            sent = await target.send(content=marker_for(row.key), embed=row.embed)
            new_pairs.append((row, sent))
    except Exception:
        summary.fail(row.key, "Discord send failed during rebuild")
        for _row, sent in new_pairs:
            try:
                await sent.delete()
            except Exception:
                pass
        return summary, target
    try:
        for row, sent in new_pairs:
            await _write_message_id(tab, header_map, row, str(sent.id))
            summary.created += 1
    except Exception:
        summary.fail(row.key, "Sheets message_id update failed during rebuild")
        for _row, sent in new_pairs:
            try:
                await sent.delete()
            except Exception:
                pass
        return summary, target
    bot_id = getattr(getattr(bot, "user", None), "id", None)
    for msg in await _iter_feature_messages(target, bot_id):
        if str(getattr(msg, "id", "")) in {
            str(getattr(m, "id", "")) for _r, m in new_pairs
        }:
            continue
        if str(getattr(msg, "id", "")) in old_ids or is_feature_message(msg):
            try:
                await msg.delete()
                summary.removed += 1
            except Exception:
                summary.fail("old messages", "failed to remove old managed message")
    return summary, target


async def refresh(bot: discord.Client) -> tuple[Summary, Any | None]:
    target, tab, header_map, rows, errors = await preflight(bot)
    if errors:
        return errors, target
    assert target is not None
    summary = Summary()
    for row in rows:
        msg = await _fetch(target, row.message_id)
        if row.enabled:
            if msg is not None and is_feature_message(msg, {row.key}):
                await msg.edit(content=marker_for(row.key), embed=row.embed)
                summary.refreshed += 1
            else:
                sent = await target.send(content=marker_for(row.key), embed=row.embed)
                await _write_message_id(tab, header_map, row, str(sent.id))
                summary.created += 1
        else:
            if msg is not None and is_feature_message(msg, {row.key}):
                await msg.delete()
                await _write_message_id(tab, header_map, row, "")
                summary.removed += 1
            elif row.message_id:
                await _write_message_id(tab, header_map, row, "")
                summary.skipped += 1
            else:
                summary.skipped += 1
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
        details = []
        for key, reasons in summary.failures.items():
            details.append(f"{key}: {'; '.join(reasons)}")
        embed.add_field(
            name="Failure details", value="\n".join(details)[:1024], inline=False
        )
    return embed
