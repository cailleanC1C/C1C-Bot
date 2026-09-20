from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
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
from modules.community.leagues.history import HistoryCaptureError, capture_weekly_history
from shared.config import cfg
from shared.logfmt import channel_label, user_label
from shared.sheets.async_core import acall_with_backoff, afetch_records, afetch_values, aget_worksheet
from shared.sheets.export_utils import ImageExportError, export_pdf_as_png, get_tab_gid

if TYPE_CHECKING:
    from modules.community.reaction_roles import ReactionRolesCog

log = logging.getLogger("c1c.community.leagues")

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_APPROVAL_CONFIG_KEY = "league_approval_state_tab"
_PUBLISH_CONFIG_KEY = "league_publish_state_tab"
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


class LeagueRetryView(discord.ui.View):
    """Persistent recovery control for a failed weekly league publication."""

    def __init__(self, cog: "LeaguesCog") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Retry Failed Step",
        emoji="🔄",
        style=discord.ButtonStyle.primary,
        custom_id="c1c:leagues:retry_failed_step",
    )
    async def retry_failed_step(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await self.cog._handle_retry_interaction(interaction)


class LeaguesCog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # Approval prompt state is durable in the configured LeagueApprovalState tab.
        # These in-memory fields only de-dupe concurrent handling within this process.
        self._handled_messages: set[int] = set()
        self._job_lock = asyncio.Lock()
        self._approval_lock = asyncio.Lock()

        sheet_id = str(cfg.get("LEAGUES_SHEET_ID", "") or "").strip()
        if not sheet_id:
            log.warning("Leagues sheet ID missing at startup; feature will remain idle")

    async def cog_load(self) -> None:
        # Persistent custom_id keeps recovery usable after a bot restart.
        self.bot.add_view(LeagueRetryView(self))
        self._stale_recovery_task = asyncio.create_task(self._recover_stale_jobs_after_ready())

    def cog_unload(self) -> None:
        task = getattr(self, "_stale_recovery_task", None)
        if task is not None:
            task.cancel()

    async def _recover_stale_jobs_after_ready(self) -> None:
        try:
            await self.bot.wait_until_ready()
            loaded = await self._approval_sheet()
            if loaded is None:
                return
            tab_name, worksheet, header_map, matrix = loaded
            for row_number, raw in enumerate(matrix[1:], start=2):
                values = {
                    name: (str(raw[idx]).strip() if idx < len(raw) else "")
                    for name, idx in header_map.items()
                }
                if values.get("status", "").lower() != "posting":
                    continue
                row = {
                    "tab": tab_name,
                    "worksheet": worksheet,
                    "header_map": header_map,
                    "row_number": row_number,
                    "values": values,
                }
                reason = "Previous league job was interrupted by a bot restart; use Retry Failed Step to resume safely."
                await self._set_job_fields(
                    row,
                    {"status": "failed", "last_error": reason, "updated_at_utc": self._utc_iso()},
                )
                try:
                    channel_id = int(values.get("prompt_channel_id", ""))
                    channel = await self._resolve_channel(channel_id)
                    durable_week = self._format_week_key(values.get("season_key"), values.get("week_key"))
                except (TypeError, ValueError, HistoryCaptureError):
                    continue
                await self._progress_message(channel, row, durable_week, state="failed", detail=f"**Error:** {reason}")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("failed to recover stale league jobs")

    # === Helpers ===
    @staticmethod
    def _parse_int_config(key: str) -> int | None:
        raw = cfg.get(key)
        try:
            return int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _admin_ids() -> list[int]:
        raw = cfg.get("LEAGUE_ADMIN_IDS", "")
        admin_ids: list[int] = []
        parts = raw if isinstance(raw, (list, tuple, set, frozenset)) else str(raw).split(",")
        for part in parts:
            token = str(part).strip()
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
            log.info(
                "league approval admin gate passed",
                extra={"gate": "league_admin_ids", "message_id": payload.message_id},
            )
            return True

        member = getattr(payload, "member", None)
        member_source = "payload" if member is not None else "missing"
        if member is None and payload.guild_id is not None:
            guild = self.bot.get_guild(payload.guild_id)
            if guild is not None:
                member = guild.get_member(payload.user_id)
                member_source = "guild_cache" if member is not None else "guild_fetch"
                if member is None:
                    try:
                        member = await guild.fetch_member(payload.user_id)
                    except Exception:
                        log.info(
                            "league approval admin gate failed",
                            extra={
                                "reason": "member_fetch_failed",
                                "configured_user_ids": bool(admin_ids),
                                "message_id": payload.message_id,
                            },
                        )
                        member = None
        if member is not None and is_admin_member(member):
            log.info(
                "league approval admin gate passed",
                extra={
                    "gate": "discord_admin",
                    "member_source": member_source,
                    "message_id": payload.message_id,
                },
            )
            return True
        log.info(
            "league approval admin gate failed",
            extra={
                "reason": "not_in_league_admin_ids_or_discord_admin",
                "configured_user_ids": bool(admin_ids),
                "member_available": member is not None,
                "member_source": member_source,
                "message_id": payload.message_id,
            },
        )
        return False

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
        if bundle.slug == "storm":
            previous_week = now.date() - dt.timedelta(days=7)
            calendar_week = previous_week.isocalendar().week
            return f"{bundle.display_name} – Calendar Week {calendar_week} Results"
        today = now.date().isoformat()
        return f"{bundle.display_name} – Weekly Update {today}"

    # === Event listeners ===
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        channel = getattr(message, "channel", None)
        if not channel or getattr(channel, "id", None) != self._parse_int_config(
            "LEAGUES_SUBMISSION_CHANNEL_ID"
        ):
            return
        if not any(self._is_image_attachment(att) for att in message.attachments):
            return
        guild = getattr(message, "guild", None)
        if not isinstance(guild, discord.Guild):
            return

        role_id = self._parse_int_config("C1C_LEAGUE_ROLE_ID")
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
            status = row["values"].get("status", "").strip().lower()
            posted_at_utc = row["values"].get("posted_at_utc", "").strip()
            retrying_failed = status == "failed" and not posted_at_utc
            if status == "posted" or posted_at_utc:
                log.info(
                    "approval_not_retryable",
                    extra={
                        "reason": "approval_not_retryable",
                        "status": status,
                        "posted_at_utc": posted_at_utc,
                        "message_id": payload.message_id,
                    },
                )
                return
            if status not in {"pending", "failed"}:
                log.info(
                    "approval_not_retryable",
                    extra={
                        "reason": "approval_not_retryable",
                        "status": status,
                        "posted_at_utc": posted_at_utc,
                        "message_id": payload.message_id,
                    },
                )
                return
            if retrying_failed:
                self._handled_messages.discard(payload.message_id)
                log.info(
                    "approval_retry_from_failed",
                    extra={
                        "reason": "approval_retry_from_failed",
                        "status": status,
                        "message_id": payload.message_id,
                        "season_key": row["values"].get("season_key"),
                        "week_key": row["values"].get("week_key"),
                    },
                )

            approvers = self._parse_approvers(row["values"].get("approved_by_user_ids", ""))
            if payload.user_id in approvers and not retrying_failed:
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
            durable_week = self._format_week_key(
                row["values"].get("season_key", ""), row["values"].get("week_key", "")
            )
            ok = await self.run_leagues_job(
                trigger="reaction_approval",
                status_channel=channel,
                week_key=durable_week,
            )
        except Exception as exc:
            ok = False
            error = f"{type(exc).__name__}: {exc}"
            log.exception("league board publish failed", extra={"reason": error})
        else:
            error = "" if ok else ""

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
                        "last_error": (
                            error[:500]
                            if error
                            else ("" if ok else fresh["values"].get("last_error", "posting job returned failure")[:500])
                        ),
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

    @staticmethod
    def _format_week_key(season_key: object, week_key: object) -> str:
        season = str(season_key or "").strip()
        week = str(week_key or "").strip().removeprefix("W").zfill(2)
        if not season or not week.isdigit():
            raise HistoryCaptureError("durable approval season/week is invalid")
        return f"{season}-W{week}"

    @classmethod
    def _current_week_key(cls, now: dt.datetime | None = None) -> str:
        season, week = cls._approval_keys(now)
        return cls._format_week_key(season, week)

    def _config_tab_name(self) -> str:
        return str(cfg.get("LEAGUES_CONFIG_TAB", "Config") or "Config").strip() or "Config"

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
        sheet_id = str(cfg.get("LEAGUES_SHEET_ID", "") or "").strip()
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
            "progress_message_id": "",
            "prepare_status": "pending",
            "legendary_status": "pending",
            "rising_status": "pending",
            "storm_status": "pending",
            "announcement_status": "pending",
        }
        ordered = [""] * len(header_map)
        for name, idx in header_map.items():
            if name in values:
                ordered[idx] = values[name]
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
        await self.run_leagues_job(
            trigger="command",
            status_channel=ctx.channel,
            week_key=self._current_week_key(),
        )

    # === Reminder helpers ===
    async def send_monday_reminder(self) -> None:
        log.info("league reminder fired", extra={"weekday": "monday"})
        channel = await self._resolve_channel(self._parse_int_config("LEAGUES_REMINDER_THREAD_ID"))
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
        channel = await self._resolve_channel(self._parse_int_config("LEAGUES_REMINDER_THREAD_ID"))
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
        week_key: str,
    ) -> bool:
        async with self._job_lock:
            return await self._run_leagues_job(
                trigger=trigger, status_channel=status_channel, week_key=week_key
            )

    async def _run_leagues_job(
        self,
        *,
        trigger: str,
        status_channel: discord.abc.Messageable | None,
        week_key: str,
    ) -> bool:
        sheet_id = str(cfg.get("LEAGUES_SHEET_ID", "") or "").strip()
        season_key, week_number = self._split_durable_week(week_key)
        approval_row = await self._find_approval_row_for_week(season_key, week_number)

        async def fail(reason: str) -> bool:
            log.error("league publish stopped", extra={"trigger": trigger, "reason": reason, "week_key": week_key})
            await self._set_job_fields(
                approval_row,
                {
                    "status": "failed",
                    "last_error": reason[:500],
                    "updated_at_utc": self._utc_iso(),
                },
            )
            await self._progress_message(
                status_channel, approval_row, week_key, state="failed", detail=f"**Error:** {reason}"
            )
            return False

        if not sheet_id:
            return await fail("LEAGUES_SHEET_ID is missing.")

        channel_ids = {
            "legendary": self._parse_int_config("LEAGUES_LEGENDARY_THREAD_ID"),
            "rising": self._parse_int_config("LEAGUES_RISING_THREAD_ID"),
            "storm": self._parse_int_config("LEAGUES_STORMFORGED_THREAD_ID"),
        }
        announcement_id = self._parse_int_config("ANNOUNCEMENT_CHANNEL_ID")
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
            return await fail(f"missing targets: {', '.join(sorted(missing_targets))}")

        try:
            bundles = await aload_league_bundles(sheet_id, config_tab=self._config_tab_name())
        except LeaguesConfigError as exc:
            return await fail(str(exc))
        except Exception as exc:
            log.exception("leagues config load failed")
            return await fail(f"config load error: {exc}")

        validation_error = self._validate_bundles(bundles)
        if validation_error:
            return await fail(validation_error)

        if await self._publish_state_sheet(sheet_id) is None:
            return await fail("LeaguePublishState is unavailable or misconfigured.")

        initial_updates: dict[str, object] = {
            "status": "posting",
            "updated_at_utc": self._utc_iso(),
        }
        if approval_row is not None:
            for key in (
                "prepare_status",
                "legendary_status",
                "rising_status",
                "storm_status",
                "announcement_status",
            ):
                if not approval_row["values"].get(key):
                    initial_updates[key] = "pending"
        await self._set_job_fields(approval_row, initial_updates)
        await self._progress_message(status_channel, approval_row, week_key, state="running")

        try:
            history_summary = await capture_weekly_history(
                sheet_id,
                config_tab=self._config_tab_name(),
                week_key=week_key,
                trigger=trigger,
            )
        except Exception as exc:
            log.exception("league history capture failed", extra={"week_key": week_key})
            return await fail(f"history capture failed: {exc}")

        # Phase 1: render every asset needed by this run before publishing anything.
        await self._set_job_fields(
            approval_row,
            {"prepare_status": "preparing", "updated_at_utc": self._utc_iso()},
        )
        await self._progress_message(status_channel, approval_row, week_key, state="running")

        loop = asyncio.get_running_loop()
        prepared: dict[str, tuple[discord.File, list[discord.File]]] = {}
        for bundle in bundles:
            current = approval_row["values"].get(f"{bundle.slug}_status", "pending") if approval_row else "pending"
            if current == "posted":
                continue
            header_file = await self._export_header_image(loop, sheet_id, bundle)
            if isinstance(header_file, str):
                await self._set_job_fields(approval_row, {"prepare_status": "failed"})
                return await fail(header_file)
            board_files = await self._export_board_images(loop, sheet_id, bundle)
            if isinstance(board_files, str):
                await self._set_job_fields(approval_row, {"prepare_status": "failed"})
                return await fail(board_files)
            prepared[bundle.slug] = (header_file, board_files)

        await self._set_job_fields(
            approval_row,
            {"prepare_status": "ready", "updated_at_utc": self._utc_iso()},
        )
        await self._progress_message(status_channel, approval_row, week_key, state="running")

        # Phase 2: reconcile partial components and publish only unfinished leagues.
        jump_links: dict[str, str] = {}
        now = dt.datetime.now(dt.timezone.utc)
        for bundle in bundles:
            channel = targets[bundle.slug]
            status_key = f"{bundle.slug}_status"
            current = approval_row["values"].get(status_key, "pending") if approval_row else "pending"
            if current == "posted":
                link = await self._component_header_link(sheet_id, week_key, bundle.slug, channel)
                if not link:
                    await self._set_job_fields(approval_row, {status_key: "failed"})
                    return await fail(f"{bundle.display_name} is marked posted but its recorded header message is missing.")
                jump_links[bundle.slug] = link
                continue

            if current in {"partial", "posting", "failed"}:
                try:
                    await self._cleanup_partial_component(sheet_id, week_key, bundle.slug, channel)
                except Exception as exc:
                    await self._set_job_fields(approval_row, {status_key: "failed"})
                    return await fail(f"cleanup of partial {bundle.display_name} post failed ({exc}).")

            await self._set_job_fields(approval_row, {status_key: "posting"})
            await self._progress_message(status_channel, approval_row, week_key, state="running")
            header_file, board_files = prepared[bundle.slug]
            try:
                header_msg = await channel.send(content=self._league_title(bundle, now), file=header_file)
                try:
                    await self._record_publish_message(sheet_id, week_key, bundle.slug, "header", header_msg)
                except Exception:
                    await header_msg.delete()
                    raise
                jump_links[bundle.slug] = header_msg.jump_url
                for board_file in board_files:
                    message = await channel.send(file=board_file)
                    try:
                        await self._record_publish_message(sheet_id, week_key, bundle.slug, "board", message)
                    except Exception:
                        await message.delete()
                        raise
            except Exception as exc:
                log.exception("league component publish failed", extra={"league": bundle.slug})
                await self._set_job_fields(approval_row, {status_key: "partial"})
                return await fail(f"sending {bundle.display_name} failed ({exc}).")

            await self._set_job_fields(approval_row, {status_key: "posted"})
            await self._progress_message(status_channel, approval_row, week_key, state="running")

        announcement_status = approval_row["values"].get("announcement_status", "pending") if approval_row else "pending"
        if announcement_status != "posted":
            if announcement_status in {"partial", "posting", "failed"}:
                try:
                    await self._cleanup_partial_component(sheet_id, week_key, "announcement", announcement_channel)
                except Exception as exc:
                    await self._set_job_fields(approval_row, {"announcement_status": "failed"})
                    return await fail(f"cleanup of partial announcement failed ({exc}).")

            await self._set_job_fields(approval_row, {"announcement_status": "posting"})
            await self._progress_message(status_channel, approval_row, week_key, state="running")
            announcement_text = self._build_announcement(bundles, jump_links)
            announcement_embed = discord.Embed(description=announcement_text)
            announcement_embed.set_footer(
                text=(
                    "Want to keep up to date with our C1C League Leaderboards? Click the 🏆 emoji to subscribe. "
                    "To unsubscribe, remove your reaction."
                )
            )
            try:
                announcement_message = await announcement_channel.send(
                    content=self._league_role_mention(), embed=announcement_embed
                )
                try:
                    await self._record_publish_message(
                        sheet_id, week_key, "announcement", "announcement", announcement_message
                    )
                except Exception:
                    await announcement_message.delete()
                    raise
                rr: ReactionRolesCog | None = self.bot.get_cog("ReactionRolesCog")  # type: ignore[name-defined]
                if rr is not None:
                    await rr.attach_to_message(announcement_message, key="leagues")
            except Exception as exc:
                log.exception("leagues announcement failed")
                await self._set_job_fields(approval_row, {"announcement_status": "partial"})
                return await fail(f"league announcement/reaction-role setup failed ({exc}).")
            await self._set_job_fields(approval_row, {"announcement_status": "posted"})

        await self._set_job_fields(
            approval_row,
            {
                "status": "posted",
                "posted_at_utc": self._utc_iso(),
                "last_error": "",
                "updated_at_utc": self._utc_iso(),
            },
        )
        await self._progress_message(
            status_channel,
            approval_row,
            week_key,
            state="complete",
            detail=history_summary.status_text(),
        )
        return True

    async def _configured_sheet_tab(self, sheet_id: str, config_key: str) -> str | None:
        try:
            rows = await afetch_records(sheet_id, self._config_tab_name())
        except Exception:
            log.exception("league config tab lookup failed", extra={"config_key": config_key})
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
            if key.lower() == config_key.lower() and value:
                return value
        log.error("league configured tab missing", extra={"config_key": config_key})
        return None

    async def _publish_state_sheet(
        self, sheet_id: str
    ) -> tuple[Any, dict[str, int], list[list[Any]]] | None:
        tab_name = await self._configured_sheet_tab(sheet_id, _PUBLISH_CONFIG_KEY)
        if not tab_name:
            return None
        try:
            matrix = await afetch_values(sheet_id, tab_name)
            worksheet = await aget_worksheet(sheet_id, tab_name)
        except Exception:
            log.exception("league publish state load failed", extra={"tab": tab_name})
            return None
        if not matrix:
            return None
        header = [str(cell or "").strip() for cell in matrix[0]]
        header_map = {name: idx for idx, name in enumerate(header) if name}
        required = {
            "season_key", "week_key", "component", "message_type",
            "message_id", "status", "created_at_utc", "updated_at_utc",
        }
        if not required.issubset(header_map):
            log.error(
                "league publish state missing required headers",
                extra={"missing": sorted(required - set(header_map))},
            )
            return None
        return worksheet, header_map, matrix

    @staticmethod
    def _split_durable_week(week_key: str) -> tuple[str, str]:
        season, week = week_key.split("-W", 1)
        return season, week.zfill(2)

    async def _publish_rows(
        self, sheet_id: str, week_key: str, component: str | None = None
    ) -> list[dict[str, Any]]:
        loaded = await self._publish_state_sheet(sheet_id)
        if loaded is None:
            return []
        worksheet, header_map, matrix = loaded
        season, week = self._split_durable_week(week_key)
        found: list[dict[str, Any]] = []
        for row_number, row in enumerate(matrix[1:], start=2):
            values = {
                name: (str(row[idx]).strip() if idx < len(row) else "")
                for name, idx in header_map.items()
            }
            if values.get("season_key") != season or values.get("week_key") != week:
                continue
            if component is not None and values.get("component") != component:
                continue
            found.append(
                {
                    "worksheet": worksheet,
                    "header_map": header_map,
                    "row_number": row_number,
                    "values": values,
                }
            )
        return found

    async def _record_publish_message(
        self,
        sheet_id: str,
        week_key: str,
        component: str,
        message_type: str,
        message: discord.Message,
    ) -> None:
        loaded = await self._publish_state_sheet(sheet_id)
        if loaded is None:
            raise RuntimeError("LeaguePublishState is unavailable")
        worksheet, header_map, _matrix = loaded
        season, week = self._split_durable_week(week_key)
        now = self._utc_iso()
        values = {
            "season_key": season,
            "week_key": week,
            "component": component,
            "message_type": message_type,
            "message_id": str(message.id),
            "status": "posted",
            "created_at_utc": now,
            "updated_at_utc": now,
        }
        ordered = [""] * len(header_map)
        for name, idx in header_map.items():
            if name in values:
                ordered[idx] = values[name]
        await acall_with_backoff(worksheet.append_row, ordered, value_input_option="RAW")

    async def _mark_publish_row(self, row: dict[str, Any], status: str) -> None:
        header_map = row["header_map"]
        if "status" not in header_map:
            return
        worksheet = row["worksheet"]
        row_number = int(row["row_number"])
        status_col = self._column_label(header_map["status"])
        updated_col = self._column_label(header_map["updated_at_utc"])
        await acall_with_backoff(
            worksheet.update,
            f"{status_col}{row_number}",
            [[status]],
            value_input_option="RAW",
        )
        await acall_with_backoff(
            worksheet.update,
            f"{updated_col}{row_number}",
            [[self._utc_iso()]],
            value_input_option="RAW",
        )

    async def _fetch_recorded_message(
        self, channel: discord.abc.Messageable, message_id: str
    ) -> discord.Message | None:
        if not hasattr(channel, "fetch_message"):
            return None
        try:
            return await channel.fetch_message(int(message_id))  # type: ignore[attr-defined]
        except discord.NotFound:
            return None
        except Exception:
            log.exception("failed to fetch recorded league message", extra={"message_id": message_id})
            return None

    async def _cleanup_partial_component(
        self,
        sheet_id: str,
        week_key: str,
        component: str,
        channel: discord.abc.Messageable,
    ) -> None:
        for row in await self._publish_rows(sheet_id, week_key, component):
            if row["values"].get("status") != "posted":
                continue
            message = await self._fetch_recorded_message(channel, row["values"].get("message_id", ""))
            if message is not None:
                try:
                    await message.delete()
                except Exception:
                    log.exception(
                        "failed to delete partial league message",
                        extra={"component": component, "message_id": row["values"].get("message_id")},
                    )
                    raise
            await self._mark_publish_row(row, "deleted")

    async def _component_header_link(
        self,
        sheet_id: str,
        week_key: str,
        component: str,
        channel: discord.abc.Messageable,
    ) -> str | None:
        for row in await self._publish_rows(sheet_id, week_key, component):
            values = row["values"]
            if values.get("status") != "posted" or values.get("message_type") != "header":
                continue
            message = await self._fetch_recorded_message(channel, values.get("message_id", ""))
            if message is not None:
                return message.jump_url
        return None

    async def _set_job_fields(
        self, row: dict[str, Any] | None, updates: Mapping[str, object]
    ) -> None:
        if row is None:
            return
        await self._update_approval_row(row, updates)
        row["values"].update({key: str(value) for key, value in updates.items()})

    def _progress_text(
        self,
        row: dict[str, Any] | None,
        week_key: str,
        *,
        state: str,
        detail: str = "",
    ) -> str:
        values = row["values"] if row is not None else {}
        labels = {
            "pending": "⏸️ waiting",
            "preparing": "🔄 preparing",
            "ready": "✅ ready",
            "posting": "🔄 posting",
            "partial": "⚠️ partial",
            "posted": "✅ posted",
            "failed": "❌ failed",
        }
        overall = {
            "running": "🔄 Running",
            "recovery": "🔄 Recovery running",
            "failed": "❌ Failed",
            "complete": "✅ Complete",
        }.get(state, state)
        lines = [
            "## 🏆 C1C Leagues — Weekly Update",
            f"**Week:** {week_key}",
            f"**Status:** {overall}",
            "",
            f"📸 Images — {labels.get(values.get('prepare_status', 'pending'), values.get('prepare_status', 'pending'))}",
            f"🦅 Legendary League — {labels.get(values.get('legendary_status', 'pending'), values.get('legendary_status', 'pending'))}",
            f"🌟 Rising Stars League — {labels.get(values.get('rising_status', 'pending'), values.get('rising_status', 'pending'))}",
            f"⚡ Stormforged League — {labels.get(values.get('storm_status', 'pending'), values.get('storm_status', 'pending'))}",
            f"📣 Announcement — {labels.get(values.get('announcement_status', 'pending'), values.get('announcement_status', 'pending'))}",
        ]
        if detail:
            lines.extend(["", detail[:900]])
        lines.extend(["", f"**Last update:** {dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"])
        return "\n".join(lines)

    async def _progress_message(
        self,
        channel: discord.abc.Messageable | None,
        row: dict[str, Any] | None,
        week_key: str,
        *,
        state: str,
        detail: str = "",
    ) -> discord.Message | None:
        if channel is None:
            return None
        content = self._progress_text(row, week_key, state=state, detail=detail)
        view: discord.ui.View | None = LeagueRetryView(self) if state == "failed" else None
        message: discord.Message | None = None
        existing_id = (row["values"].get("progress_message_id", "") if row else "").strip()
        if existing_id and hasattr(channel, "fetch_message"):
            try:
                message = await channel.fetch_message(int(existing_id))  # type: ignore[attr-defined]
            except Exception:
                message = None
        try:
            if message is None:
                message = await channel.send(content, view=view)
                await self._set_job_fields(row, {"progress_message_id": str(message.id)})
            else:
                await message.edit(content=content, view=view)
        except Exception:
            log.exception("failed to update leagues progress message")
            return message
        return message

    async def _is_retry_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id in self._admin_ids():
            return True
        member = interaction.user
        return isinstance(member, discord.Member) and is_admin_member(member)

    async def _handle_retry_interaction(self, interaction: discord.Interaction) -> None:
        if not await self._is_retry_admin(interaction):
            await interaction.response.send_message("You are not allowed to retry this league job.", ephemeral=True)
            return
        progress_id = str(getattr(interaction.message, "id", ""))
        loaded = await self._approval_sheet()
        row = None
        if loaded is not None:
            tab_name, worksheet, header_map, matrix = loaded
            for row_number, raw in enumerate(matrix[1:], start=2):
                values = {
                    name: (str(raw[idx]).strip() if idx < len(raw) else "")
                    for name, idx in header_map.items()
                }
                if values.get("progress_message_id") == progress_id:
                    row = {
                        "tab": tab_name,
                        "worksheet": worksheet,
                        "header_map": header_map,
                        "row_number": row_number,
                        "values": values,
                    }
                    break
        if row is None:
            await interaction.response.send_message("I could not find the league job state for this message.", ephemeral=True)
            return
        if row["values"].get("status") == "posted" or row["values"].get("posted_at_utc"):
            await interaction.response.send_message("This league job is already complete.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        durable_week = self._format_week_key(row["values"].get("season_key"), row["values"].get("week_key"))
        await self._set_job_fields(
            row,
            {"status": "posting", "last_error": "", "updated_at_utc": self._utc_iso()},
        )
        await self._progress_message(interaction.channel, row, durable_week, state="recovery", detail=f"Recovery requested by <@{interaction.user.id}>.")
        try:
            ok = await self.run_leagues_job(
                trigger="retry_button",
                status_channel=interaction.channel,
                week_key=durable_week,
            )
        except Exception as exc:
            ok = False
            error = f"{type(exc).__name__}: {exc}"
            log.exception("league recovery failed")
        else:
            error = "" if ok else row["values"].get("last_error", "posting job returned failure")
        fresh = await self._find_approval_row_for_week(
            row["values"].get("season_key", ""), row["values"].get("week_key", "")
        )
        await self._set_job_fields(
            fresh,
            {
                "status": "posted" if ok else "failed",
                "posted_at_utc": self._utc_iso() if ok else "",
                "updated_at_utc": self._utc_iso(),
                "last_error": error[:500],
            },
        )
        await interaction.followup.send(
            "League recovery completed." if ok else "League recovery stopped again; the status message has the failure.",
            ephemeral=True,
        )

    @staticmethod
    async def _cleanup_posts(messages: list[discord.Message]) -> None:
        # Kept for compatibility with older callers/tests. New league publication
        # recovery uses durable per-component message records instead.
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
        except Exception as exc:
            log.exception("gid lookup failed", extra={"key": spec.key, "tab": spec.sheet_name})
            return f"{slug.title()}: gid lookup failed for {spec.key} ({exc})"
        if gid is None:
            return f"{slug.title()}: gid missing for {spec.sheet_name}"

        last_error = "unknown export failure"
        for attempt in range(1, 4):
            try:
                png_bytes = await export_pdf_as_png(
                    sheet_id,
                    gid,
                    spec.cell_range,
                    log_context={
                        "label": spec.key,
                        "tab": spec.sheet_name,
                        "range": spec.cell_range,
                        "attempt": attempt,
                    },
                    raise_on_failure=True,
                )
                if png_bytes:
                    return discord.File(fp=io.BytesIO(png_bytes), filename=filename)
                last_error = "export returned no data"
            except ImageExportError as exc:
                last_error = str(exc)
                log.warning(
                    "league image export attempt failed",
                    extra={"key": spec.key, "attempt": attempt, "reason": last_error},
                )
            except Exception as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                log.exception(
                    "league image export attempt failed",
                    extra={"key": spec.key, "attempt": attempt},
                )
            if attempt < 3:
                await asyncio.sleep(attempt)

        return f"{slug.title()}: {spec.key} export failed after 3 attempts ({last_error})"

    def _league_role_mention(self) -> str:
        role_id = self._parse_int_config("C1C_LEAGUE_ROLE_ID")
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
