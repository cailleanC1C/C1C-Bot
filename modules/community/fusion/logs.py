"""Shared Fusion logging/reporting helpers built on existing runtime patterns."""

from __future__ import annotations

import logging
from typing import Any, Mapping

import discord

from modules.common import runtime as rt
from shared.dedupe import EventDeduper
from shared.logfmt import human_reason

log = logging.getLogger("c1c.community.fusion.logs")
_ALERT_DEDUPER = EventDeduper(window_s=300.0, max_keys=512)


def _render_fields(fields: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for key, value in fields.items():
        if value in (None, "", "-"):
            continue
        parts.append(f"{key}={value}")
    return " • ".join(parts)


async def send_ops_alert(
    *,
    component: str,
    summary: str,
    dedupe_key: str | None = None,
    error: BaseException | None = None,
    fields: Mapping[str, Any] | None = None,
) -> None:
    """Emit a Fusion-focused alert to the configured internal log channel."""

    key = dedupe_key or ""
    if key and not _ALERT_DEDUPER.should_emit(key):
        return

    detail = _render_fields(fields or {})
    reason = human_reason(error) if error is not None else "-"
    message = f"❌ Fusion — component={component} • summary={summary} • reason={reason}"
    if detail:
        message = f"{message} • {detail}"

    try:
        await rt.send_log_message(message)
    except Exception:
        log.warning("failed to send Fusion ops alert", exc_info=True)


def interaction_context(
    interaction: discord.Interaction,
    *,
    custom_id: str | None = None,
) -> dict[str, Any]:
    data = getattr(interaction, "data", None) or {}
    channel = getattr(interaction, "channel", None)
    message = getattr(interaction, "message", None)
    return {
        "guild_id": getattr(getattr(interaction, "guild", None), "id", None)
        or getattr(interaction, "guild_id", None),
        "channel_id": getattr(channel, "id", None),
        "message_id": getattr(message, "id", None),
        "interaction_id": getattr(interaction, "id", None),
        "custom_id": custom_id or data.get("custom_id"),
        "user_id": getattr(getattr(interaction, "user", None), "id", None),
    }
