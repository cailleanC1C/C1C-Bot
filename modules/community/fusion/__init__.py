"""Fusion community extension."""

from __future__ import annotations

import logging

from discord.ext import commands

from .cog import FusionCog

log = logging.getLogger("c1c.community.fusion")

__all__ = ["FusionCog", "setup"]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(FusionCog(bot))
    log.info("Fusion extension loaded")
