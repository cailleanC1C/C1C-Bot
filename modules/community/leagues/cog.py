from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
import os
from typing import TYPE_CHECKING, Any, Iterable, Mapping

import discord
from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only, is_admin_member
from modules.community.leagues.config import (
    LeagueBundle,
    LeagueSpec,
    LeaguesConfigError,
    aload_league_bundles,
)
from shared.logfmt import channel_label, user_label
from shared.sheets.async_core import acall_with_backoff, afetch_records, afetch_values, aget_worksheet
from shared.sheets.export_utils import export_pdf_as_png, get_tab_gid

if TYPE_CHECKING:
    from modules.community.reaction_roles import ReactionRolesCog

log = logging.getLogger("c1c.community.leagues")

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_APPROVAL_CONFIG_KEY = "league_approval_state_tab"
_APPROVAL_HEADERS = (
    "season_key",
    "week_key",
    "prompt_message_id",
    "prompt_channel_id",
    "status",
    "required_reactions",
    "approved_by_user_ids",
    "posted_at_utc",
    "created_at_utc",
    "updated_at_utc",
    "last_error",
)
_APPROVAL_ACTIVE_STATUSES = {"pending"}
_APPROVAL_DUPLICATE_PROMPT_STATUSES = {"pending", "posting", "approved", "posted"}
_APPROVAL_EMOJIS = {"👍", "👍🏻", "👍🏽", "👍🏿", "👍🏾"}


class LeaguesCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Approval prompt state is durable in the configured LeagueApprovalState tab.
        # These in-memory fields only de-dupe concurrent handling within this process.
        self._handled_messages: set[int] = set()
        self._job_lock = asyncio.Lock()
        self._approval_lock = asyncio.Lock()

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

    async def _is_valid_approval_admin(self, payload: discord.RawReactionActionEvent) -> bool:
        admin_ids = self._admin_ids()
        if payload.user_id in admin_ids:
            return True

        member = getattr(payload, "member", None)
        if member is None and payload.guild_id is not None:
            guild = self.bot.get_guild(payload.guild_id)
            if guild is not None:
                member = guild.get_member(payload.user_id)
                if member is None:
                    try:
                        member = await guild.fetch_member(payload.user_id)
                    except Exception:
                        member = None
        return bool(member is not None and is_admin_member(member))

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
        if str(payload.emoji) not in _APPROVAL_EMOJIS:
            log.debug("league approval reaction ignored", extra={"reason": "wrong_emoji", "message_id": payload.message_id})
            return
        log.info(
            "league approval reaction candidate seen",
            extra={
                "guild_id": payload.guild_id,
                "channel_id": payload.channel_id,
                "message_id": payload.message_id,
                "user_id": payload.user_id,
            },
        )
        if payload.user_id == getattr(self.bot.user, "id", None):
            log.debug("league approval reaction ignored", extra={"reason": "bot_user", "message_id": payload.message_id})
            return
        if not await self._is_valid_approval_admin(payload):
            log.info(
                "league approval reaction ignored",
                extra={"reason": "user_not_allowed", "message_id": payload.message_id, "user_id": payload.user_id},
            )
            return

        async with self._approval_lock:
            row = await self._find_approval_row(payload.channel_id, payload.message_id, include_terminal=True)
            if row is None:
                log.info(
                    "league approval reaction ignored",
                    extra={"reason": "no_active_approval_row", "channel_id": payload.channel_id, "message_id": payload.message_id},
                )
                return
            status = row["values"].get("status", "").lower()
            if status != "pending":
                log.info(
                    "league approval reaction ignored",
                    extra={"reason": "approval_not_pending", "status": status, "message_id": payload.message_id},
                )
                return

            approvers = self._parse_approvers(row["values"].get("approved_by_user_ids", ""))
            if payload.user_id in approvers:
                log.info("league approval reaction ignored", extra={"reason": "user_already_approved", "message_id": payload.message_id})
                return
            approvers.add(payload.user_id)
            await self._update_approval_row(
                row,
                {
                    "approved_by_user_ids": ",".join(str(user_id) for user_id in sorted(approvers)),
                    "updated_at_utc": self._utc_iso(),
                    "last_error": "",
                },
            )
            required = self._parse_positive_int(row["values"].get("required_reactions"), default=1)
            if len(approvers) < required:
                log.info(
                    "league approval threshold not reached yet",
                    extra={"message_id": payload.message_id, "approvals": len(approvers), "required_reactions": required},
                )
                return
            if payload.message_id in self._handled_messages:
                log.info("league approval reaction ignored", extra={"reason": "approval_already_posted", "message_id": payload.message_id})
                return
            self._handled_messages.add(payload.message_id)
            await self._update_approval_row(row, {"status": "posting", "updated_at_utc": self._utc_iso(), "last_error": ""})

        channel = await self._resolve_channel(payload.channel_id)
        log.info(
            "league approval threshold reached; posting starts",
            extra={
                "source": "reaction_approval",
                "season_key": row["values"].get("season_key"),
                "week_key": row["values"].get("week_key"),
                "prompt_message_id": payload.message_id,
                "prompt_channel_id": payload.channel_id,
            },
        )
        try:
            log.info("league board publish started", extra={"trigger": "reaction_approval", "message_id": payload.message_id})
            ok = await self.run_leagues_job(trigger="reaction_approval", status_channel=channel)
        except Exception as exc:
            ok = False
            error = f"{type(exc).__name__}: {exc}"
            log.exception("league board publish failed", extra={"reason": error})
        else:
            error = "" if ok else "posting job returned failure"

        posted_at = ""
        async with self._approval_lock:
            fresh = await self._find_approval_row(payload.channel_id, payload.message_id, include_terminal=True)
            if fresh is not None:
                posted_at = self._utc_iso() if ok else ""
                await self._update_approval_row(
                    fresh,
                    {
                        "status": "posted" if ok else "failed",
                        "posted_at_utc": posted_at,
                        "updated_at_utc": self._utc_iso(),
                        "last_error": error[:500],
                    },
                )
        log.info(
            "league board publish succeeded" if ok else "league board publish failed",
            extra={"success": ok, "posted_at_utc": posted_at if ok else "", "last_error": error},
        )

    @staticmethod
    def _column_label(index: int) -> str:
        value = index + 1
        label = ""
        while value > 0:
            value, remainder = divmod(value - 1, 26)
            label = chr(65 + remainder) + label
        return label or "A"

    @staticmethod
    def _utc_iso() -> str:
        return dt.datetime.now(dt.timezone.utc).isoformat()

    @staticmethod
    def _parse_approvers(raw: object) -> set[int]:
        approvers: set[int] = set()
        for part in str(raw or "").replace(";", ",").split(","):
            token = part.strip()
            if token.isdigit():
                approvers.add(int(token))
        return approvers

    @staticmethod
    def _parse_positive_int(raw: object, *, default: int) -> int:
        try:
            value = int(str(raw or "").strip())
        except (TypeError, ValueError):
            return default
        return value if value > 0 else default

    @staticmethod
    def _approval_keys(now: dt.datetime | None = None) -> tuple[str, str]:
        current = now or dt.datetime.now(dt.timezone.utc)
        iso = current.isocalendar()
        return str(iso.year), f"{iso.week:02d}"

    def _config_tab_name(self) -> str:
        return os.getenv("LEAGUES_CONFIG_TAB", "Config").strip() or "Config"

    async def _approval_state_tab(self, sheet_id: str) -> str | None:
        try:
            rows = await afetch_records(sheet_id, self._config_tab_name())
        except Exception:
            log.exception("league approval config load failed", extra={"config_key": _APPROVAL_CONFIG_KEY})
            return None
        for row in rows or []:
            key = ""
            value = ""
            for column, cell in row.items():
                normalized = str(column or "").strip().lower()
                if normalized in {"spec_key", "key", "name"}:
                    key = str(cell or "").strip()
                if normalized in {"sheet_name", "sheet", "tab", "value", "val"}:
                    value = str(cell or "").strip()
            if key.strip().lower() == _APPROVAL_CONFIG_KEY and value:
                log.info("league approval state tab config found", extra={"config_key": _APPROVAL_CONFIG_KEY, "sheet": "LEAGUES_SHEET_ID"})
                return value
        log.error("league approval state tab config missing", extra={"config_key": _APPROVAL_CONFIG_KEY, "sheet": "LEAGUES_SHEET_ID", "config_tab": self._config_tab_name()})
        return None

    async def _approval_sheet(self) -> tuple[str, Any, dict[str, int], list[list[Any]]] | None:
        sheet_id = os.getenv("LEAGUES_SHEET_ID", "").strip()
        if not sheet_id:
            log.error("league approval unavailable; LEAGUES_SHEET_ID missing")
            return None
        tab_name = await self._approval_state_tab(sheet_id)
        if not tab_name:
            return None
        try:
            matrix = await afetch_values(sheet_id, tab_name)
        except Exception:
            log.exception("league approval state load failed", extra={"tab": tab_name})
            return None
        if not matrix:
            log.error("league approval state header missing", extra={"tab": tab_name})
            return None
        header = [str(cell or "").strip() for cell in matrix[0]]
        header_map = {name: idx for idx, name in enumerate(header) if name}
        missing = [name for name in _APPROVAL_HEADERS if name not in header_map]
        if missing:
            log.error("league approval state missing required headers", extra={"tab": tab_name, "missing": missing})
            return None
        try:
            worksheet = await aget_worksheet(sheet_id, tab_name)
        except Exception:
            log.exception("league approval worksheet fetch failed", extra={"tab": tab_name})
            return None
        return tab_name, worksheet, header_map, matrix

    async def _find_approval_row(self, channel_id: int, message_id: int, *, include_terminal: bool = False) -> dict[str, Any] | None:
        loaded = await self._approval_sheet()
        if loaded is None:
            return None
        tab_name, worksheet, header_map, matrix = loaded
        for row_number, row in enumerate(matrix[1:], start=2):
            values = {name: (str(row[idx]).strip() if idx < len(row) else "") for name, idx in header_map.items()}
            if values.get("prompt_channel_id") != str(channel_id) or values.get("prompt_message_id") != str(message_id):
                continue
            status = values.get("status", "").lower()
            if include_terminal or status in _APPROVAL_ACTIVE_STATUSES:
                log.info("league approval state row matched", extra={"channel_id": channel_id, "message_id": message_id, "status": status, "row_number": row_number})
                return {"tab": tab_name, "worksheet": worksheet, "header_map": header_map, "row_number": row_number, "values": values}
        log.info("league approval state row not matched", extra={"channel_id": channel_id, "message_id": message_id})
        return None


    @staticmethod
    def _approval_row_log_extra(row: dict[str, Any], *, reason: str) -> dict[str, object]:
        values = row.get("values", {})
        return {
            "reason": reason,
            "season_key": values.get("season_key", ""),
            "week_key": values.get("week_key", ""),
            "status": values.get("status", ""),
            "prompt_message_id": values.get("prompt_message_id", ""),
            "prompt_channel_id": values.get("prompt_channel_id", ""),
            "last_error": values.get("last_error", ""),
        }

    async def _approval_prompt_message_exists(self, row: dict[str, Any]) -> bool | None:
        values = row.get("values", {})
        try:
            channel_id = int(str(values.get("prompt_channel_id", "")).strip())
            message_id = int(str(values.get("prompt_message_id", "")).strip())
        except (TypeError, ValueError):
            return None
        channel = await self._resolve_channel(channel_id)
        if channel is None or not hasattr(channel, "fetch_message"):
            return None
        try:
            await channel.fetch_message(message_id)  # type: ignore[attr-defined]
        except discord.NotFound:
            return False
        except Exception:
            return None
        return True

    async def _find_approval_row_for_week(self, season_key: str, week_key: str) -> dict[str, Any] | None:
        loaded = await self._approval_sheet()
        if loaded is None:
            return None
        tab_name, worksheet, header_map, matrix = loaded
        for row_number, row in enumerate(matrix[1:], start=2):
            values = {name: (str(row[idx]).strip() if idx < len(row) else "") for name, idx in header_map.items()}
            if values.get("season_key") == season_key and values.get("week_key") == week_key:
                return {"tab": tab_name, "worksheet": worksheet, "header_map": header_map, "row_number": row_number, "values": values}
        return None

    async def _update_approval_row(self, row: dict[str, Any], updates: Mapping[str, object]) -> None:
        worksheet = row["worksheet"]
        header_map: dict[str, int] = row["header_map"]
        row_number = int(row["row_number"])
        for key, value in updates.items():
            if key not in header_map:
                continue
            column = self._column_label(header_map[key])
            await acall_with_backoff(worksheet.update, f"{column}{row_number}", [[str(value)]], value_input_option="RAW")

    async def _create_approval_prompt_state(
        self,
        message: discord.Message,
        loaded: tuple[str, Any, dict[str, int], list[list[Any]]] | None = None,
    ) -> None:
        loaded = loaded or await self._approval_sheet()
        if loaded is None:
            raise RuntimeError("league approval state sheet unavailable")
        _tab_name, worksheet, header_map, _matrix = loaded
        season_key, week_key = self._approval_keys()
        now = self._utc_iso()
        created_at = now
        values = {
            "season_key": season_key,
            "week_key": week_key,
            "prompt_message_id": str(message.id),
            "prompt_channel_id": str(getattr(message.channel, "id", "")),
            "status": "pending",
            "required_reactions": "1",
            "approved_by_user_ids": "",
            "posted_at_utc": "",
            "created_at_utc": created_at,
            "updated_at_utc": now,
            "last_error": "",
        }
        ordered = [values[name] for name in _APPROVAL_HEADERS]
        await acall_with_backoff(worksheet.append_row, ordered, value_input_option="RAW")
        log.info("league approval state row created", extra={"message_id": message.id, "channel_id": getattr(message.channel, "id", None), "season_key": season_key, "week_key": week_key})

    # === Commands ===
    @tier("admin")
    @help_metadata(function_group="operational", section="utilities", access_tier="admin")
    @commands.group(
        name="leagues",
        invoke_without_command=True,
        help="C1C Leagues admin commands.",
    )
    @admin_only()
    async def leagues(self, ctx: commands.Context) -> None:
        if ctx.invoked_subcommand is not None:
            return
        await ctx.send("Usage: !leagues post")

    @tier("admin")
    @help_metadata(function_group="operational", section="utilities", access_tier="admin")
    @leagues.command(name="post", help="Manually run the C1C Leagues posting job.")
    @admin_only()
    async def leagues_post(self, ctx: commands.Context) -> None:
        await self.run_leagues_job(trigger="command", status_channel=ctx.channel)

    # === Reminder helpers ===
    async def send_monday_reminder(self) -> None:
        log.info("league reminder fired", extra={"weekday": "monday"})
        channel = await self._resolve_channel(self._parse_int_env("LEAGUES_REMINDER_THREAD_ID"))
        if channel is None:
            log.warning("league reminder skipped", extra={"weekday": "monday", "reason": "reminder_thread_missing"})
            return
        mentions = self._admin_mentions_text()
        lines = [
            "📝 C1C Leagues – Sheet Update Reminder",
            "It’s Monday – time to update the C1C_Leagues sheet so this week’s boards are ready.",
        ]
        if mentions:
            lines.append(mentions)
        await channel.send("\n".join(lines))
        log.info("league reminder sent", extra={"weekday": "monday", "channel_id": getattr(channel, "id", None)})

    async def send_wednesday_reminder(self) -> None:
        log.info("league approval prompt fired", extra={"weekday": "wednesday"})
        season_key, week_key = self._approval_keys()
        loaded = await self._approval_sheet()
        if loaded is None:
            log.warning("league approval prompt skipped", extra={"reason": "approval_state_unavailable", "season_key": season_key, "week_key": week_key})
            return
        tab_name, worksheet, header_map, matrix = loaded
        existing = None
        for row_number, row in enumerate(matrix[1:], start=2):
            values = {name: (str(row[idx]).strip() if idx < len(row) else "") for name, idx in header_map.items()}
            if values.get("season_key") == season_key and values.get("week_key") == week_key:
                existing = {"tab": tab_name, "worksheet": worksheet, "header_map": header_map, "row_number": row_number, "values": values}
                break
        channel = await self._resolve_channel(self._parse_int_env("LEAGUES_REMINDER_THREAD_ID"))
        if channel is None:
            log.warning("league approval prompt skipped", extra={"reason": "reminder_thread_missing"})
            return
        if existing is not None:
            status = existing["values"].get("status", "").strip().lower()
            if status == "failed" and not existing["values"].get("posted_at_utc", "").strip():
                log.warning(
                    "league approval prompt recovery allowed after failed row",
                    extra=self._approval_row_log_extra(existing, reason="failed_without_posted_at"),
                )
            elif status == "pending" and await self._approval_prompt_message_exists(existing) is False:
                log.warning(
                    "league approval prompt recovery allowed for stale approval row",
                    extra=self._approval_row_log_extra(existing, reason="prompt_message_deleted"),
                )
            elif status in _APPROVAL_DUPLICATE_PROMPT_STATUSES:
                log.info(
                    "league approval prompt skipped",
                    extra=self._approval_row_log_extra(existing, reason="approval_row_exists"),
                )
                return
            else:
                log.warning(
                    "league approval prompt skipped",
                    extra=self._approval_row_log_extra(existing, reason="manual_cleanup_required"),
                )
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
        self._handled_messages.discard(message.id)
        await self._create_approval_prompt_state(message, loaded)
        log.info("league approval prompt sent", extra={"message_id": message.id, "channel_id": getattr(channel, "id", None), "season_key": season_key, "week_key": week_key})

    # === Core job ===
    async def run_leagues_job(
        self,
        *,
        trigger: str,
        status_channel: discord.abc.Messageable | None,
    ) -> bool:
        async with self._job_lock:
            return await self._run_leagues_job(trigger=trigger, status_channel=status_channel)

    async def _run_leagues_job(
        self,
        *,
        trigger: str,
        status_channel: discord.abc.Messageable | None,
    ) -> bool:
        sheet_id = os.getenv("LEAGUES_SHEET_ID", "").strip()
        if not sheet_id:
            await self._post_status(
                status_channel,
                f"❌ C1C Leagues job failed\nTrigger: {trigger}\nReason: LEAGUES_SHEET_ID is missing.",
                trigger=trigger,
            )
            return False

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
            return False

        try:
            bundles = await aload_league_bundles(sheet_id, config_tab=self._config_tab_name())
        except LeaguesConfigError as exc:
            await self._post_status(
                status_channel,
                f"❌ C1C Leagues job failed\nTrigger: {trigger}\nReason: {exc}.",
                trigger=trigger,
            )
            return False
        except Exception as exc:
            log.exception("leagues config load failed")
            await self._post_status(
                status_channel,
                f"❌ C1C Leagues job failed\nTrigger: {trigger}\nReason: config load error: {exc}.",
                trigger=trigger,
            )
            return False

        validation_error = self._validate_bundles(bundles)
        if validation_error:
            await self._post_status(
                status_channel,
                f"❌ C1C Leagues job failed\nTrigger: {trigger}\nReason: {validation_error}.",
                trigger=trigger,
            )
            return False

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
                return False

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
                return False

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
                return False

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
                    return False
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
            announcement_message = await announcement_channel.send(
                content=self._league_role_mention(),
                embed=announcement_embed,
            )
        except Exception:
            log.exception("leagues announcement failed")
            await self._post_status(
                status_channel,
                "⚠️ C1C Leagues boards posted, but announcement failed – check ANNOUNCEMENT_CHANNEL_ID and permissions.",
                trigger=trigger,
            )
            return False
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
        return True

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

    def _league_role_mention(self) -> str:
        role_id = self._parse_int_env("C1C_LEAGUE_ROLE_ID")
        return f"<@&{role_id}>" if role_id else "@C1CLeague"

    def _build_announcement(
        self, bundles: Iterable[LeagueBundle], jump_links: Mapping[str, str]
    ) -> str:
        jump_map = {bundle.slug: jump_links[bundle.slug] for bundle in bundles}
        return "\n".join(
            [
                "# Shifting Echoes from the C1CLeague …",
                "",
                "The climb never truly stops. Each week, new names rise, old banners hold the line, and some records quietly fall in the dust behind you.",
                "",
                "🦅 **Legendary League**  ",
                "The gates never close for long. New contenders keep pushing the limits, and the old guard keeps proving why they’re still on top.",
                "",
                "🌟 **Rising Stars League**  ",
                "Not every victory is shouted from rooftops. Some of you are carving your place into the stone one quiet, relentless step at a time.",
                "",
                "⚡ **Stormforged League**  ",
                "Where clans clash, storms crackle, and every key, banner and fight adds another spark to the scoreboard.",
                "",
                "Want to see what stirred the rankings this time?",
                "",
                f"🔹 **Legendary League** – [Jump to this week’s update]({jump_map['legendary']})  ",
                f"🔹 **Rising Stars League** – [Jump to this week’s update]({jump_map['rising']})  ",
                f"🔹 **Stormforged League** – [Jump to this week’s update]({jump_map['storm']})",
                "",
            ]
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(LeaguesCog(bot))
    log.info("C1C Leagues cog loaded")
