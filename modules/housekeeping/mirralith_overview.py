from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
import math
import os
from dataclasses import dataclass
from typing import Iterable, List

import discord
from PIL import Image, ImageDraw, ImageFont

from modules.common import runtime as runtime_helpers
from shared.sheets import core as sheets_core
from shared.sheets import recruitment
from shared.sheets.export_utils import export_pdf_as_png, get_tab_gid

log = logging.getLogger("c1c.housekeeping.mirralith")


@dataclass(frozen=True)
class ImageSpec:
    label: str
    description: str
    tab_key: str
    range_key: str
    filename: str


IMAGE_SPECS: List[ImageSpec] = [
    ImageSpec(
        label="[MIRRALITH_CLAN_STATUS]",
        description="Mirralith • Clan Status",
        tab_key="MIRRALITH_TAB",
        range_key="MIRRALITH_CLAN_RANGE",
        filename="mirralith_clan_status.png",
    ),
    ImageSpec(
        label="[MIRRALITH_LEADERSHIP]",
        description="Mirralith • Clan Leadership",
        tab_key="MIRRALITH_TAB",
        range_key="MIRRALITH_LEADERSHIP_RANGE",
        filename="mirralith_leadership.png",
    ),
    ImageSpec(
        label="[MIRRALITH_CLUSTER_ELITE_END]",
        description="Cluster Structure",
        tab_key="CLUSTER_STRUCTURE_TAB",
        range_key="CLUSTER_ELITE_END_RANGE",
        filename="cluster_elite_end.png",
    ),
    ImageSpec(
        label="[MIRRALITH_CLUSTER_EARLY_END]",
        description="",
        tab_key="CLUSTER_STRUCTURE_TAB",
        range_key="CLUSTER_EARLY_END_RANGE",
        filename="cluster_early_end.png",
    ),
    ImageSpec(
        label="[MIRRALITH_CLUSTER_LATE]",
        description=" ",
        tab_key="CLUSTER_STRUCTURE_TAB",
        range_key="CLUSTER_LATE_RANGE",
        filename="cluster_late.png",
    ),
    ImageSpec(
        label="[MIRRALITH_CLUSTER_MID]",
        description=" ",
        tab_key="CLUSTER_STRUCTURE_TAB",
        range_key="CLUSTER_MID_RANGE",
        filename="cluster_mid.png",
    ),
    ImageSpec(
        label="[MIRRALITH_CLUSTER_EARLY]",
        description=" ",
        tab_key="CLUSTER_STRUCTURE_TAB",
        range_key="CLUSTER_EARLY_RANGE",
        filename="cluster_early.png",
    ),
    ImageSpec(
        label="[MIRRALITH_CLUSTER_BEGINNER]",
        description=" ",
        tab_key="CLUSTER_STRUCTURE_TAB",
        range_key="CLUSTER_BEGINNER_RANGE",
        filename="cluster_beginner.png",
    ),
]


def _normalize_rows(values: Iterable[Iterable[object]]) -> list[list[str]]:
    rows: list[list[str]] = []
    for row in values:
        normalized = [str(cell) if cell is not None else "" for cell in row]
        rows.append(normalized)
    return rows


def _pad_rows(rows: list[list[str]], columns: int) -> list[list[str]]:
    padded: list[list[str]] = []
    for row in rows:
        copy = list(row)
        while len(copy) < columns:
            copy.append("")
        padded.append(copy[:columns])
    return padded


def _column_widths(rows: list[list[str]], font: ImageFont.ImageFont) -> list[int]:
    widths: list[int] = []
    columns = max(len(row) for row in rows)
    for index in range(columns):
        widest = max(font.getlength(row[index]) for row in rows)
        widths.append(max(40, math.ceil(widest) + 6))
    return widths


