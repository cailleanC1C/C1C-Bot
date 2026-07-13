from __future__ import annotations

import logging

import discord
from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only
from modules.housekeeping.achievement_collector import (
    AchievementCollectorError,
    AchievementCollectorScheduler,
    LeaderboardCache,
    build_leaderboard,
    effective_limit,
    leaderboard_embed,
    rank_embed,
    resolve_config,
    resolve_messageable,
)

log = logging.getLogger("c1c.housekeeping.achievement_collector.cog")
_ALLOWED_NONE = discord.AllowedMentions.none()


def _error_embed(message: str) -> discord.Embed:
    return discord.Embed(title="Achievement Collector", description=message, colour=discord.Colour.red())


class AchievementCollectorCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._cache: LeaderboardCache | None = None
        self._scheduler = AchievementCollectorScheduler(self)

    async def cog_load(self) -> None:
        self._scheduler.start()

    async def cog_unload(self) -> None:
        self._scheduler.cancel()

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send(embed=_error_embed("Limit must be a positive integer."), allowed_mentions=_ALLOWED_NONE)
            return
        raise error

    async def _get_or_build_cache(self, guild: discord.Guild) -> LeaderboardCache:
        if self._cache is not None and self._cache.guild_id == guild.id:
            return self._cache
        config = await resolve_config()
        self._cache = await build_leaderboard(guild, config)
        return self._cache

    async def _rebuild(self, guild: discord.Guild) -> tuple[object, LeaderboardCache]:
        config = await resolve_config()
        cache = await build_leaderboard(guild, config)
        self._cache = cache
        return config, cache

    async def publish_scheduled(self, config=None) -> None:
        guilds = list(getattr(self.bot, "guilds", []) or [])
        if not guilds:
            log.warning("achievement collector scheduled post skipped; no guilds")
            return
        guild = guilds[0]
        if config is None:
            config = await resolve_config()
        cache = await build_leaderboard(guild, config)
        self._cache = cache
        channel = await resolve_messageable(self.bot, config.channel_id)
        if channel is None:
            raise AchievementCollectorError("Invalid achievement_collector_channel_id.")
        await channel.send(embed=leaderboard_embed(cache, config.default_limit), allowed_mentions=_ALLOWED_NONE)

    @tier("public")
    @help_metadata(function_group="community", section="achievements", access_tier="public")
    @commands.group(name="achievementcollector", invoke_without_command=True, help="Achievement Collector leaderboard.")
    async def achievementcollector_group(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is None:
            await ctx.send(embed=_error_embed('Use "!achievementcollector preview", "publish", or "rank".'), allowed_mentions=_ALLOWED_NONE)

    @tier("admin")
    @help_metadata(function_group="operational", section="achievements", access_tier="admin")
    @achievementcollector_group.command(name="preview")
    @admin_only()
    async def preview(self, ctx: commands.Context, limit: int | None = None) -> None:
        if ctx.guild is None:
            await ctx.send(embed=_error_embed("This command can only be used in a server."), allowed_mentions=_ALLOWED_NONE)
            return
        try:
            config, cache = await self._rebuild(ctx.guild)
            resolved_limit = effective_limit(limit, config)  # type: ignore[arg-type]
        except AchievementCollectorError as exc:
            await ctx.send(embed=_error_embed(str(exc)), allowed_mentions=_ALLOWED_NONE)
            return
        except Exception:
            log.exception("achievement collector preview failed")
            await ctx.send(embed=_error_embed("Achievement Collector preview failed. Check the bot logs."), allowed_mentions=_ALLOWED_NONE)
            return
        await ctx.send(embed=leaderboard_embed(cache, resolved_limit, preview=True), allowed_mentions=_ALLOWED_NONE)

    @tier("admin")
    @help_metadata(function_group="operational", section="achievements", access_tier="admin")
    @achievementcollector_group.command(name="publish")
    @admin_only()
    async def publish(self, ctx: commands.Context, limit: int | None = None) -> None:
        if ctx.guild is None:
            await ctx.send(embed=_error_embed("This command can only be used in a server."), allowed_mentions=_ALLOWED_NONE)
            return
        try:
            config, cache = await self._rebuild(ctx.guild)
            resolved_limit = effective_limit(limit, config)  # type: ignore[arg-type]
            channel = await resolve_messageable(self.bot, config.channel_id)  # type: ignore[attr-defined]
            if channel is None:
                raise AchievementCollectorError("Invalid achievement_collector_channel_id.")
            await channel.send(embed=leaderboard_embed(cache, resolved_limit), allowed_mentions=_ALLOWED_NONE)
        except AchievementCollectorError as exc:
            await ctx.send(embed=_error_embed(str(exc)), allowed_mentions=_ALLOWED_NONE)
            return
        except Exception:
            log.exception("achievement collector publish failed")
            await ctx.send(embed=_error_embed("Achievement Collector publish failed. Check the bot logs."), allowed_mentions=_ALLOWED_NONE)
            return
        await ctx.send(embed=discord.Embed(title="Achievement Collector", description="Published Achievement Collectors leaderboard.", colour=discord.Colour.green()), allowed_mentions=_ALLOWED_NONE)

    @tier("public")
    @help_metadata(function_group="community", section="achievements", access_tier="public")
    @achievementcollector_group.command(name="rank")
    async def rank(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        if ctx.guild is None:
            await ctx.send(embed=_error_embed("This command can only be used in a server."), allowed_mentions=_ALLOWED_NONE)
            return
        target = member or ctx.author
        try:
            cache = await self._get_or_build_cache(ctx.guild)
        except AchievementCollectorError as exc:
            await ctx.send(embed=_error_embed(str(exc)), allowed_mentions=_ALLOWED_NONE)
            return
        except Exception:
            log.exception("achievement collector rank failed")
            await ctx.send(embed=_error_embed("Achievement Collector rank failed. Check the bot logs."), allowed_mentions=_ALLOWED_NONE)
            return
        await ctx.send(embed=rank_embed(target, cache), allowed_mentions=_ALLOWED_NONE)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AchievementCollectorCog(bot))
