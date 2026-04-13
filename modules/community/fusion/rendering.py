"""Fusion announcement rendering helpers."""

from __future__ import annotations

import discord

from shared.sheets.fusion import FusionEventRow, FusionRow

_FUSION_EMBED_COLOR = discord.Color.blurple()


def _format_dt_utc(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M UTC")


def _format_event_line(event: FusionEventRow) -> str:
    points = str(event.points_needed) if event.points_needed is not None else "TBA"
    bonus_text = f" (+{event.bonus:g} bonus)" if event.bonus is not None and event.bonus > 0 else ""
    return f"• **{event.event_name}** — {event.reward_amount:g}{bonus_text} | points: {points}"


def build_fusion_announcement_embed(fusion: FusionRow, events: list[FusionEventRow]) -> discord.Embed:
    """Build the Step 2 fusion publish announcement embed."""

    has_bonus = any(event.bonus is not None and event.bonus > 0 for event in events)
    summary_lines = [
        f"**Champion:** {fusion.champion}",
        f"**Type:** {fusion.fusion_type or 'Unknown'}",
        f"**Window:** {_format_dt_utc(fusion.start_at_utc)} → {_format_dt_utc(fusion.end_at_utc)}",
        f"**Progress Target:** {fusion.needed} needed / {fusion.available} available",
        f"**Events:** {len(events)} total" + (" • includes bonus rewards" if has_bonus else ""),
    ]

    event_preview = sorted(events, key=lambda row: (row.start_at_utc, row.sort_order, row.event_id))[:5]
    if event_preview:
        event_lines = [_format_event_line(event) for event in event_preview]
    else:
        event_lines = ["No event rows configured yet."]

    embed = discord.Embed(
        title=f"Fusion Live: {fusion.fusion_name}",
        description="\n".join(summary_lines),
        colour=_FUSION_EMBED_COLOR,
    )
    embed.add_field(name="Event Preview (First 5)", value="\n".join(event_lines), inline=False)
    embed.add_field(
        name="How to use",
        value="Fusion reminders and tracking will be posted here.",
        inline=False,
    )
    embed.set_footer(text=f"Fusion ID: {fusion.fusion_id}")
    return embed


__all__ = ["build_fusion_announcement_embed"]
