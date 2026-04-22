"""Public sharing embed renderers for user fusion progress."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import discord

from shared.sheets import fusion as fusion_sheets

ShareMode = Literal["summary", "detailed"]

_DISPLAY_STATUS_ORDER = ("done", "in_progress", "skipped", "missed", "not_started")
_STATUS_LABELS = {
    "not_started": "Not Started",
    "in_progress": "In Progress",
    "done": "Done",
    "done_bonus": "Done + Bonus",
    "skipped": "Skipped",
    "missed": "Missed",
}
_STATUS_ICONS = {
    "done": "✅",
    "done_bonus": "✅",
    "in_progress": "🟡",
    "skipped": "⏭️",
    "missed": "⚠️",
    "not_started": "⬜",
}
_ALLOWED_PROGRESS_STATES = frozenset({"not_started", "in_progress", "done", "done_bonus", "skipped"})


@dataclass(slots=True)
class ProgressShareSnapshot:
    counts: dict[str, int]
    display_status_by_event: dict[str, str]
    completed_reward_total: float


def _event_bonus_amount(event: fusion_sheets.FusionEventRow) -> float:
    return event.bonus if event.bonus is not None else 0.0


def _effective_display_status(
    *,
    event: fusion_sheets.FusionEventRow,
    progress_by_event: Mapping[str, str],
    now: dt.datetime,
) -> str:
    status = progress_by_event.get(event.event_id, "not_started")
    if status not in _ALLOWED_PROGRESS_STATES:
        status = "not_started"
    if status in {"done", "done_bonus"}:
        return status

    timing = fusion_sheets.get_valid_event_timing(event, for_helper="fusion_my_progress_share")
    if timing is None:
        return status
    start_at, end_at = timing
    if fusion_sheets.derive_event_status(start_at_utc=start_at, end_at_utc=end_at, now=now) == "ended":
        return "missed"
    return status


def build_share_snapshot(
    *,
    events: Sequence[fusion_sheets.FusionEventRow],
    progress_by_event: Mapping[str, str],
    now: dt.datetime | None = None,
) -> ProgressShareSnapshot:
    current_time = now or dt.datetime.now(dt.timezone.utc)
    counts = {status: 0 for status in _DISPLAY_STATUS_ORDER}
    display_status_by_event: dict[str, str] = {}
    completed_reward_total = 0.0

    for event in events:
        status = _effective_display_status(event=event, progress_by_event=progress_by_event, now=current_time)
        if status == "done_bonus":
            display_status_by_event[event.event_id] = status
            counts["done"] += 1
            completed_reward_total += event.reward_amount + _event_bonus_amount(event)
            continue
        if status not in counts:
            status = "not_started"
        display_status_by_event[event.event_id] = status
        counts[status] += 1
        if status == "done":
            completed_reward_total += event.reward_amount

    return ProgressShareSnapshot(
        counts=counts,
        display_status_by_event=display_status_by_event,
        completed_reward_total=completed_reward_total,
    )


def _build_overall_progress_line(
    *,
    target: fusion_sheets.FusionRow,
    snapshot: ProgressShareSnapshot,
) -> str:
    reward_type = str(target.reward_type or "").strip().casefold()
    if reward_type == "fragments":
        return f"Progress: {snapshot.completed_reward_total:g} / {target.available:g} fragments"
    if reward_type:
        return f"Progress: {snapshot.completed_reward_total:g} / {target.available:g} {reward_type}"
    return f"Progress: {snapshot.completed_reward_total:g} / {target.available:g}"


def _build_summary_block(*, snapshot: ProgressShareSnapshot, overall_progress_line: str) -> str:
    return (
        f"✅ Done: {snapshot.counts['done']}\n"
        f"🟡 In Progress: {snapshot.counts['in_progress']}\n"
        f"⏭️ Skipped: {snapshot.counts['skipped']}\n"
        f"⚠️ Missed: {snapshot.counts['missed']}\n"
        f"⬜ Not Started: {snapshot.counts['not_started']}\n"
        f"{overall_progress_line}"
    )


def build_progress_share_embed(
    *,
    target: fusion_sheets.FusionRow,
    events: Sequence[fusion_sheets.FusionEventRow],
    progress_by_event: Mapping[str, str],
    user_display_name: str,
    mode: ShareMode,
) -> discord.Embed:
    snapshot = build_share_snapshot(events=events, progress_by_event=progress_by_event)
    overall_progress_line = _build_overall_progress_line(target=target, snapshot=snapshot)

    embed = discord.Embed(
        title=f"Progress Share — {target.fusion_name}",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="User", value=user_display_name, inline=False)
    embed.add_field(
        name="Summary",
        value=_build_summary_block(snapshot=snapshot, overall_progress_line=overall_progress_line),
        inline=False,
    )

    if mode == "detailed":
        sorted_events = sorted(events, key=lambda row: (row.sort_order, row.start_at_utc, row.event_id))
        detail_lines = []
        for event in sorted_events:
            status = snapshot.display_status_by_event.get(event.event_id, "not_started")
            icon = _STATUS_ICONS.get(status, _STATUS_ICONS["not_started"])
            label = _STATUS_LABELS.get(status, "Not Started")
            detail_lines.append(f"{icon} {event.event_name}: {label}")
        embed.add_field(name="Event Breakdown", value="\n".join(detail_lines)[:1024] or "No events available.", inline=False)

    embed.set_footer(text=f"Share Mode: {mode.title()}")
    return embed


__all__ = ["ShareMode", "ProgressShareSnapshot", "build_progress_share_embed", "build_share_snapshot"]
