"""Live Arena tournament registration extension."""

import logging
from .cog import LiveArenaTournamentCog

log = logging.getLogger("c1c.community.live_arena_registration")


async def setup(bot):
    cog = LiveArenaTournamentCog(bot)
    await bot.add_cog(cog)
    await cog.prepare()


__all__ = ["LiveArenaTournamentCog", "setup"]
