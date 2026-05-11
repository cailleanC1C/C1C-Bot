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


def _is_stepped_fragment_event(event: fusion_sheets.FusionEventRow) -> bool:
    return (
        event.reward_amount > 0
        and str(event.reward_type or "").strip().lower() == "fragment"
        and bool(event.milestones)
    )


@dataclass(slots=True)
class ProgressShareSnapshot:
    counts: dict[str, int]
    display_status_by_event: dict[str, str]
    completed_reward_total: float
    in_progress_partial_total: float
    skipped_reward_total: float


def _event_bonus_amount(event: fusion_sheets.FusionEventRow) -> float:
    return event.bonus if event.bonus is not None else 0.0


def _effective_display_status(
    *,
    event: fusion_sheets.FusionEventRow,
    progress_by_event: Mapping[str, str],
    partial_by_event: Mapping[str, float] | None = None,
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
    if status == "not_started" and fusion_sheets.derive_event_status(start_at_utc=start_at, end_at_utc=end_at, now=now) == "ended":
        return "missed"
    return status


def build_share_snapshot(
    *,
    events: Sequence[fusion_sheets.FusionEventRow],
    progress_by_event: Mapping[str, str],
    partial_by_event: Mapping[str, float] | None = None,
    now: dt.datetime | None = None,
) -> ProgressShareSnapshot:
    current_time = now or dt.datetime.now(dt.timezone.utc)
    counts = {status: 0 for status in _DISPLAY_STATUS_ORDER}
    display_status_by_event: dict[str, str] = {}
    completed_reward_total = 0.0
    in_progress_partial_total = 0.0
    skipped_reward_total = 0.0
    partial_map = partial_by_event or {}

    for event in events:
        raw_status = str(progress_by_event.get(event.event_id, "not_started") or "").strip().lower()
        status = _effective_display_status(
            event=event,
            progress_by_event=progress_by_event,
            partial_by_event=partial_by_event,
            now=current_time,
        )
        partial_amount = max(0.0, float(partial_map.get(event.event_id, 0.0)))
        if status == "done_bonus":
            display_status_by_event[event.event_id] = status
            counts["done"] += 1
            done_base = event.reward_amount
            if _is_stepped_fragment_event(event) and partial_amount > 0:
                done_base = partial_amount
            completed_reward_total += done_base + _event_bonus_amount(event)
            continue
        if status not in counts:
            status = "not_started"
        display_status_by_event[event.event_id] = status
        counts[status] += 1
        if status == "done":
            if _is_stepped_fragment_event(event) and partial_amount > 0:
                completed_reward_total += partial_amount
            else:
                completed_reward_total += event.reward_amount
        elif status == "in_progress":
            in_progress_partial_total += partial_amount
        elif status in {"skipped", "missed"}:
            skipped_reward_total += event.reward_amount
            if raw_status == "done_bonus":
                skipped_reward_total += _event_bonus_amount(event)

    return ProgressShareSnapshot(
        counts=counts,
        display_status_by_event=display_status_by_event,
        completed_reward_total=completed_reward_total + in_progress_partial_total,
        in_progress_partial_total=in_progress_partial_total,
        skipped_reward_total=skipped_reward_total,
    )


def _build_overall_progress_line(
    *,
    target: fusion_sheets.FusionRow,
    snapshot: ProgressShareSnapshot,
) -> str:
    reward_type = str(target.reward_type or "").strip()
    if reward_type:
        return f"Progress: {snapshot.completed_reward_total:g} / {target.available:g} {reward_type}"
    return f"Progress: {snapshot.completed_reward_total:g} / {target.available:g} rewards"


def _build_summary_block(*, snapshot: ProgressShareSnapshot, overall_progress_line: str) -> str:
    return (
        f"✅ Done: {snapshot.counts['done']}\n"
        f"🟡 In Progress: {snapshot.counts['in_progress']}\n"
        f"⏭️ Skipped: {snapshot.counts['skipped']}\n"
        f"⚠️ Missed: {snapshot.counts['missed']}\n"
        f"⬜ Not Started: {snapshot.counts['not_started']}\n"
        f"{overall_progress_line}"
    )


def _build_strategic_progress_block(*, target: fusion_sheets.FusionRow, snapshot: ProgressShareSnapshot) -> str:
    reward_type = str(target.reward_type or "").strip().title() or "Reward"
    acquired = snapshot.completed_reward_total
    skipped = snapshot.skipped_reward_total
    to_go = max(float(target.needed) - acquired, 0.0)
    to_go_line = "Fusion ready" if to_go <= 0 else f"{to_go:g} to go"
    return (
        f"**{reward_type} Progress**\n"
        f"{acquired:g} acquired\n"
        f"{skipped:g} skipped\n"
        f"{to_go_line}\n\n"
        f"{target.needed:g} / {target.available:g} needed"
    )


def build_progress_share_embed(
    *,
    target: fusion_sheets.FusionRow,
    events: Sequence[fusion_sheets.FusionEventRow],
    progress_by_event: Mapping[str, str],
    partial_by_event: Mapping[str, float] | None = None,
    user_display_name: str,
    mode: ShareMode,
) -> discord.Embed:
    snapshot = build_share_snapshot(
        events=events,
        progress_by_event=progress_by_event,
        partial_by_event=partial_by_event,
    )
    overall_progress_line = _build_overall_progress_line(target=target, snapshot=snapshot)

    embed = discord.Embed(
        title=f"Progress Share: {target.fusion_name}",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="User", value=user_display_name, inline=False)
    embed.add_field(
        name="Summary",
        value=_build_summary_block(snapshot=snapshot, overall_progress_line=overall_progress_line),
        inline=False,
    )
    embed.add_field(
        name="\u200b",
        value=_build_strategic_progress_block(target=target, snapshot=snapshot),
        inline=False,
    )

    if mode == "detailed":
        sorted_events = sorted(events, key=lambda row: (row.sort_order, row.start_at_utc, row.event_id))
        detail_lines = []
        for event in sorted_events:
            status = snapshot.display_status_by_event.get(event.event_id, "not_started")
            icon = _STATUS_ICONS.get(status, _STATUS_ICONS["not_started"])
            label = _STATUS_LABELS.get(status, "Not Started")
            line = f"{icon} {event.event_name}: {label}"
            partial_amount = max(0.0, float((partial_by_event or {}).get(event.event_id, 0.0)))
            if status in {"in_progress", "done", "done_bonus"} and partial_amount > 0 and _is_stepped_fragment_event(event):
                line += f" ({partial_amount:g} / {event.reward_amount:g} fragments)"
            detail_lines.append(line)
        embed.add_field(name="Event Breakdown", value="\n".join(detail_lines)[:1024] or "No events available.", inline=False)

    embed.set_footer(text=f"Share Mode: {mode.title()}")
    return embed


__all__ = ["ShareMode", "ProgressShareSnapshot", "build_progress_share_embed", "build_share_snapshot"]