def export_sheet_range_to_png(spreadsheet_id: str, tab_name: str, cell_range: str) -> bytes:
    """Render the given Sheets range into a PNG image and return the bytes."""

    a1_range = f"{tab_name}!{cell_range}" if tab_name else cell_range
    values = sheets_core.sheets_read(spreadsheet_id, a1_range)
    rows = _normalize_rows(values or [])
    if not rows:
        return b""

    columns = max(len(row) for row in rows)
    if columns <= 0:
        return b""

    font = ImageFont.load_default()
    padded_rows = _pad_rows(rows, columns)
    widths = _column_widths(padded_rows, font)
    text_height = font.getbbox("Ag")[3] - font.getbbox("Ag")[1]
    padding_x = 8
    padding_y = 6
    line_width = 1
    row_height = text_height + padding_y * 2

    image_width = int(sum(widths) + line_width * (columns + 1)) + padding_x * 2
    image_height = int(len(padded_rows) * row_height + line_width * (len(padded_rows) + 1)) + padding_y * 2

    image = Image.new("RGB", (image_width, image_height), color="white")
    draw = ImageDraw.Draw(image)

    # Grid lines
    y = padding_y
    for _ in range(len(padded_rows) + 1):
        draw.line([(0, y), (image_width, y)], fill=(220, 220, 220), width=line_width)
        y += row_height + line_width

    x = padding_x
    for width in widths:
        draw.line([(x, 0), (x, image_height)], fill=(220, 220, 220), width=line_width)
        x += width + line_width
    draw.line([(image_width - line_width, 0), (image_width - line_width, image_height)], fill=(220, 220, 220), width=line_width)

    # Cell text
    y = padding_y + line_width
    for row in padded_rows:
        x = padding_x + line_width
        for index, cell in enumerate(row):
            draw.text((x + 2, y + padding_y - 2), cell, font=font, fill=(20, 20, 20))
            x += widths[index] + line_width
        y += row_height + line_width

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def build_mirralith_message_content(label: str, description: str, updated_date: str) -> str:
    lines = [
        f" ",
        f"# {description}",
        f"-# *Last updated {updated_date}  ||{label}||*",
    ]
    return "\n".join(lines)


async def upsert_labeled_message(
    channel: discord.TextChannel, label: str, content: str, file: discord.File
) -> None:
    bot_member = getattr(getattr(channel, "guild", None), "me", None)
    bot_id = getattr(bot_member, "id", None)
    if bot_id is None:
        client_user = getattr(getattr(channel, "client", None), "user", None)
        bot_id = getattr(client_user, "id", None)

    async def _find_existing() -> discord.Message | None:
        try:
            async for message in channel.history(limit=50):
                if bot_id is not None and getattr(message.author, "id", None) != bot_id:
                    continue
                if label in (message.content or ""):
                    return message
        except Exception:
            log.exception(
                "failed to inspect history for Mirralith label",
                extra={"channel_id": getattr(channel, "id", None), "label": label},
            )
        return None

    try:
        existing = await _find_existing()
        if existing is not None:
            try:
                await existing.edit(content=content, attachments=[file])
                return
            except Exception:
                log.exception(
                    "failed to edit Mirralith message; sending new message instead",
                    extra={"channel_id": getattr(channel, "id", None), "label": label},
                )
        await channel.send(content=content, file=file)
    except Exception:
        log.exception(
            "failed to upsert Mirralith message",
            extra={"channel_id": getattr(channel, "id", None), "label": label},
        )


