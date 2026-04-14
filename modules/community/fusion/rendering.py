"""Fusion announcement rendering helpers."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

import discord

from shared.sheets.fusion import FusionEventRow, FusionRow

_FUSION_EMBED_COLOR = discord.Color.blurple()
_EMBED_FIELD_VALUE_LIMIT = 1024
_EMBED_MAX_FIELDS = 25


def _format_dt_utc(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _format_day_label(value: dt.date) -> str:
    return f"Starts {value.strftime('%a, %b')} {value.day}"


def _format_event_line(event: FusionEventRow) -> str:
    has_bonus = event.bonus is not None and event.bonus > 0
    points_text = (
        f"{event.points_needed} points" if event.points_needed is not None else "points TBA"
    )
    bonus_text = f" (+{event.bonus:g} bonus)" if has_bonus else ""
    return (
        f"• {event.event_name} — {points_text} for {event.reward_amount:g} fragments"
        f"{bonus_text}"
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


def build_fusion_announcement_embed(fusion: FusionRow, events: list[FusionEventRow]) -> discord.Embed:
    """Build the Step 2 fusion publish announcement embed."""

    has_bonus = any(event.bonus is not None and event.bonus > 0 for event in events)
    summary_lines = [
        f"Type: {fusion.fusion_type}",
        f"Runs from {_format_dt_utc(fusion.start_at_utc)} → {_format_dt_utc(fusion.end_at_utc)}",
        f"Target: {fusion.needed} fragments needed / {fusion.available} available",
        f"Schedule: {len(events)} events/tournaments"
        + (" • includes bonus rewards" if has_bonus else ""),
    ]
    if fusion.fusion_structure.strip():
        summary_lines.insert(1, fusion.fusion_structure.strip())

    sorted_events = sorted(events, key=lambda row: (row.start_at_utc, row.sort_order, row.event_id))
    grouped_events: dict[dt.date, list[FusionEventRow]] = defaultdict(list)
    for event in sorted_events:
        grouped_events[event.start_at_utc.astimezone(dt.timezone.utc).date()].append(event)

    embed = discord.Embed(
        title=f"Fusion: {fusion.fusion_name}",
        description="\n".join(summary_lines),
        colour=_FUSION_EMBED_COLOR,
    )

    for day in sorted(grouped_events):
        day_lines = [_format_event_line(event) for event in grouped_events[day]]
        for idx, chunk in enumerate(_chunk_lines(day_lines, _EMBED_FIELD_VALUE_LIMIT)):
            if len(embed.fields) >= _EMBED_MAX_FIELDS:
                return embed
            field_name = _format_day_label(day)
            if idx > 0:
                field_name = f"{field_name} (cont.)"
            embed.add_field(name=field_name, value=chunk, inline=False)

    embed.set_footer(text=f"Fusion ID: {fusion.fusion_id}")
    return embed


__all__ = ["build_fusion_announcement_embed"]
