"""Configured Discord embed rendering."""

from __future__ import annotations
import discord
from .models import norm, truthy


def choose_row(rows, key, tournament_id):
    candidates = [
        r
        for r in rows
        if norm(r.get("message_key", r.get("key"))) == norm(key)
        and truthy(r.get("active", True))
    ]
    return next(
        (r for r in candidates if str(r.get("tournament_id", "")) == tournament_id),
        next(
            (r for r in candidates if not str(r.get("tournament_id", "")).strip()), None
        ),
    )


def configured_embed(row, values):
    if not row:
        return discord.Embed()

    class Safe(dict):
        def __missing__(self, key):
            return "{" + key + "}"

    fmt = Safe(values)
    title = str(row.get("title", "")).format_map(fmt)
    body = str(row.get("description", row.get("content", ""))).format_map(fmt)
    raw = str(row.get("color", row.get("colour", "0"))).strip().lstrip("#")
    try:
        color = int(raw, 16)
    except ValueError:
        color = 0
    return discord.Embed(title=title, description=body, colour=color)
