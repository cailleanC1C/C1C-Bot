from __future__ import annotations

"""Shared helpers for Discord embeds."""

from typing import Literal

import discord

EmbedCategory = Literal["admin", "recruitment", "community"]

_COLOURS: dict[EmbedCategory, discord.Colour] = {
    "admin": discord.Colour(0xF200E5),
    "recruitment": discord.Colour(0x1B8009),
    "community": discord.Colour(0x3498DB),
}

SERVER_RULES_PRIMARY_COLOUR = discord.Colour(0x4472C4)
SERVER_RULES_SPIRIT_COLOUR = discord.Colour(0x356854)
SERVER_RULES_QUICK_GUIDE_COLOUR = discord.Colour(0xFFD666)
SERVER_RULES_FAQ_COLOUR = discord.Colour(0x607D8B)

_SERVER_RULES_COLOURS: dict[str, discord.Colour] = {
    "#4472c4": SERVER_RULES_PRIMARY_COLOUR,
    "#356854": SERVER_RULES_SPIRIT_COLOUR,
    "#ffd666": SERVER_RULES_QUICK_GUIDE_COLOUR,
    "#607d8b": SERVER_RULES_FAQ_COLOUR,
}


def get_embed_colour(category: EmbedCategory) -> discord.Colour:
    """Return the embed colour for the given category."""

    return _COLOURS.get(category, discord.Colour.default())


def get_server_rules_colour(value: str) -> discord.Colour | None:
    return _SERVER_RULES_COLOURS.get(str(value).strip().lower())


__all__ = [
    "EmbedCategory",
    "SERVER_RULES_FAQ_COLOUR",
    "SERVER_RULES_PRIMARY_COLOUR",
    "SERVER_RULES_QUICK_GUIDE_COLOUR",
    "SERVER_RULES_SPIRIT_COLOUR",
    "get_embed_colour",
    "get_server_rules_colour",
]
