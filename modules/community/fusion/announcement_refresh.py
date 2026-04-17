"""Fusion announcement auto-refresh loop helpers."""

from __future__ import annotations

import datetime as dt
import hashlib
import logging

import discord
from discord.ext import commands

from modules.community.fusion.announcements import resolve_stored_announcement
from modules.community.fusion.opt_in_view import build_fusion_opt_in_view
from modules.community.fusion.rendering import build_fusion_announcement_embed
from shared.sheets import fusion as fusion_sheets

log = logging.getLogger("c1c.community.fusion.announcement_refresh")


def _utc_now(now: dt.datetime | None = None) -> dt.datetime:
    if now is None:
        return dt.datetime.now(dt.timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=dt.timezone.utc)
    return now.astimezone(dt.timezone.utc)


def _compute_status_hash(
    events: list[fusion_sheets.FusionEventRow],
    *,
    now: dt.datetime,
) -> str:
    pairs: list[tuple[str, str]] = []
    for event in sorted(events, key=lambda row: (row.start_at_utc, row.sort_order, row.event_id)):
        timing = fusion_sheets.get_valid_event_timing(event, for_helper="fusion_announcement_refresh")
        if timing is None:
            continue
        start_at, end_at = timing
        status = fusion_sheets.derive_event_status(start_at_utc=start_at, end_at_utc=end_at, now=now)
        pairs.append((event.event_id, status))
    encoded = "|".join(f"{event_id}:{status}" for event_id, status in pairs).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _needs_refresh(
    fusion: fusion_sheets.FusionRow,
    *,
    now: dt.datetime,
    status_hash: str,
) -> bool:
    last_refresh = fusion.last_announcement_refresh_at
    if last_refresh is None:
        return True
    if last_refresh.date() != now.date():
        return True
    return str(fusion.last_announcement_status_hash or "").strip() != status_hash


async def _fetch_existing_announcement(
    bot: commands.Bot,
    target: fusion_sheets.FusionRow,
) -> discord.Message | None:
    resolution = await resolve_stored_announcement(bot, target)
    return resolution.message


async def process_fusion_announcement_refreshes(
    bot: commands.Bot,
    *,
    now: dt.datetime | None = None,
) -> None:
    is_closed = getattr(bot, "is_closed", None)
    is_ready = getattr(bot, "is_ready", None)
    if callable(is_closed) and is_closed():
        return
    if callable(is_ready) and not is_ready():
        return

    reference = _utc_now(now)
    try:
        targets = await fusion_sheets.get_published_fusions()
    except Exception:
        log.exception("fusion announcement refresh failed to load published fusions")
        return

    for target in targets:
        try:
            try:
                events = await fusion_sheets.get_fusion_events(target.fusion_id)
            except Exception:
                log.exception(
                    "fusion announcement refresh failed to load events",
                    extra={"fusion_id": target.fusion_id},
                )
                continue
            status_hash = _compute_status_hash(events, now=reference)
            if not _needs_refresh(target, now=reference, status_hash=status_hash):
                continue

            existing_message = await _fetch_existing_announcement(bot, target)
            if existing_message is None:
                log.warning(
                    "fusion announcement refresh skipped; existing announcement missing",
                    extra={
                        "fusion_id": target.fusion_id,
                        "announcement_channel_id": target.announcement_channel_id,
                        "announcement_message_id": target.announcement_message_id,
                    },
                )
                continue

            try:
                announcement_embed = build_fusion_announcement_embed(target, events, now=reference)
                announcement_view = build_fusion_opt_in_view(target)
                await existing_message.edit(embed=announcement_embed, view=announcement_view)
            except Exception:
                log.exception(
                    "fusion announcement refresh failed to edit existing announcement",
                    extra={
                        "fusion_id": target.fusion_id,
                        "announcement_channel_id": target.announcement_channel_id,
                        "announcement_message_id": target.announcement_message_id,
                    },
                )
                continue

            try:
                await fusion_sheets.update_fusion_announcement_refresh_state(
                    target.fusion_id,
                    refreshed_at=reference,
                    status_hash=status_hash,
                )
            except Exception:
                log.exception(
                    "fusion announcement refresh failed to persist refresh state",
                    extra={"fusion_id": target.fusion_id, "status_hash": status_hash},
                )
                continue
        except Exception:
            log.exception(
                "fusion announcement refresh failed",
                extra={"fusion_id": target.fusion_id},
            )


__all__ = ["process_fusion_announcement_refreshes"]
