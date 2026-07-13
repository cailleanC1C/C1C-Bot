from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from dataclasses import dataclass
from typing import Any, Mapping

import discord
from modules.common.embeds import get_embed_colour
from shared.sheets import async_core, recruitment

log = logging.getLogger("c1c.housekeeping.achievement_collector")

CONFIG_KEYS: tuple[str, ...] = (
    "achievement_collector_channel_id",
    "achievement_collector_default_limit",
    "achievement_collector_max_limit",
    "achievement_collector_min_count",
    "achievement_collector_schedule_rrule",
    "achievement_collector_schedule_time_utc",
    "achievement_collector_roles_tab",
)
LEADERBOARD_INTRO = "The shiny badges have been counted. Some of you are getting alarmingly shiny:"
RANK_FOOTER = "Want to snoop on another rank? Use !achievementcollector rank @member"
PREVIEW_FOOTER = "Preview only. Use !achievementcollector publish to send this to the configured channel."
NON_RAID_RANK_COPY = "{mention} is not on the collector board right now. No raid badge, no shiny leaderboard nonsense."



class AchievementCollectorError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AchievementCollectorConfig:
    channel_id: int
    default_limit: int
    max_limit: int
    min_count: int
    schedule_rrule: str
    schedule_time_utc: str
    roles_tab: str


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    member_id: int
    mention: str
    display_name: str
    count: int
    rank: int


@dataclass(frozen=True, slots=True)
class LeaderboardCache:
    guild_id: int
    built_at: dt.datetime
    entries: tuple[LeaderboardEntry, ...]
    counts: Mapping[int, int]


def _normalize_header(value: object) -> str:
    return str(value or "").strip().lower()


def _cell(row: list[Any], idx: int) -> str:
    if idx < 0 or idx >= len(row):
        return ""
    return str(row[idx] or "").strip()


async def _config_value(key: str) -> str:
    value = await recruitment.get_config_value_async(key)
    if value is None or not str(value).strip():
        raise AchievementCollectorError(f"Missing required Config key(s): {key}")
    return str(value).strip()


def _parse_int(value: str, key: str, *, minimum: int = 0) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise AchievementCollectorError(f"Config key {key} must be an integer.") from exc
    if parsed < minimum:
        raise AchievementCollectorError(f"Config key {key} must be >= {minimum}.")
    return parsed


async def resolve_config() -> AchievementCollectorConfig:
    values = {key: await _config_value(key) for key in CONFIG_KEYS}
    channel_id = _parse_int(values["achievement_collector_channel_id"], "achievement_collector_channel_id", minimum=1)
    default_limit = _parse_int(values["achievement_collector_default_limit"], "achievement_collector_default_limit", minimum=1)
    max_limit = _parse_int(values["achievement_collector_max_limit"], "achievement_collector_max_limit", minimum=1)
    min_count = _parse_int(values["achievement_collector_min_count"], "achievement_collector_min_count", minimum=0)
    if default_limit > max_limit:
        raise AchievementCollectorError("Config key achievement_collector_default_limit must not exceed achievement_collector_max_limit.")
    parse_schedule(values["achievement_collector_schedule_rrule"], values["achievement_collector_schedule_time_utc"])
    return AchievementCollectorConfig(channel_id, default_limit, max_limit, min_count, values["achievement_collector_schedule_rrule"], values["achievement_collector_schedule_time_utc"], values["achievement_collector_roles_tab"])


