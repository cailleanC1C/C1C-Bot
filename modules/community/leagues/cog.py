from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
import os
from typing import TYPE_CHECKING, Iterable, Mapping

import discord
from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only
from modules.community.leagues.config import (
    LeagueBundle,
    LeagueSpec,
    LeaguesConfigError,
    load_league_bundles,
)
from shared.logfmt import channel_label, user_label
from shared.sheets.export_utils import export_pdf_as_png, get_tab_gid

if TYPE_CHECKING:
    from modules.community.reaction_roles import ReactionRolesCog

log = logging.getLogger("c1c.community.leagues")

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class LeaguesCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._last_wednesday_message_id: int | None = None
        self._handled_messages: set[int] = set()
        self._job_lock = asyncio.Lock()

        sheet_id = os.getenv("LEAGUES_SHEET_ID", "").strip()
        if not sheet_id:
            log.warning("Leagues sheet ID missing at startup; feature will remain idle")

    # === Helpers ===
    @staticmethod
    def _parse_int_env(key: str) -> int | None:
        raw = os.getenv(key)
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _admin_ids() -> list[int]:
        raw = os.getenv("LEAGUE_ADMIN_IDS", "")
        admin_ids: list[int] = []
        for part in raw.split(","):
            token = part.strip()
            if not token:
                continue
            try:
                admin_ids.append(int(token))
            except (TypeError, ValueError):
                continue
        return admin_ids

    @staticmethod
    def _is_image_attachment(attachment: discord.Attachment) -> bool:
        content_type = (attachment.content_type or "").lower()
        if content_type.startswith("image/"):
            return True
        name = (attachment.filename or "").lower()
        return any(name.endswith(ext) for ext in _IMAGE_EXTENSIONS)

    async def _resolve_channel(self, channel_id: int | None) -> discord.abc.Messageable | None:
        if channel_id is None:
            return None
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except Exception:
                return None
        if isinstance(channel, (discord.TextChannel, discord.Thread)):
            return channel
        return None

    def _admin_mentions_text(self) -> str:
        admin_ids = self._admin_ids()
        if not admin_ids:
            return ""
        return " ".join(f"<@{user_id}>" for user_id in admin_ids)

    @staticmethod
    def _league_title(bundle: LeagueBundle, now: dt.datetime) -> str:
        today = now.date().isoformat()
        return f"{bundle.display_name} – Weekly Update {today}"

    # === Event listeners ===
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        channel = getattr(message, "channel", None)
        if not channel or getattr(channel, "id", None) != self._parse_int_env(
            "LEAGUES_SUBMISSION_CHANNEL_ID"
        ):
            return
        if not any(self._is_image_attachment(att) for att in message.attachments):
            return
        guild = getattr(message, "guild", None)
        if not isinstance(guild, discord.Guild):
            return

        role_id = self._parse_int_env("C1C_LEAGUE_ROLE_ID")
        if not role_id:
            return
        role = guild.get_role(role_id)
        member = getattr(message, "author", None)
        if not isinstance(member, discord.Member) or role is None:
            return

        if role in getattr(member, "roles", []):
            return

        try:
            await member.add_roles(role, reason="C1C Leagues: submission role grant")
        except Exception:
            log.exception("failed to assign C1CLeague role", extra={"member": member.id})
            return

        try:
            log.info(
                "✅ C1C Leagues — role granted",
                extra={
                    "user": user_label(guild, member.id),
                    "channel": channel_label(guild, channel.id),
                },
            )
        except Exception:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if not self._last_wednesday_message_id:
            return
        if payload.message_id != self._last_wednesday_message_id:
            return
        if payload.user_id == getattr(self.bot.user, "id", None):
            return
        if str(payload.emoji) not in {"👍", "👍🏻", "👍🏽", "👍🏿", "👍🏾"}:
            return
        admin_ids = self._admin_ids()
        if not admin_ids or payload.user_id not in admin_ids:
            return
        if payload.message_id in self._handled_messages:
            return

        self._handled_messages.add(payload.message_id)
        channel = await self._resolve_channel(payload.channel_id)
        await self.run_leagues_job(trigger="reaction", status_channel=channel)

    # === Commands ===
    @commands.group(
        name="leagues",
        invoke_without_command=True,
        help="C1C Leagues admin commands.",
    )
    @tier("admin")
    @help_metadata(function_group="operational", section="utilities", access_tier="admin")
    @admin_only()
    async def leagues(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        await ctx.send("Usage: !leagues post")

    @leagues.command(name="post", help="Manually run the C1C Leagues posting job.")
    @tier("admin")
    @help_metadata(function_group="operational", section="utilities", access_tier="admin")
    @admin_only()
    async def leagues_post(self, ctx: commands.Context) -> None:
        await self.run_leagues_job(trigger="command", status_channel=ctx.channel)

    # === Reminder helpers ===
    async def send_monday_reminder(self) -> None:
        channel = await self._resolve_channel(self._parse_int_env("LEAGUES_REMINDER_THREAD_ID"))
        if channel is None:
            log.warning("Leagues reminder thread missing; Monday reminder skipped")
            return
        mentions = self._admin_mentions_text()
        lines = [
            "📝 C1C Leagues – Sheet Update Reminder",
            "It’s Monday – time to update the C1C_Leagues sheet so this week’s boards are ready.",
        ]
        if mentions:
            lines.append(mentions)
        await channel.send("\n".join(lines))

    async def send_wednesday_reminder(self) -> None:
        channel = await self._resolve_channel(self._parse_int_env("LEAGUES_REMINDER_THREAD_ID"))
        if channel is None:
            log.warning("Leagues reminder thread missing; Wednesday reminder skipped")
            return
        mentions = self._admin_mentions_text()
        lines = [
            "🌩 C1C Leagues – Post This Week’s Boards?",
            "If the C1C_Leagues sheet is fully updated, react with 👍 on this message to publish all three leagues for this week.",
        ]
        if mentions:
            lines.append(mentions)
        message = await channel.send("\n".join(lines))
        try:
            await message.add_reaction("👍")
        except Exception:
            pass
        self._last_wednesday_message_id = message.id
        self._handled_messages.discard(message.id)

    # === Core job ===
    async def run_leagues_job(
        self,
        *,
        trigger: str,
        status_channel: discord.abc.Messageable | None,
    ) -> None:
        async with self._job_lock:
            await self._run_leagues_job(trigger=trigger, status_channel=status_channel)

    async def _run_leagues_job(
        self,
        *,
        trigger: str,
        status_channel: discord.abc.Messageable | None,
    ) -> None:
        sheet_id = os.getenv("LEAGUES_SHEET_ID", "").strip()
        if not sheet_id:
            await self._post_status(
                status_channel,
                f"❌ C1C Leagues job failed\nTrigger: {trigger}\nReason: LEAGUES_SHEET_ID is missing.",
                trigger=trigger,
            )
            return

        channel_ids = {
            "legendary": self._parse_int_env("LEAGUES_LEGENDARY_THREAD_ID"),
            "rising": self._parse_int_env("LEAGUES_RISING_THREAD_ID"),
            "storm": self._parse_int_env("LEAGUES_STORMFORGED_THREAD_ID"),
        }
        announcement_id = self._parse_int_env("ANNOUNCEMENT_CHANNEL_ID")

        targets: dict[str, discord.abc.Messageable] = {}
        missing_targets: list[str] = []

        for slug, channel_id in channel_ids.items():
            channel = await self._resolve_channel(channel_id)
            if channel is None:
                missing_targets.append(slug)
            else:
                targets[slug] = channel

        announcement_channel = await self._resolve_channel(announcement_id)
        if announcement_channel is None:
            missing_targets.append("announcement")

        if missing_targets:
            reason = f"missing targets: {', '.join(sorted(missing_targets))}"
            await self._post_status(
                status_channel,
                f"❌ C1C Leagues job failed\nTrigger: {trigger}\nReason: {reason}.",
                trigger=trigger,
            )
            return

        try:
            bundles = load_league_bundles(sheet_id, config_tab="Config")
        except LeaguesConfigError as exc:
            await self._post_status(
                status_channel,
                f"❌ C1C Leagues job failed\nTrigger: {trigger}\nReason: {exc}.",
                trigger=trigger,
            )
            return
        except Exception as exc:
            log.exception("leagues config load failed")
            await self._post_status(
                status_channel,
                f"❌ C1C Leagues job failed\nTrigger: {trigger}\nReason: config load error: {exc}.",
                trigger=trigger,
            )
            return

        validation_error = self._validate_bundles(bundles)
        if validation_error:
            await self._post_status(
                status_channel,
                f"❌ C1C Leagues job failed\nTrigger: {trigger}\nReason: {validation_error}.",
                trigger=trigger,
            )
            return

        loop = asyncio.get_running_loop()
        posted_messages: list[discord.Message] = []
        jump_links: dict[str, str] = {}
        now = dt.datetime.now(dt.timezone.utc)

        for bundle in bundles:
            channel = targets[bundle.slug]

            header_file = await self._export_header_image(loop, sheet_id, bundle)
            if isinstance(header_file, str):
                await self._post_status(
                    status_channel,
                    f"❌ C1C Leagues job failed\nTrigger: {trigger}\nReason: {header_file}.",
                    trigger=trigger,
                )
                return

            title = self._league_title(bundle, now)
            try:
                header_msg = await channel.send(content=title, file=header_file)
            except Exception as exc:
                log.exception("failed to send league header", extra={"league": bundle.slug})
                await self._cleanup_posts(posted_messages)
                await self._post_status(
                    status_channel,
                    f"❌ C1C Leagues job failed\nTrigger: {trigger}\nReason: sending {bundle.display_name} header failed ({exc}).",
                    trigger=trigger,
                )
                return

            posted_messages.append(header_msg)
            jump_links[bundle.slug] = header_msg.jump_url

            board_files = await self._export_board_images(loop, sheet_id, bundle)
            if isinstance(board_files, str):
                await self._cleanup_posts(posted_messages)
                await self._post_status(
                    status_channel,
                    f"❌ C1C Leagues job failed\nTrigger: {trigger}\nReason: {board_files}.",
                    trigger=trigger,
                )
                return

            for board_file in board_files:
                try:
                    message = await channel.send(file=board_file)
                except Exception as exc:
                    log.exception("failed to send league board", extra={"league": bundle.slug})
                    await self._cleanup_posts(posted_messages)
                    await self._post_status(
                        status_channel,
                        f"❌ C1C Leagues job failed\nTrigger: {trigger}\nReason: sending {bundle.display_name} board failed ({exc}).",
                        trigger=trigger,
                    )
                    return
                posted_messages.append(message)

        announcement_text = self._build_announcement(bundles, jump_links)
        announcement_embed = discord.Embed(description=announcement_text)
        announcement_embed.set_footer(
            text=(
                "Want to keep up to date with our C1C League Leaderboards? Click the 🏆 emoji to subscribe. "
                "To unsubscribe, remove your reaction."
            )
        )

        reaction_roles_attached: int | None = None
        try:
            announcement_message = await announcement_channel.send(embed=announcement_embed)
        except Exception as exc:
            log.exception("leagues announcement failed")
            await self._post_status(
                status_channel,
                "⚠️ C1C Leagues boards posted, but announcement failed – check ANNOUNCEMENT_CHANNEL_ID and permissions.",
                trigger=trigger,
            )
            return
        else:
            try:
                rr: ReactionRolesCog | None = self.bot.get_cog("ReactionRolesCog")  # type: ignore[name-defined]
                if rr is not None:
                    reaction_roles_attached = await rr.attach_to_message(
                        announcement_message, key="leagues"
                    )
            except Exception:
                reaction_roles_attached = None
                log.exception("leagues reaction-roles wiring failed")

        log.info(
            "📣 leagues: announcement posted",
            extra={
                "images": len(posted_messages),
                "announcement_id": getattr(announcement_message, "id", None),
                "reaction_roles": {"key": "leagues", "attached": reaction_roles_attached},
            },
        )

        await self._post_status(
            status_channel,
            "\n".join(
                [
                    "🧹 C1C Leagues job finished",
                    f"Trigger: {trigger}",
                    f"Leagues updated: {len(bundles)} / {len(bundles)}",
                    "Result: all posted successfully",
                    f"Timestamp: {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
                ]
            ),
            trigger=trigger,
        )

    @staticmethod
    async def _cleanup_posts(messages: list[discord.Message]) -> None:
        for message in messages:
            try:
                await message.delete()
            except Exception:
                continue

    async def _post_status(
        self, channel: discord.abc.Messageable | None, content: str, *, trigger: str
    ) -> None:
        if channel is None:
            log.warning("leagues status channel missing", extra={"trigger": trigger})
            return
        try:
            await channel.send(content)
        except Exception:
            log.exception("failed to send leagues status message")

    def _validate_bundles(self, bundles: Iterable[LeagueBundle]) -> str | None:
        for bundle in bundles:
            if bundle.header is None:
                return f"{bundle.display_name}: header missing in Leagues Config tab"
            if not bundle.boards:
                return f"{bundle.display_name}: no boards configured in Leagues Config tab"
        return None

    async def _export_header_image(
        self,
        loop: asyncio.AbstractEventLoop,
        sheet_id: str,
        bundle: LeagueBundle,
    ) -> discord.File | str:
        if bundle.header is None:
            return f"{bundle.display_name}: header missing in Leagues Config tab"

        return await self._export_spec(
            loop,
            sheet_id,
            bundle.slug,
            bundle.header,
            filename=f"{bundle.slug}_header.png",
        )

    async def _export_board_images(
        self,
        loop: asyncio.AbstractEventLoop,
        sheet_id: str,
        bundle: LeagueBundle,
    ) -> list[discord.File] | str:
        files: list[discord.File] = []
        for spec in bundle.boards:
            index = spec.index if spec.index is not None else len(files) + 1
            file = await self._export_spec(
                loop,
                sheet_id,
                bundle.slug,
                spec,
                filename=f"{bundle.slug}_{index}.png",
            )
            if isinstance(file, str):
                return file
            files.append(file)
        return files

    async def _export_spec(
        self,
        loop: asyncio.AbstractEventLoop,
        sheet_id: str,
        slug: str,
        spec: LeagueSpec,
        *,
        filename: str,
    ) -> discord.File | str:
        try:
            gid = await loop.run_in_executor(None, get_tab_gid, sheet_id, spec.sheet_name)
        except Exception:
            log.exception("gid lookup failed", extra={"key": spec.key, "tab": spec.sheet_name})
            return f"{slug.title()}: gid lookup failed for {spec.key}"

        if gid is None:
            return f"{slug.title()}: gid missing for {spec.sheet_name}"

        try:
            png_bytes = await export_pdf_as_png(
                sheet_id,
                gid,
                spec.cell_range,
                log_context={
                    "label": spec.key,
                    "tab": spec.sheet_name,
                    "range": spec.cell_range,
                },
            )
        except Exception:
            log.exception("export failed", extra={"key": spec.key, "tab": spec.sheet_name})
            return f"{slug.title()}: export failed for {spec.key}"

        if not png_bytes:
            return f"{slug.title()}: export returned no data for {spec.key}"

        return discord.File(fp=io.BytesIO(png_bytes), filename=filename)

    def _build_announcement(
        self, bundles: Iterable[LeagueBundle], jump_links: Mapping[str, str]
    ) -> str:
        jump_map = {bundle.slug: jump_links[bundle.slug] for bundle in bundles}
        role_id = self._parse_int_env("C1C_LEAGUE_ROLE_ID")
        mention = f"<@&{role_id}>" if role_id else "@C1CLeague"
        return "\n".join(
            [
                f"📊 Shifting Echoes from the {mention} …",
                "",
                "The climb never truly stops. Each week, new names rise, old banners hold the line, and some records quietly fall in the dust behind you.",
                "",
                "🦅 Legendary League  ",
                "The gates never close for long. New contenders keep pushing the limits, and the old guard keeps proving why they’re still on top.",
                "",
                "🌟 Rising Stars League  ",
                "Not every victory is shouted from rooftops. Some of you are carving your place into the stone one quiet, relentless step at a time.",
                "",
                "⚡ Stormforged League  ",
                "Where clans clash, storms crackle, and every key, banner and fight adds another spark to the scoreboard.",
                "",
                "Want to see what stirred the rankings this time?",
                "",
                f"🔹 Legendary League – [Jump to this week’s update]({jump_map['legendary']})  ",
                f"🔹 Rising Stars League – [Jump to this week’s update]({jump_map['rising']})  ",
                f"🔹 Stormforged League – [Jump to this week’s update]({jump_map['storm']})",
                "",
                mention,
            ]
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LeaguesCog(bot))
    log.info("C1C Leagues cog loaded")
