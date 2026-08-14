"""Shared Victory Ledger link rendering for Live Arena."""

from __future__ import annotations


def match_thread_link(thread_id: object, guild_id: object) -> str:
    """Render a stable named jump link without exposing the forum thread title."""
    thread = str(thread_id or "").strip()
    guild = str(guild_id or "").strip()
    if not thread or not guild:
        return "Forum post pending"
    return f"💬 [Open match thread](https://discord.com/channels/{guild}/{thread})"
