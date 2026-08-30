"""Automatic revision metadata for the sheet-driven Server Rules/FAQ feature."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import discord

from modules.ops import server_rules as base
import modules.ops.server_rules_interactive as interactive

REVISION_HEADERS = {"last_updated", "content_hash"}
HASH_FIELDS = (
    "message_key",
    "section",
    "order",
    "enabled",
    "title",
    "description",
    "colour",
    "thumbnail_url",
    "footer",
    "topic_key",
    "topic_title",
)
LAST_UPDATED_PREFIX = "Last updated: "
_DATE_RE = re.compile(r"^\d{4}-[A-Z][a-z]+-\d{2}$")

_ORIGINAL_BUILD_EMBED = base.build_embed
_ORIGINAL_QUESTION_LIST = interactive._question_list
_INSTALLED = False


@dataclass(frozen=True)
class RevisionUpdate:
    row: base.Row
    last_updated: str
    content_hash: str


def _date_value(value: str) -> datetime | None:
    text = (value or "").strip()
    if not text or not _DATE_RE.fullmatch(text):
        return None
    try:
        parsed = datetime.strptime(text, "%Y-%B-%d")
    except ValueError:
        return None
    return parsed if parsed.strftime("%Y-%B-%d") == text else None


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%B-%d")


def _content_hash(row: base.Row) -> str:
    payload = [(field, row.data.get(field, "").strip()) for field in HASH_FIELDS]
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _managed_rows(rows: list[base.Row]) -> list[base.Row]:
    return [row for row in rows if row.key]


def _plan_updates(
    rows: list[base.Row], *, today: str | None = None
) -> tuple[list[RevisionUpdate], list[tuple[str, str]]]:
    today = today or _today_utc()
    if _date_value(today) is None:
        raise ValueError("today must use YYYY-MMMM-DD")

    updates: list[RevisionUpdate] = []
    errors: list[tuple[str, str]] = []
    for row in _managed_rows(rows):
        label = row.key or f"row {row.row_number}"
        stored_date = row.data.get("last_updated", "").strip()
        stored_hash = row.data.get("content_hash", "").strip()
        current_hash = _content_hash(row)

        if stored_date and _date_value(stored_date) is None:
            errors.append(
                (label, "last_updated must use YYYY-MMMM-DD, for example 2026-August-30")
            )
            continue
        if stored_hash and not re.fullmatch(r"[0-9a-f]{64}", stored_hash):
            errors.append((label, "content_hash must be blank or a lowercase SHA-256 hash"))
            continue

        if not stored_hash:
            # Baseline migration: preserve an existing valid date and only seed the hash.
            updates.append(
                RevisionUpdate(row, stored_date or today, current_hash)
            )
        elif stored_hash != current_hash:
            updates.append(RevisionUpdate(row, today, current_hash))

    return updates, errors


def _revision_footer(existing: str, last_updated: str) -> str:
    stamp = f"{LAST_UPDATED_PREFIX}{last_updated}"
    return f"{existing} • {stamp}" if existing else stamp


def build_embed(row: base.Row) -> tuple[discord.Embed | None, list[str]]:
    """Render the normal Sheet embed and append its revision stamp when present."""

    embed, errors = _ORIGINAL_BUILD_EMBED(row)
    if embed is None or errors:
        return embed, errors

    last_updated = row.data.get("last_updated", "").strip()
    if not last_updated:
        return embed, []
    if _date_value(last_updated) is None:
        return None, [
            "last_updated must use YYYY-MMMM-DD, for example 2026-August-30"
        ]

    existing = getattr(getattr(embed, "footer", None), "text", None) or ""
    footer = _revision_footer(existing, last_updated)
    if len(footer) > base.MAX_FOOTER:
        return None, ["footer plus last_updated exceeds Discord limit"]
    embed.set_footer(text=footer)
    if base._embed_text_len(embed) > base.MAX_TOTAL:
        return None, ["embed total text plus last_updated exceeds Discord limit"]
    return embed, []


def question_list(topic: interactive.Topic, ui: interactive.UI) -> discord.Embed:
    """Add the newest question revision date to the generated FAQ group list."""

    embed = _ORIGINAL_QUESTION_LIST(topic, ui)
    dates = [
        parsed
        for row in topic.questions
        if (parsed := _date_value(row.data.get("last_updated", ""))) is not None
    ]
    if dates:
        newest = max(dates).strftime("%Y-%B-%d")
        embed.set_footer(text=f"{LAST_UPDATED_PREFIX}{newest}")
    return embed


def install() -> None:
    """Install revision-aware renderers once for permanent and interactive embeds."""

    global _INSTALLED
    if _INSTALLED:
        return
    base.build_embed = build_embed
    interactive._question_list = question_list
    _INSTALLED = True


def _cell_range(headers: dict[str, int], field: str, row_number: int) -> str:
    return f"{base._a1_col(headers[field])}{row_number}"


async def _write_updates(
    tab: str, headers: dict[str, int], updates: list[RevisionUpdate]
) -> None:
    if not updates:
        return
    worksheet = await base._worksheet(tab)
    payload: list[dict[str, Any]] = []
    for update in updates:
        payload.extend(
            (
                {
                    "range": _cell_range(headers, "last_updated", update.row.row_number),
                    "values": [[update.last_updated]],
                },
                {
                    "range": _cell_range(headers, "content_hash", update.row.row_number),
                    "values": [[update.content_hash]],
                },
            )
        )
    await base.sheets_core.acall_with_backoff(worksheet.batch_update, payload)


async def _sync_content_metadata(
    bot: discord.Client,
) -> tuple[base.Summary | None, Any | None]:
    """Validate and persist revision metadata before publish/refresh mutations."""

    target, tab, headers, rows, validation = await base.preflight(bot)
    if validation is not None:
        return validation, target

    missing = sorted(REVISION_HEADERS - set(headers))
    if missing:
        summary = base.Summary()
        summary.fail("config", "missing headers: " + ", ".join(missing))
        return summary, target

    updates, errors = _plan_updates(rows)
    if errors:
        summary = base.Summary()
        for key, reason in errors:
            summary.fail(key, reason)
        return summary, target

    try:
        await _write_updates(tab, headers, updates)
    except Exception:
        summary = base.Summary()
        summary.fail("sheet", "failed to update Server Rules revision metadata")
        return summary, target

    return None, target


async def publish(bot: discord.Client):
    install()
    validation, target = await _sync_content_metadata(bot)
    if validation is not None:
        return validation, target
    return await interactive.publish(bot)


async def refresh(bot: discord.Client):
    install()
    validation, target = await _sync_content_metadata(bot)
    if validation is not None:
        return validation, target
    return await interactive.refresh(bot)