def parse_schedule(rrule_text: str, time_utc: str, *, now: dt.datetime | None = None) -> dt.datetime:
    try:
        hour_text, minute_text = str(time_utc).strip().split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except ValueError as exc:
        raise AchievementCollectorError("Config key achievement_collector_schedule_time_utc must use HH:MM UTC format.") from exc
    base = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    dtstart = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    try:
        rule_text = str(rrule_text).strip()
        if rule_text.upper().startswith("RRULE:"):
            rule_text = rule_text.split(":", 1)[1]
        parts = dict(part.split("=", 1) for part in rule_text.split(";") if part)
    except ValueError as exc:
        raise AchievementCollectorError("Config key achievement_collector_schedule_rrule must be valid iCalendar RRULE syntax.") from exc
    freq = parts.get("FREQ", "").upper()
    if freq not in {"MONTHLY", "WEEKLY", "DAILY"}:
        raise AchievementCollectorError("Config key achievement_collector_schedule_rrule must be valid iCalendar RRULE syntax.")

    def candidate_for(month_offset: int = 0, day: int | None = None) -> dt.datetime:
        year = base.year + ((base.month - 1 + month_offset) // 12)
        month = ((base.month - 1 + month_offset) % 12) + 1
        use_day = day if day is not None else base.day
        return dt.datetime(year, month, use_day, hour, minute, tzinfo=dt.timezone.utc)

    if freq == "DAILY":
        cand = dtstart
        return cand if cand >= base else cand + dt.timedelta(days=1)
    if freq == "WEEKLY":
        byday = parts.get("BYDAY", "").upper()
        weekdays = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
        wanted = weekdays.get(byday, base.weekday()) if byday else base.weekday()
        days = (wanted - base.weekday()) % 7
        cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0) + dt.timedelta(days=days)
        return cand if cand >= base else cand + dt.timedelta(days=7)
    # MONTHLY supports the deployed rule
    # FREQ=MONTHLY;BYDAY=MO;BYSETPOS=1 (first Monday of the month) plus
    # BYMONTHDAY for simpler monthly day-of-month schedules.
    weekdays = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}

    def monthly_byday_candidate(month_offset: int, weekday: int, setpos: int) -> dt.datetime:
        year = base.year + ((base.month - 1 + month_offset) // 12)
        month = ((base.month - 1 + month_offset) % 12) + 1
        first = dt.datetime(year, month, 1, hour, minute, tzinfo=dt.timezone.utc)
        if setpos > 0:
            delta_days = (weekday - first.weekday()) % 7
            day = 1 + delta_days + ((setpos - 1) * 7)
        else:
            if month == 12:
                next_month = dt.datetime(year + 1, 1, 1, hour, minute, tzinfo=dt.timezone.utc)
            else:
                next_month = dt.datetime(year, month + 1, 1, hour, minute, tzinfo=dt.timezone.utc)
            last = next_month - dt.timedelta(days=1)
            delta_days = (last.weekday() - weekday) % 7
            day = last.day - delta_days + ((setpos + 1) * 7)
        return dt.datetime(year, month, day, hour, minute, tzinfo=dt.timezone.utc)

    try:
        if "BYDAY" in parts:
            byday = parts["BYDAY"].upper()
            if "," in byday or byday not in weekdays:
                raise ValueError
            setpos = int(parts.get("BYSETPOS", "1"))
            if setpos == 0:
                raise ValueError
            for offset in range(0, 24):
                try:
                    cand = monthly_byday_candidate(offset, weekdays[byday], setpos)
                except ValueError:
                    continue
                if cand >= base:
                    return cand
        else:
            day = int(parts.get("BYMONTHDAY", "1"))
            if not 1 <= day <= 31:
                raise ValueError
            for offset in range(0, 24):
                try:
                    cand = candidate_for(offset, day)
                except ValueError:
                    continue
                if cand >= base:
                    return cand
    except ValueError as exc:
        raise AchievementCollectorError("Config key achievement_collector_schedule_rrule must be valid iCalendar RRULE syntax.") from exc
    raise AchievementCollectorError("Config key achievement_collector_schedule_rrule produced no future runs.")


def effective_limit(raw_limit: int | None, config: AchievementCollectorConfig) -> int:
    if raw_limit is None:
        return config.default_limit
    if raw_limit <= 0:
        raise AchievementCollectorError("Limit must be a positive integer.")
    return min(raw_limit, config.max_limit)


def resolve_raid_role_id(guild: discord.Guild) -> int:
    raw_role_id = os.getenv("RAID_ROLE_ID", "").strip()
    if not raw_role_id:
        raise AchievementCollectorError("Missing RAID_ROLE_ID; Achievement Collector requires the active clan raid role.")
    try:
        role_id = int(raw_role_id)
    except ValueError as exc:
        raise AchievementCollectorError("RAID_ROLE_ID must be a valid Discord role ID integer.") from exc
    if role_id <= 0:
        raise AchievementCollectorError("RAID_ROLE_ID must be a valid Discord role ID integer.")
    if guild.get_role(role_id) is None:
        raise AchievementCollectorError("RAID_ROLE_ID could not be resolved in this guild.")
    return role_id


def member_has_role(member: discord.Member, role_id: int) -> bool:
    return any(getattr(role, "id", None) == role_id for role in getattr(member, "roles", []) or [])


async def load_active_role_ids(config: AchievementCollectorConfig) -> set[int]:
    sheet_id = os.getenv("ACHIEVEMENTS_SHEET_ID", "").strip()
    if not sheet_id:
        raise AchievementCollectorError("Missing ACHIEVEMENTS_SHEET_ID.")
    matrix = await async_core.afetch_values(sheet_id, config.roles_tab)
    if not matrix:
        raise AchievementCollectorError("Achievement roles worksheet is empty.")
    headers = {_normalize_header(value): idx for idx, value in enumerate(matrix[0]) if _normalize_header(value)}
    missing = [name for name in ("role_id", "active") if name not in headers]
    if missing:
        raise AchievementCollectorError("Achievement roles worksheet missing required header(s): " + ", ".join("Active" if m == "active" else m for m in missing))
    role_col = headers["role_id"]
    active_col = headers["active"]
    role_ids: set[int] = set()
    for row in matrix[1:]:
        role_value = _cell(row, role_col)
        if not role_value:
            continue
        if _cell(row, active_col).lower() != "true":
            continue
        try:
            role_ids.add(int(role_value))
        except ValueError:
            log.warning("achievement collector stale/invalid role_id ignored", extra={"role_id": role_value})
    return role_ids


async def build_leaderboard(guild: discord.Guild, config: AchievementCollectorConfig) -> LeaderboardCache:
    raid_role_id = resolve_raid_role_id(guild)
    active_role_ids = await load_active_role_ids(config)
    existing_role_ids: set[int] = set()
    for role_id in active_role_ids:
        if guild.get_role(role_id) is None:
            log.warning("achievement collector stale/deleted role ignored", extra={"guild_id": guild.id, "role_id": role_id})
            continue
        existing_role_ids.add(role_id)

    rows: list[tuple[str, int, str, int]] = []
    counts: dict[int, int] = {}
    for member in getattr(guild, "members", []) or []:
        if getattr(member, "bot", False):
            continue
        if not member_has_role(member, raid_role_id):
            continue
        count = sum(1 for role in getattr(member, "roles", []) if getattr(role, "id", None) in existing_role_ids)
        counts[int(member.id)] = count
        if count >= config.min_count:
            rows.append((str(getattr(member, "display_name", "")), int(member.id), str(member.mention), count))
    rows.sort(key=lambda row: (-row[3], row[0].lower()))
    entries = tuple(LeaderboardEntry(member_id, mention, display_name, count, idx) for idx, (display_name, member_id, mention, count) in enumerate(rows, start=1))
    return LeaderboardCache(guild.id, dt.datetime.now(dt.timezone.utc), entries, counts)


def _achievements_word(count: int) -> str:
    return "achievement" if count == 1 else "achievements"


def leaderboard_embed(cache: LeaderboardCache, limit: int, *, preview: bool = False) -> discord.Embed:
    title = "Achievement Collectors Preview" if preview else "Achievement Collectors"
    if not cache.entries:
        embed = discord.Embed(title="Achievement Collectors", description="No achievement collectors found yet. Suspiciously unshiny.", colour=get_embed_colour("community"))
        embed.set_footer(text=RANK_FOOTER)
        return embed
    lines = [LEADERBOARD_INTRO, ""]
    for entry in cache.entries[:limit]:
        lines.append(f"{entry.rank}. {entry.mention} - {entry.count} {_achievements_word(entry.count)}")
    embed = discord.Embed(title=title, description="\n".join(lines), colour=get_embed_colour("community"))
    embed.set_footer(text=PREVIEW_FOOTER if preview else RANK_FOOTER)
    return embed


def non_raid_rank_embed(member: discord.Member) -> discord.Embed:
    embed = discord.Embed(title="Achievement Collector Rank", description=NON_RAID_RANK_COPY.format(mention=member.mention), colour=get_embed_colour("community"))
    embed.set_footer(text=RANK_FOOTER)
    return embed


def rank_embed(member: discord.Member, cache: LeaderboardCache) -> discord.Embed:
    entry = next((item for item in cache.entries if item.member_id == member.id), None)
    count = int(cache.counts.get(member.id, 0))
    mention = member.mention
    if count == 0:
        desc = f"{mention} has no counted achievements yet. Tragic. Fixable. Go collect shiny things."
    elif count == 1:
        desc = f"{mention} has 1 achievement. The collection has begun. Someone hide the shiny things."
    elif entry is None:
        desc = f"{mention} has {count} achievements. The hoard is growing, but not enough to make the board yet."
    elif entry.rank == 1:
        desc = f"{mention} is sitting at rank #1 with {count} achievements. Disgustingly shiny. Respectfully."
    elif entry.rank in (2, 3):
        desc = f"{mention} has {count} achievements and is rank #{entry.rank}. Dangerously shiny behaviour."
    elif 4 <= entry.rank <= 10:
        desc = f"{mention} is rank #{entry.rank} with {count} achievements. Not the throne, but definitely in the shiny danger zone."
    else:
        desc = f"{mention} has {count} achievements and is rank #{entry.rank}. The hoard is growing. Slowly. Suspiciously."
    embed = discord.Embed(title="Achievement Collector Rank", description=desc, colour=get_embed_colour("community"))
    embed.set_footer(text=RANK_FOOTER)
    return embed


async def resolve_messageable(bot: discord.Client, channel_id: int) -> Any | None:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)  # type: ignore[attr-defined]
        except Exception:
            return None
    return channel if hasattr(channel, "send") else None


