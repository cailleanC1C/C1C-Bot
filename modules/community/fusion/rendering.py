"""Fusion announcement rendering helpers."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from urllib.parse import urlparse

import discord

from shared.sheets.fusion import FusionEventRow, FusionRow
from shared.sheets import fusion as fusion_sheets

_FUSION_EMBED_COLOR = discord.Color.blurple()
_EMBED_FIELD_VALUE_LIMIT = 1024
_SCHEDULE_FIELD_TARGET = 900
_EMBED_MAX_FIELDS = 25


def _format_dt_utc(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _format_day_label(value: dt.date) -> str:
    return value.strftime("%a, %b ") + str(value.day)


def _humanize_type(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "Unknown"
    normalized = text.replace("_", " ").replace("-", " ")
    return " ".join(token.capitalize() for token in normalized.split())


def _status_icon(status: str) -> str:
    if status == "live":
        return "🔥"
    if status == "ended":
        return "🏁"
    return "⏳"


def _format_event_line(event: FusionEventRow, *, status: str) -> str:
    has_bonus = event.bonus is not None and event.bonus > 0
    points_text = f"{event.points_needed} pts" if event.points_needed is not None else "pts TBA"
    bonus_text = f" (+{event.bonus:g} bonus)" if has_bonus else ""
    return (
        f"{_status_icon(status)} {event.event_name} — "
        f"{points_text} for {event.reward_amount:g} frags{bonus_text}"
    )


def _chunk_lines(lines: list[str], limit: int) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in lines:
        line_len = len(line)
        if line_len > limit:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            chunks.append(line[:limit])
            continue

        added_len = line_len if not current else line_len + 1
        if current and current_len + added_len > limit:
            chunks.append("\n".join(current))
            current = [line]
            current_len = line_len
            continue

        current.append(line)
        current_len += added_len

    if current:
        chunks.append("\n".join(current))
    return chunks


def _format_month_day(value: dt.date) -> str:
    return f"{value.strftime('%b')} {value.day}"


def _format_date_range(start: dt.date, end: dt.date) -> str:
    if start == end:
        return _format_month_day(start)
    if start.year == end.year and start.month == end.month:
        return f"{start.strftime('%b')} {start.day}–{end.day}"
    return f"{_format_month_day(start)}–{_format_month_day(end)}"


def _normalize_image_url(value: str) -> str | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    parsed = urlparse(candidate)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    return candidate


def _build_fusion_embed(
    fusion: FusionRow,
    events: list[FusionEventRow],
    *,
    now: dt.datetime | None = None,
) -> discord.Embed:
    has_bonus = any(event.bonus is not None and event.bonus > 0 for event in events)
    sorted_events = sorted(events, key=lambda row: (row.start_at_utc, row.sort_order, row.event_id))
    event_days = [event.start_at_utc.astimezone(dt.timezone.utc).date() for event in sorted_events]

    summary_lines = [
        f"Type: {_humanize_type(fusion.fusion_type)}",
        f"Runs: {_format_dt_utc(fusion.start_at_utc)} -> {_format_dt_utc(fusion.end_at_utc)}",
        "",
        f"Target: {fusion.needed:g} fragments needed / {fusion.available:g} available",
        f"Schedule: {len(events)} events" + (" • includes bonus rewards" if has_bonus else ""),
    ]
    if fusion.fusion_structure.strip():
        summary_lines.insert(1, fusion.fusion_structure.strip())

    milestones_lines = [
        f"First start: {_format_day_label(min(event_days))}" if event_days else "First start: TBA",
        f"Last start: {_format_day_label(max(event_days))}" if event_days else "Last start: TBA",
    ]
    if has_bonus:
        bonus_events = [event.event_name for event in sorted_events if event.bonus is not None and event.bonus > 0]
        prefix = "Bonus event" if len(bonus_events) == 1 else "Bonus events"
        milestones_lines.append(f"{prefix}: {', '.join(bonus_events)}")

    embed = discord.Embed(
        title=f"Fusion: {fusion.fusion_name}",
        description="\n".join(summary_lines),
        colour=_FUSION_EMBED_COLOR,
    )
    champion_image_url = _normalize_image_url(fusion.champion_image_url)
    if champion_image_url:
        embed.set_image(url=champion_image_url)
    embed.add_field(name="Key Milestones", value="\n".join(milestones_lines), inline=False)
    sorted_events = sorted(events, key=lambda row: (row.start_at_utc, row.sort_order, row.event_id))
    status_by_event_id: dict[str, str] = {}
    for event in sorted_events:
        timing = fusion_sheets.get_valid_event_timing(event, for_helper="fusion_embed")
        if timing is None:
            status = "upcoming"
        else:
            start_at, end_at = timing
            status = fusion_sheets.derive_event_status(start_at_utc=start_at, end_at_utc=end_at, now=now)
        status_by_event_id[event.event_id] = status

    if sorted_events and len(embed.fields) < _EMBED_MAX_FIELDS:
        completed = sum(1 for status in status_by_event_id.values() if status == "ended")
        active = sum(1 for status in status_by_event_id.values() if status == "live")
        remaining = len(sorted_events) - completed - active
        embed.add_field(
            name="Schedule Status",
            value=f"{completed} ended • {active} live • {remaining} upcoming",
            inline=False,
        )

    grouped_events: dict[dt.date, list[FusionEventRow]] = defaultdict(list)
    for event in sorted_events:
        grouped_events[event.start_at_utc.astimezone(dt.timezone.utc).date()].append(event)

    if not sorted_events:
        embed.add_field(name="Schedule", value="No events available.", inline=False)
        embed.set_footer(text=f"Fusion ID: {fusion.fusion_id}")
        return embed

    if len(embed.fields) < _EMBED_MAX_FIELDS:
        embed.add_field(
            name="Legend",
            value="⏳ Upcoming • 🔥 Live • 🏁 Ended",
            inline=False,
        )

    # --- SIMPLE STRUCTURE: ONE DAY = ONE FIELD ---
    for day in sorted(grouped_events):
        if len(embed.fields) >= _EMBED_MAX_FIELDS:
            break
        lines = [
            f"• {_format_event_line(event, status=status_by_event_id[event.event_id])}"
            for event in grouped_events[day]
        ]
        embed.add_field(
            name=_format_day_label(day),
            value="\n".join(lines),
            inline=False
        )
    embed.set_footer(text=f"Fusion ID: {fusion.fusion_id}")
    return embed


def build_fusion_announcement_embed(
    fusion: FusionRow,
    events: list[FusionEventRow],
    *,
    now: dt.datetime | None = None,
) -> discord.Embed:
    """Build the Step 2 fusion publish announcement embed."""

    return _build_fusion_embed(fusion, events, now=now)


__all__ = ["build_fusion_announcement_embed"]