async def run_mirralith_overview_job(bot: discord.Client, trigger: str = "scheduled") -> None:
    log.info("Running Mirralith overview job (trigger=%s)", trigger)

    raw_channel_id = os.getenv("MIRRALITH_CHANNEL_ID")
    try:
        channel_id = int(raw_channel_id) if raw_channel_id is not None else None
    except (TypeError, ValueError):
        log.warning("Mirralith overview channel ID invalid; aborting")
        return

    if not channel_id:
        log.warning("Mirralith overview channel ID missing; aborting")
        return

    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            log.exception("failed to resolve Mirralith overview channel", extra={"channel_id": channel_id})
            return

    if not isinstance(channel, discord.TextChannel):
        log.warning("Mirralith overview channel is not a text channel", extra={"channel_id": channel_id})
        return

    spreadsheet_id = os.getenv("RECRUITMENT_SHEET_ID")
    if not spreadsheet_id:
        log.warning("Recruitment sheet ID missing; skipping Mirralith overview job")
        return

    loop = asyncio.get_running_loop()
    run_time = dt.datetime.now(dt.timezone.utc)
    updated_date = run_time.date().isoformat()
    run_timestamp = run_time.strftime("%Y-%m-%d %H:%M UTC")

    updated_labels: list[str] = []
    failed_labels: list[tuple[str, str]] = []

    for spec in IMAGE_SPECS:
        def record_failure(reason: str) -> None:
            failed_labels.append((spec.label, reason))

        try:
            tab_name = recruitment.get_config_value(spec.tab_key, "") or ""
            range_value = recruitment.get_config_value(spec.range_key, "") or ""
        except Exception as exc:
            record_failure("config lookup failed")
            log.warning(
                "Mirralith config lookup failed; skipping spec",
                extra={
                    "label": spec.label,
                    "tab_key": spec.tab_key,
                    "range_key": spec.range_key,
                    "error": str(exc),
                },
            )
            continue
        if not tab_name or not range_value:
            record_failure("tab or range missing")
            log.warning(
                "Mirralith spec missing tab or range; skipping",
                extra={"label": spec.label, "tab_key": spec.tab_key, "range_key": spec.range_key},
            )
            continue

        log.info(
            "Mirralith spec resolved",
            extra={"label": spec.label, "tab": tab_name, "cell_range": range_value},
        )

        if ":" not in range_value:
            record_failure("invalid range")
            log.error(
                "mirralith_export: invalid range (missing colon)",
                extra={"label": spec.label, "tab": tab_name, "range": range_value},
            )
            continue

        try:
            gid = await loop.run_in_executor(None, get_tab_gid, spreadsheet_id, tab_name)
        except Exception as exc:
            record_failure("gid lookup failed")
            log.error(
                "❌ error — mirralith_export • label=%s • tab=%s • range=%s • reason=%s",
                spec.label,
                tab_name,
                range_value,
                f"gid_lookup_failed:{exc}",
                extra={"label": spec.label, "tab": tab_name, "range": range_value},
            )
            continue

        if gid is None:
            record_failure("gid missing")
            log.error(
                "❌ error — mirralith_export • label=%s • tab=%s • range=%s • reason=%s",
                spec.label,
                tab_name,
                range_value,
                "gid_missing",
                extra={"label": spec.label, "tab": tab_name, "range": range_value},
            )
            continue

        try:
            png_bytes = await export_pdf_as_png(
                spreadsheet_id,
                gid,
                range_value,
                log_context={
                    "label": spec.label,
                    "tab": tab_name,
                    "range": range_value,
                },
            )
        except Exception:
            record_failure("export exception")
            log.exception(
                "❌ error — mirralith_export • label=%s • tab=%s • range=%s • reason=%s",
                spec.label,
                tab_name,
                range_value,
                "export_exception",
                extra={"label": spec.label, "tab": tab_name, "range": range_value},
            )
            continue

        if not png_bytes:
            record_failure("render failed")
            log.warning(
                "failed to export Mirralith range (PDF renderer unavailable or failed)",
                extra={"label": spec.label, "tab": tab_name, "range": range_value},
            )
            continue

        file = discord.File(io.BytesIO(png_bytes), filename=spec.filename)
        content = build_mirralith_message_content(spec.label, spec.description, updated_date)

        try:
            await upsert_labeled_message(channel, spec.label, content, file)
            updated_labels.append(spec.label)
        except Exception:
            record_failure("message upsert failed")
            log.exception(
                "failed to upsert Mirralith overview message",
                extra={"channel_id": channel_id, "label": spec.label},
            )

    summary_parts = [
        "🧹 Mirralith overview job finished",
        f"Trigger: {trigger}",
    ]

    total_specs = len(IMAGE_SPECS)
    summary_parts.append(f"Specs updated: {len(updated_labels)} / {total_specs}")

    if failed_labels:
        failed_items = ", ".join(f"{label} ({reason})" for label, reason in failed_labels)
        summary_parts.append(f"Failed: {failed_items}")
    else:
        summary_parts.append("Result: all updated successfully")

    summary_parts.append(f"Timestamp: {run_timestamp}")
    log.info("Mirralith overview job finished.")

    try:
        await runtime_helpers.send_log_message("\n".join(summary_parts))
    except Exception:
        log.warning("failed to send Mirralith overview summary to log channel", exc_info=True)


__all__ = [
    "IMAGE_SPECS",
    "ImageSpec",
    "build_mirralith_message_content",
    "export_sheet_range_to_png",
    "run_mirralith_overview_job",
    "upsert_labeled_message",
]