class AchievementCollectorScheduler:
    def __init__(self, cog: Any) -> None:
        self.cog = cog
        self.task: asyncio.Task[None] | None = None
        self.last_post_key: str | None = None

    def start(self) -> None:
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run(), name="achievement_collector_scheduler")

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()

    async def _run(self) -> None:
        await self.cog.bot.wait_until_ready()
        while not self.cog.bot.is_closed():
            try:
                config = await resolve_config()
                next_run = parse_schedule(config.schedule_rrule, config.schedule_time_utc)
                await asyncio.sleep(max(0.0, (next_run - dt.datetime.now(dt.timezone.utc)).total_seconds()))
                post_key = next_run.strftime("%Y-%m-%dT%H:%MZ")
                if post_key != self.last_post_key:
                    self.last_post_key = post_key
                    await self.cog.publish_scheduled(config)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("achievement collector scheduler failed; retrying after backoff")
                await asyncio.sleep(3600)


__all__ = [
    "AchievementCollectorConfig", "AchievementCollectorError", "AchievementCollectorScheduler",
    "LeaderboardCache", "LeaderboardEntry", "NON_RAID_RANK_COPY", "build_leaderboard",
    "effective_limit", "leaderboard_embed", "load_active_role_ids", "member_has_role",
    "non_raid_rank_embed", "parse_schedule", "rank_embed", "resolve_config",
    "resolve_messageable", "resolve_raid_role_id",
]
