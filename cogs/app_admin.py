"""App-level administrative commands registered under the cogs namespace."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import discord
from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.help import build_coreops_footer
from c1c_coreops.rbac import admin_only
from modules.common import feature_flags, runtime as runtime_helpers
from modules.common.discord_utils import resolve_message_target
from modules.common.logs import channel_label
from modules.ops import cluster_role_map, server_map
from shared.config import get_who_we_are_channel_id
from shared.sheets import recruitment as recruitment_sheet


log = logging.getLogger(__name__)

CACHE_REFRESH_JOBS = {
    "cache_refresh:clans",
    "cache_refresh:templates",
    "cache_refresh:clan_tags",
    "cache_refresh:onboarding_questions",
}
RECRUITMENT_JOBS = {
    "onboarding_idle_watcher",
    "welcome_incomplete_scan",
}
HOUSEKEEPING_JOBS = {
    "server_map_refresh",
    "cleanup_watcher",
    "housekeeping_keepalive",
}
GROUP_ORDER = ("Cache Refresh", "recruitment", "housekeeping", "other")


def _format_interval_label(delta: timedelta) -> str:
    seconds = int(delta.total_seconds())
    if seconds % 3600 == 0:
        hours = max(1, seconds // 3600)
        return f"every {hours} hrs"
    minutes = max(1, int(round(seconds / 60)))
    return f"every {minutes} min"


def _job_identifier(job: object) -> str:
    return getattr(job, "name", None) or getattr(job, "tag", None) or "job"


def _group_for_job(job_name: str) -> str:
    if job_name in CACHE_REFRESH_JOBS:
        return "Cache Refresh"
    if job_name in RECRUITMENT_JOBS:
        return "recruitment"
    if job_name in HOUSEKEEPING_JOBS:
        return "housekeeping"
    return "other"


def _display_job_name(job_name: str, group_name: str) -> str:
    if group_name == "Cache Refresh" and job_name.startswith("cache_refresh:"):
        return job_name.split("cache_refresh:", 1)[1]
    return job_name


def _job_sort_key(job: object) -> tuple[datetime, str]:
    next_run = getattr(job, "next_run", None)
    if isinstance(next_run, datetime):
        next_key = next_run
    else:
        next_key = datetime.max.replace(tzinfo=timezone.utc)
    job_name = _job_identifier(job)
    return (next_key, job_name)


def _format_next_run(next_run: datetime | None) -> str:
    if next_run is None:
        return "pending"
    try:
        return next_run.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return "pending"


def _chunk_job_entries(entries: list[list[str]], limit: int = 1024) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for entry in entries:
        entry_text = "\n".join(entry)
        projected = current_len + len(entry_text) + (1 if current else 0)
        if current and projected > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        current.append(entry_text)
        current_len += len(entry_text) + (1 if current_len else 0)
    if current:
        chunks.append("\n".join(current))
    return chunks or ["—"]


def _build_scheduler_embeds(runtime, component: str | None) -> list[discord.Embed]:
    jobs = runtime.scheduler.jobs if hasattr(runtime, "scheduler") else []
    filter_token = component.strip().lower() if component else None
    if filter_token:
        jobs = [
            job
            for job in jobs
            if (getattr(job, "component", "") or "").lower() == filter_token
        ]

    title = "Scheduler — Next Runs"
    if filter_token:
        title = f"{title} ({filter_token})"

    embed_color = discord.Colour.blurple()
    footer_text = build_coreops_footer(bot_version=os.getenv("BOT_VERSION", "dev"))

    if not jobs:
        scope = filter_token or "any component"
        embed = discord.Embed(title=title, colour=embed_color)
        embed.description = f"No scheduled jobs under {scope}."
        embed.set_footer(text=footer_text)
        return [embed]

    grouped: dict[str, list] = {group: [] for group in GROUP_ORDER}
    for job in jobs:
        job_name = _job_identifier(job)
        group_name = _group_for_job(job_name)
        grouped.setdefault(group_name, []).append(job)

    fields: list[tuple[str, str]] = []
    for group_name in GROUP_ORDER:
        group_jobs = grouped.get(group_name) or []
        if not group_jobs:
            continue
        entries: list[list[str]] = []
        for job in sorted(group_jobs, key=_job_sort_key):
            job_name = _job_identifier(job)
            display_name = _display_job_name(job_name, group_name)
            next_label = _format_next_run(getattr(job, "next_run", None))
            interval = getattr(job, "interval", None)
            cadence = _format_interval_label(interval) if interval else "every ?"
            entries.append(
                [
                    f"• {display_name}",
                    f"  next: {next_label} ({cadence})",
                ]
            )
        for chunk in _chunk_job_entries(entries):
            fields.append((group_name, chunk))

    embeds: list[discord.Embed] = []
    current = discord.Embed(title=title, colour=embed_color)
    current_len = len(title)
    field_count = 0
    reserve = len(footer_text)

    for name, value in fields:
        field_len = len(name) + len(value)
        if (
            field_count >= 25
            or current_len + field_len + reserve > 6000
        ):
            current.set_footer(text=footer_text)
            embeds.append(current)
            current = discord.Embed(title=title, colour=embed_color)
            current_len = len(title)
            field_count = 0
        current.add_field(name=name, value=value, inline=False)
        current_len += field_len
        field_count += 1

    if not embeds or field_count:
        current.set_footer(text=footer_text)
        embeds.append(current)

    return embeds

class AppAdmin(commands.Cog):
    """Lightweight administrative utilities for bot operators."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        command = self.bot.get_command("ping")
        if command is None:
            return

        extras = getattr(command, "extras", None)
        if not isinstance(extras, dict):
            extras = {}
            setattr(command, "extras", extras)

        extras.setdefault("function_group", "operational")
        extras.setdefault("access_tier", "admin")

        try:
            setattr(command, "function_group", "operational")
        except Exception:
            pass
        try:
            setattr(command, "access_tier", "admin")
        except Exception:
            pass

        coreops = self.bot.get_cog("CoreOpsCog")
        apply_attrs = getattr(coreops, "_apply_metadata_attributes", None)
        if callable(apply_attrs):
            apply_attrs(command)

    @tier("admin")
    @help_metadata(
        function_group="operational",
        section="utilities",
        access_tier="admin",
    )
    @commands.command(
        name="ping",
        hidden=True,
        help="Quick admin check to confirm the bot is responsive.",
    )
    @admin_only()
    async def ping(self, ctx: commands.Context) -> None:
        """React with a paddle to confirm the bot processed the request."""

        try:
            await ctx.message.add_reaction("🏓")
        except Exception:
            # Reaction failures are non-fatal (missing perms, deleted message, etc.).
            pass

    @tier("admin")
    @help_metadata(
        function_group="operational",
        section="utilities",
        access_tier="admin",
    )
    @commands.group(
        name="servermap",
        invoke_without_command=True,
        hidden=True,
        help="Admin tools for the automated #server-map post.",
    )
    @admin_only()
    async def servermap(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        await ctx.reply("Usage: !servermap refresh", mention_author=False)

    @servermap.command(
        name="refresh",
        help="Rebuild the #server-map channel immediately from the live guild structure.",
    )
    @admin_only()
    async def servermap_refresh(self, ctx: commands.Context) -> None:
        if not feature_flags.is_enabled("SERVER_MAP"):
            await ctx.reply(
                "Server map feature is currently disabled in FeatureToggles.",
                mention_author=False,
            )
            await runtime_helpers.send_log_message(
                "📘 Server map — skipped • reason=feature_disabled"
            )
            return

        result = await server_map.refresh_server_map(
            self.bot, force=True, actor="command", requested_channel="ctx"
        )
        if result.status == "ok":
            await ctx.reply(
                f"Server map refreshed — messages={result.message_count} • chars={result.total_chars}.",
                mention_author=False,
            )
            return

        if result.status == "disabled":
            await ctx.reply(
                "Server map feature is currently disabled in FeatureToggles.",
                mention_author=False,
            )
            return
        reason = result.reason or "unknown"
        await ctx.reply(
            f"Server map refresh failed ({reason}). Check logs for details.",
            mention_author=False,
        )

    @tier("admin")
    @help_metadata(
        function_group="operational",
        section="utilities",
        access_tier="admin",
    )
    @commands.command(
        name="next",
        hidden=True,
        help="Show upcoming scheduled jobs. Optionally filter by component.",
    )
    @admin_only()
    async def next_jobs(self, ctx: commands.Context, component: str | None = None) -> None:
        runtime = runtime_helpers.get_active_runtime()
        if runtime is None:
            await ctx.reply("Scheduler unavailable.", mention_author=False)
            return
        embeds = _build_scheduler_embeds(runtime, component)
        if not embeds:
            await ctx.reply("Scheduler unavailable.", mention_author=False)
            return
        await ctx.reply(embed=embeds[0], mention_author=False)
        for embed in embeds[1:]:
            await ctx.send(embed=embed)

    @tier("admin")
    @help_metadata(
        function_group="operational",
        section="utilities",
        access_tier="admin",
    )
    @commands.command(
        name="whoweare",
        hidden=True,
        help="Generate the live Who We Are overview from the WhoWeAre sheet.",
    )
    @admin_only()
    async def whoweare(self, ctx: commands.Context) -> None:
        guild = getattr(ctx, "guild", None)
        guild_name = getattr(guild, "name", "unknown guild")

        if not feature_flags.is_enabled("ClusterRoleMap"):
            await ctx.reply(
                "Cluster role map feature is disabled in FeatureToggles.",
                mention_author=False,
            )
            await runtime_helpers.send_log_message(
                f"📘 **Cluster role map** — cmd=whoweare • guild={guild_name} • status=disabled"
            )
            return

        if guild is None:
            await ctx.reply(
                "This command can only be used inside a Discord guild.",
                mention_author=False,
            )
            return

        tab_name = recruitment_sheet.get_role_map_tab_name()
        try:
            entries = await cluster_role_map.fetch_role_map_rows(tab_name=tab_name)
        except cluster_role_map.RoleMapLoadError as exc:
            await ctx.reply(
                f"I couldn’t read the role map sheet (`{tab_name}`). Please check Config and try again.",
                mention_author=False,
            )
            reason = str(exc) or "unknown"
            await runtime_helpers.send_log_message(
                f"📘 **Cluster role map** — cmd=whoweare • guild={guild_name} • status=error • reason={reason}"
            )
            return

        channel_id = get_who_we_are_channel_id()
        try:
            target_channel = await resolve_message_target(self.bot, channel_id)
        except (TypeError, ValueError):
            await ctx.reply(
                "I couldn’t determine where to post the role map. Please try again in a guild channel.",
                mention_author=False,
            )
            await runtime_helpers.send_log_message(
                f"📘 **Cluster role map** — cmd=whoweare • guild={guild_name} "
                "• status=error • reason=invalid_channel"
            )
            return
        except PermissionError:
            await ctx.reply(
                "I couldn’t post the role map. Please check channel permissions and try again.",
                mention_author=False,
            )
            await runtime_helpers.send_log_message(
                f"📘 **Cluster role map** — cmd=whoweare • guild={guild_name} "
                "• status=error • reason=forbidden_channel"
            )
            return
        except RuntimeError as exc:
            await ctx.reply(
                "I couldn’t determine where to post the role map right now. Please try again shortly.",
                mention_author=False,
            )
            await runtime_helpers.send_log_message(
                f"📘 **Cluster role map** — cmd=whoweare • guild={guild_name} "
                f"• status=error • reason={exc}"
            )
            return

        guild = getattr(target_channel, "guild", guild)
        guild_name = getattr(guild, "name", "unknown guild")
        render = cluster_role_map.build_role_map_render(guild, entries)

        requested_label = channel_label(guild, channel_id)
        target_label = channel_label(guild, getattr(target_channel, "id", None))
        await runtime_helpers.send_log_message(
            "📘 **Cluster role map** — "
            f"cmd=whoweare • guild={guild_name} "
            f"• channel_fallback={target_label} • requested_channel={requested_label}"
        )

        cleaned = 0
        bot_user = getattr(self.bot, "user", None)
        bot_user_id = getattr(bot_user, "id", None)
        try:
            cleaned = await cluster_role_map.cleanup_previous_role_map_messages(
                target_channel,
                bot_id=bot_user_id,
            )
        except Exception:  # pragma: no cover - defensive logging
            log_reason = "cleanup_failed"
            await runtime_helpers.send_log_message(
                f"📘 **Cluster role map** — cmd=whoweare • guild={guild_name} • status=warning • reason={log_reason}"
            )
        else:
            if cleaned:
                await runtime_helpers.send_log_message(
                    f"📘 **Cluster role map** — cmd=whoweare • guild={guild_name} • cleaned_messages={cleaned}"
                )

        try:
            index_message = await target_channel.send(cluster_role_map.build_index_placeholder())
        except discord.HTTPException as exc:
            reason = str(exc) or "index_send_failed"
            await ctx.reply(
                "I couldn’t post the role map. Please check channel permissions and try again.",
                mention_author=False,
            )
            await runtime_helpers.send_log_message(
                f"📘 **Cluster role map** — cmd=whoweare • guild={guild_name} "
                f"• status=error • step=index_send • reason={reason}"
            )
            return

        jump_entries: list[cluster_role_map.IndexLink] = []
        for category in render.categories:
            try:
                embeds = cluster_role_map.build_category_embeds(category)
                fallback_messages = []
                if not embeds:
                    fallback_messages = cluster_role_map.build_category_fallback_messages(category)
                if not embeds and not fallback_messages:
                    continue
                if embeds:
                    message = None
                    for embed in embeds:
                        sent = await target_channel.send(
                            content=cluster_role_map.INVISIBLE_MARKER,
                            embed=embed,
                        )
                        if message is None:
                            message = sent
                else:
                    message = None
                    for body in fallback_messages:
                        sent = await target_channel.send(body)
                        if message is None:
                            message = sent
            except discord.HTTPException as exc:
                reason = str(exc) or "category_send_failed"
                await runtime_helpers.send_log_message(
                    f"📘 **Cluster role map** — cmd=whoweare • guild={guild_name} "
                    f"• status=error • step=category_send • category={category.name} • reason={reason}"
                )
                continue
            if message is None:
                continue
            jump_entries.append(
                cluster_role_map.IndexLink(
                    name=category.name,
                    emoji=category.emoji,
                    url=cluster_role_map.build_jump_url(
                        getattr(guild, "id", 0),
                        getattr(target_channel, "id", 0),
                        getattr(message, "id", 0),
                    ),
                )
            )

        if not jump_entries:
            if render.category_count:
                empty_reason = "_(Unable to post any categories — check channel permissions and try again.)_"
            else:
                empty_reason = "_(No categories are currently available — check the WhoWeAre sheet.)_"
        else:
            empty_reason = None

        try:
            final_index = cluster_role_map.build_index_message(jump_entries, empty_reason=empty_reason)
            await index_message.edit(content=final_index)
        except discord.HTTPException as exc:
            reason = str(exc) or "index_edit_failed"
            await runtime_helpers.send_log_message(
                f"📘 **Cluster role map** — cmd=whoweare • guild={guild_name} "
                f"• status=error • step=index_edit • reason={reason}"
            )

        if getattr(target_channel, "id", None) == getattr(ctx.channel, "id", None):
            ack_message = "Cluster role map updated."
        else:
            mention = getattr(target_channel, "mention", None)
            if not mention:
                label = channel_label(guild, getattr(target_channel, "id", None))
                mention = label or "the configured channel"
            ack_message = f"Cluster role map refreshed in {mention}."
        await ctx.reply(ack_message, mention_author=False)

        target_label = channel_label(guild, getattr(target_channel, "id", None))
        await runtime_helpers.send_log_message(
            "📘 **Cluster role map** — "
            f"cmd=whoweare • guild={guild_name} • categories={render.category_count} "
            f"• roles={render.role_count} • unassigned_roles={render.unassigned_roles} "
            f"• category_messages={len(jump_entries)} • target_channel={target_label}"
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AppAdmin(bot))
