"""Configured Discord embed rendering."""

from __future__ import annotations
import string
import discord
from .models import SchemaError, norm, truthy


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
        raise SchemaError("Required configured message row is missing.")
    title_template = str(row.get("title_template", row.get("title", "")))
    body_template = str(
        row.get("body_template", row.get("description", row.get("content", "")))
    )
    required = {
        name
        for template in (title_template, body_template)
        for _, name, _, _ in string.Formatter().parse(template)
        if name
    }
    missing = sorted(
        name for name in required if name not in values or values[name] is None
    )
    if missing:
        raise SchemaError("Message template requires values: " + ", ".join(missing))
    try:
        title = title_template.format_map(values)
        body = body_template.format_map(values)
    except (KeyError, ValueError) as exc:
        raise SchemaError(f"Invalid configured message template: {exc}") from exc
    if "{" in title or "{" in body:
        raise SchemaError("Configured message contains an unresolved placeholder.")
    raw = (
        str(row.get("embed_color_hex", row.get("color", row.get("colour", "0"))))
        .strip()
        .lstrip("#")
    )
    try:
        color = int(raw, 16)
    except ValueError:
        color = 0
    return discord.Embed(title=title, description=body, colour=color)
