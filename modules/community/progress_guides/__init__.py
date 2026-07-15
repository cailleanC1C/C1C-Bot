"""Read-only progress guide panel publishing."""

from __future__ import annotations

import logging

from discord.ext import commands

from .cog import ProgressGuidesCog

log = logging.getLogger("c1c.community.progress_guides")

__all__ = ["ProgressGuidesCog", "setup"]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ProgressGuidesCog(bot))
    log.info("Progress guides extension loaded")
