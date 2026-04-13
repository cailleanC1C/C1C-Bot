from __future__ import annotations

"""Daily report listing currently open Welcome and Move Request tickets."""

import logging
import re
from datetime import datetime, timezone
from typing import Iterable, Sequence

import discord
from discord import HTTPException

from modules.common.embeds import get_embed_colour
from modules.common.tickets import TicketThread, fetch_ticket_threads
from modules.recruitment.reporting.destinations import resolve_report_destination

log = logging.getLogger("c1c.recruitment.reporting.open_tickets")
_CF_RAY_RE = re.compile(r"Cloudflare Ray ID:\s*<strong[^>]*>([^<]+)<", re.IGNORECASE)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _format_lines(tickets: Sequence[TicketThread]) -> list[str]:
    if not tickets:
        return ["🔹 None right now ✨"]
    return [
        f"🔹 [{ticket.name}]({ticket.url}) {_format_timestamp(ticket.created_at)}"
        for ticket in tickets
    ]


def _chunk_lines(lines: Sequence[str], *, limit: int = 1024) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0

    for line in lines or ("",):
        if len(line) > limit:
            line = f"{line[: limit - 1]}…"

        pending_len = len(line) + (1 if current else 0)
        if current and current_len + pending_len > limit:
            chunks.append("\n".join(current) or "\u200b")
            current = [line]
            current_len = len(line)
            continue

        current.append(line)
        current_len += pending_len

    if current:
        chunks.append("\n".join(current) or "\u200b")

    return chunks or ["\u200b"]


def _new_embed() -> discord.Embed:
    return discord.Embed(title="Currently Open Tickets", colour=get_embed_colour("recruitment"))


def _set_footer(embed: discord.Embed, now_text: str, page: int) -> None:
    page_suffix = "" if page == 1 else f" (page {page})"
    embed.set_footer(text=f"Last updated {now_text}{page_suffix} UTC •")


def _build_report_embeds(
    welcome: Sequence[TicketThread], move_requests: Sequence[TicketThread]
) -> list[discord.Embed]:
    now_text = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    embeds: list[discord.Embed] = []
    page = 1

    def _add_field(title: str, value: str) -> None:
        nonlocal page
        if not embeds:
            embeds.append(_new_embed())

        embeds[-1].add_field(name=title, value=value or "\u200b", inline=False)

        if len(embeds[-1]) > 6000:
            embeds[-1].remove_field(-1)
            page += 1
            embeds.append(_new_embed())
            embeds[-1].add_field(name=title, value=value or "\u200b", inline=False)

    for title, lines in (
        ("Welcome", _format_lines(welcome)),
        ("Move Requests", _format_lines(move_requests)),
    ):
        for idx, chunk in enumerate(_chunk_lines(lines)):
            if title == "Welcome" and idx > 0:
                name = "\u200b"
            else:
                name = title if idx == 0 else f"{title} (cont.)"
            _add_field(name, chunk)

    embeds = embeds or [_new_embed()]

    for idx, embed in enumerate(embeds, start=1):
        _set_footer(embed, now_text, idx)

    return embeds


def _group_tickets(tickets: Iterable[TicketThread]) -> tuple[list[TicketThread], list[TicketThread]]:
    welcome: list[TicketThread] = []
    move_requests: list[TicketThread] = []
    for ticket in tickets:
        if not ticket.is_open:
            continue
        target = welcome if ticket.kind == "welcome" else move_requests
        target.append(ticket)
    welcome.sort(key=lambda item: item.created_at)
    move_requests.sort(key=lambda item: item.created_at)
    return welcome, move_requests


def _summarize_http_error_text(raw_text: object, *, max_chars: int = 220) -> str:
    text = str(raw_text or "").strip()
    if not text:
        return "-"

    match = _CF_RAY_RE.search(text)
    if match:
        return f"cloudflare_challenge(ray_id={match.group(1)})"

    compact = " ".join(text.split())
    if len(compact) <= max_chars:
        return compact
    return f"{compact[:max_chars]}…"


async def send_currently_open_tickets_report(bot: discord.Client) -> tuple[bool, str]:
    channel, error = await resolve_report_destination(bot)
    if channel is None:
        return False, error

    try:
        tickets = await fetch_ticket_threads(bot, include_archived=False, with_members=False)
    except Exception as exc:
        log.warning("failed to collect open tickets", exc_info=True)
        return False, f"fetch:{type(exc).__name__}"

    welcome, move_requests = _group_tickets(tickets)
    embeds = _build_report_embeds(welcome, move_requests)

    try:
        for embed in embeds:
            await channel.send(embeds=[embed])
    except HTTPException as exc:
        text_summary = _summarize_http_error_text(getattr(exc, "text", None))
        log.warning(
            "failed to send open tickets report (status=%s text=%s)",
            getattr(exc, "status", "?"),
            text_summary,
            exc_info=True,
        )
        return False, f"send:{type(exc).__name__}"
    except Exception as exc:
        log.warning("failed to send open tickets report", exc_info=True)
        return False, f"send:{type(exc).__name__}"

    return True, "-"


__all__ = ["send_currently_open_tickets_report"]
