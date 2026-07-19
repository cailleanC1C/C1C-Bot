from __future__ import annotations

import logging
import os
import re
import traceback

import discord
from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only
from modules.common import runtime as runtime_helpers
from modules.housekeeping.achievement_collector import (
    AchievementCollectorError,
    LeaderboardCache,
    build_leaderboard,
    effective_limit,
    leaderboard_embed,
    member_has_role,
    non_raid_rank_embed,
    rank_embed,
    resolve_config,
    resolve_raid_role_id,
    resolve_messageable,
)

log = logging.getLogger("c1c.housekeeping.achievement_collector.cog")
_ALLOWED_NONE = discord.AllowedMentions.none()


_SENSITIVE_EXCEPTION_DETAIL_RE = re.compile(r"\b(sheet contents?|message content|credentials?|token)\s*:", re.IGNORECASE)


def _short_exception_message(exc: BaseException) -> str:
    message = " ".join(str(exc).split()) or "-"
    sheet_id = os.getenv("ACHIEVEMENTS_SHEET_ID")
    if sheet_id:
        message = message.replace(sheet_id, "[redacted_sheet_id]")
    sensitive_match = _SENSITIVE_EXCEPTION_DETAIL_RE.search(message)
    if sensitive_match:
        message = message[: sensitive_match.start()].rstrip(" -:;•") or "[redacted]"
    return message[:180]


def _context_id(obj: object, attr: str) -> int | None:
    return getattr(getattr(obj, attr, None), "id", None)


def _error_embed(message: str) -> discord.Embed:
    return discord.Embed(title="Achievement Collector", description=message, colour=discord.Colour.red())


def _traceback_file_label(filename: str) -> str:
    try:
        return os.path.relpath(filename)
    except ValueError:
        return filename


def _exception_traceback_metadata(exc: BaseException) -> dict[str, object]:
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return {
            "exception_origin_file": None,
            "exception_origin_line": None,
            "exception_origin_function": None,
            "exception_trace_frames": [],
        }

    recent_frames = frames[-8:]
    trace_frames = [
        {
            "file": _traceback_file_label(frame.filename),
            "line": frame.lineno,
            "function": frame.name,
        }
        for frame in recent_frames
    ]
    origin = trace_frames[-1]
    return {
        "exception_origin_file": origin["file"],
        "exception_origin_line": origin["line"],
        "exception_origin_function": origin["function"],
        "exception_trace_frames": trace_frames,
    }


def _exception_origin_marker(traceback_metadata: dict[str, object]) -> str | None:
    origin_file = traceback_metadata.get("exception_origin_file")
    origin_line = traceback_metadata.get("exception_origin_line")
    origin_function = traceback_metadata.get("exception_origin_function")
    if not origin_file or not origin_line:
        return None
    if origin_function:
        return f"{origin_file}:{origin_line} {origin_function}"
    return f"{origin_file}:{origin_line}"


class AchievementCollectorCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._cache: LeaderboardCache | None = None

    async def cog_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.BadArgument):
            await ctx.send(embed=_error_embed("Limit must be a positive integer."), allowed_mentions=_ALLOWED_NONE)
            return
        raise error

    async def _report_collector_failure(
        self,
        ctx: commands.Context,
        command_name: str,
        exc: BaseException,
        *,
        limit: int | None = None,
        target_member: discord.Member | None = None,
    ) -> None:
        traceback_metadata = _exception_traceback_metadata(exc)
        log.error(
            "achievement collector %s failed",
            command_name,
            exc_info=(type(exc), exc, exc.__traceback__),
            extra={
                "achievement_collector_command": command_name,
                "guild_id": _context_id(ctx, "guild"),
                "channel_id": _context_id(ctx, "channel"),
                "actor_id": _context_id(ctx, "author"),
                "provided_limit": limit,
                "target_member_id": getattr(target_member, "id", None),
                "exception_type": type(exc).__name__,
                **traceback_metadata,
            },
        )
        fields = [
            "feature=achievement collector",
            f"command={command_name}",
            f"guild_id={_context_id(ctx, 'guild')}",
            f"channel_id={_context_id(ctx, 'channel')}",
            f"actor_id={_context_id(ctx, 'author')}",
            f"exception_type={type(exc).__name__}",
            f"exception={_short_exception_message(exc)}",
        ]
        origin_marker = _exception_origin_marker(traceback_metadata)
        if origin_marker:
            fields.append(f"origin={origin_marker}")
        if limit is not None:
            fields.append(f"limit={limit}")
        if target_member is not None:
            fields.append(f"target_member_id={getattr(target_member, 'id', None)}")
        try:
            await runtime_helpers.send_log_message("❌ " + " • ".join(fields))
        except Exception:
            log.warning("failed to send achievement collector ops failure report", exc_info=True)

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
            await self._report_collector_failure(ctx, "preview", exc, limit=limit)
            await ctx.send(embed=_error_embed(str(exc)), allowed_mentions=_ALLOWED_NONE)
            return
        except Exception as exc:
            await self._report_collector_failure(ctx, "preview", exc, limit=limit)
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
            await self._report_collector_failure(ctx, "publish", exc, limit=limit)
            await ctx.send(embed=_error_embed(str(exc)), allowed_mentions=_ALLOWED_NONE)
            return
        except Exception as exc:
            await self._report_collector_failure(ctx, "publish", exc, limit=limit)
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
            raid_role_id = resolve_raid_role_id(ctx.guild)
            cache = await self._get_or_build_cache(ctx.guild)
        except AchievementCollectorError as exc:
            await self._report_collector_failure(ctx, "rank", exc, target_member=member)
            await ctx.send(embed=_error_embed(str(exc)), allowed_mentions=_ALLOWED_NONE)
            return
        except Exception as exc:
            await self._report_collector_failure(ctx, "rank", exc, target_member=member)
            await ctx.send(embed=_error_embed("Achievement Collector rank failed. Check the bot logs."), allowed_mentions=_ALLOWED_NONE)
            return
        if not member_has_role(target, raid_role_id):
            await ctx.send(embed=non_raid_rank_embed(target), allowed_mentions=_ALLOWED_NONE)
            return
        await ctx.send(embed=rank_embed(target, cache), allowed_mentions=_ALLOWED_NONE)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AchievementCollectorCog(bot))
