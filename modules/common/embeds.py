from __future__ import annotations

"""Shared helpers for Discord embeds."""

from typing import Literal

import discord


EmbedCategory = Literal["admin", "recruitment", "community"]

SERVER_RULES_FAQ_BLUE = discord.Colour(0x4472C4)
SERVER_RULES_FAQ_GREEN = discord.Colour(0x356854)
SERVER_RULES_FAQ_YELLOW = discord.Colour(0xFFD666)
SERVER_RULES_FAQ_SLATE = discord.Colour(0x607D8B)

_COLOURS: dict[EmbedCategory, discord.Colour] = {
    "admin": discord.Colour(0xF200E5),
    "recruitment": discord.Colour(0x1B8009),
    "community": discord.Colour(0x3498DB),
}


def get_embed_colour(category: EmbedCategory) -> discord.Colour:
    """Return the embed colour for the given category."""

    return _COLOURS.get(category, discord.Colour.default())


__all__ = [
    "EmbedCategory",
    "SERVER_RULES_FAQ_BLUE",
    "SERVER_RULES_FAQ_GREEN",
    "SERVER_RULES_FAQ_YELLOW",
    "SERVER_RULES_FAQ_SLATE",
    "get_embed_colour",
]
