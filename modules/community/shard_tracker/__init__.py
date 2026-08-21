"""Shard & Mercy tracker extension."""

import logging

from discord.ext import commands

from .interaction_safe_cog import ShardTracker

__all__ = ["ShardTracker", "setup"]


async def setup(bot: commands.Bot) -> None:
    """Load the interaction-safe ShardTracker cog."""

    await bot.add_cog(ShardTracker(bot))
    logging.getLogger("c1c.shards.cog").info("Shard tracker cog loaded")
