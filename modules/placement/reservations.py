"""Reservation command for holding clan seats inside ticket threads."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Iterable, Optional

import discord
from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import is_admin_member, is_recruiter
from modules.common import feature_flags
from modules.common.logs import log as human_log
from modules.recruitment import availability
from modules.onboarding.watcher_welcome import (
    parse_welcome_thread_name,
    rename_thread_to_reserved,
)
from shared.config import (
    get_clan_lead_ids,
    get_promo_channel_id,
    get_recruiters_thread_id,
    get_recruitment_interact_channel_id,
    get_welcome_channel_id,
)
from shared.cache import telemetry as cache_telemetry
from shared.logfmt import channel_label
from shared.sheets import recruitment, reservations

log = logging.getLogger(__name__)

PROMPT_TIMEOUT_SECONDS = 300
ACTIVE_STATUS = "active"


@dataclass(slots=True)
class ReservationPromptCleanupResult:
    """Outcome from deleting tracked reservation prompt messages."""

    attempted_message_ids: list[int] = field(default_factory=list)
    deleted_count: int = 0
    failed_count: int = 0
    result: str = "skipped"


class ReservationPromptTracker:
    """Tracks exact messages that belong to one reservation prompt flow."""

    def __init__(self) -> None:
        self._messages: list[discord.Message] = []
        self._message_ids: set[int] = set()

    @property
    def message_ids(self) -> list[int]:
        return [int(getattr(message, "id")) for message in self._messages]

    def track(self, message: object | None) -> None:
        message_id = getattr(message, "id", None)
        if message_id is None:
            return
        try:
            normalized_id = int(message_id)
        except (TypeError, ValueError):
            return
        if normalized_id in self._message_ids:
            return
        delete = getattr(message, "delete", None)
        if not callable(delete):
            return
        self._message_ids.add(normalized_id)
        self._messages.append(message)

    async def cleanup(self) -> ReservationPromptCleanupResult:
        result = ReservationPromptCleanupResult(
            attempted_message_ids=[
                int(getattr(message, "id")) for message in self._messages
            ]
        )
        if not self._messages:
            result.result = "noop"
            return result

        for message in self._messages:
            try:
                await message.delete()
            except discord.NotFound:
                continue
            except discord.Forbidden:
                result.failed_count += 1
            except Exception:
                result.failed_count += 1
                log.exception(
                    "reservation_prompt_cleanup_delete_failed",
                    extra={"message_id": getattr(message, "id", None)},
                )
            else:
                result.deleted_count += 1

        result.result = "ok" if result.failed_count == 0 else "partial_failure"
        return result


class ReservationFlowAbort(Exception):
    """Raised when the interactive flow ends without creating a reservation."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(slots=True)
class ReservationDetails:
    """Normalized output from the interactive reservation flow."""

    ticket_user_id: Optional[int]
    ticket_display: str
    ticket_username: Optional[str]
    reserved_until: dt.date
    notes: str


class ReservationConversation:
    """Interactive prompt that collects reservation metadata from staff."""

    CANCEL_KEYWORDS: Iterable[str] = ("cancel",)

    def __init__(
        self,
        ctx: commands.Context,
        *,
        clan_label: str,
        manual_open: int,
        active_reservations: int,
        wait_for: Callable[..., Awaitable[discord.Message]],
        preset_user: tuple[Optional[int], str, Optional[str]] | None = None,
        prompt_tracker: ReservationPromptTracker | None = None,
    ) -> None:
        self.ctx = ctx
        self._clan_label = clan_label
        self._manual_open = manual_open
        self._active_reservations = active_reservations
        self._wait_for = wait_for
        self._author_id = getattr(ctx.author, "id", None)
        self._channel_id = getattr(ctx.channel, "id", None)
        self._preset_user = preset_user
        self.prompt_tracker = prompt_tracker or ReservationPromptTracker()

    async def run(self) -> ReservationDetails:
        """Execute the full conversation flow and return reservation details."""

        user_id, display, username = await self._prompt_user()
        reserved_until = await self._prompt_date()

        notes = ""
        if self._effective_available <= 0:
            notes = await self._prompt_reason()

        while True:
            action = await self._confirm(display, reserved_until, notes)
            if action == "yes":
                return ReservationDetails(
                    ticket_user_id=user_id,
                    ticket_display=display,
                    ticket_username=username,
                    reserved_until=reserved_until,
                    notes=notes,
                )
            if action == "user":
                user_id, display, username = await self._prompt_user(reprompt=True)
                continue
            if action == "date":
                reserved_until = await self._prompt_date(reprompt=True)
                continue

    @property
    def _effective_available(self) -> int:
        return max(self._manual_open - self._active_reservations, 0)

    async def _prompt_user(
        self, *, reprompt: bool = False
    ) -> tuple[Optional[int], str, Optional[str]]:
        prompt = (
            f"Who do you want to reserve a spot for in `{self._clan_label}`?\n"
            "You can @mention them or paste their Discord ID. Type `cancel` to abort."
        )
        if not reprompt and self._preset_user is not None:
            preset = self._preset_user
            self._preset_user = None
            return preset

        if not reprompt:
            await self._send_tracked(prompt)
        else:
            await self._send_tracked(
                "Please @mention the recruit or paste their Discord ID. Type `cancel` to abort."
            )

        while True:
            message = await self._next_message()
            content = (message.content or "").strip()
            lowered = content.lower()
            if lowered in self.CANCEL_KEYWORDS:
                await self.ctx.send("Reservation cancelled. No changes made.")
                raise ReservationFlowAbort("cancelled")

            mention = next(iter(getattr(message, "mentions", []) or []), None)
            if mention is not None:
                display = getattr(
                    mention, "mention", f"<@{getattr(mention, 'id', '???')}>"
                )
                ticket_username = _display_name(mention)
                return getattr(mention, "id", None), display, ticket_username

            match = re.search(r"\d+", content)
            if match:
                try:
                    candidate_id = int(match.group(0))
                except ValueError:
                    candidate_id = None
                if candidate_id is not None:
                    member = _resolve_member(self.ctx.guild, candidate_id)
                    if member is not None:
                        mention_text = getattr(member, "mention", f"<@{candidate_id}>")
                        return candidate_id, mention_text, _display_name(member)
                    return candidate_id, f"<@{candidate_id}>", None

            await self._send_tracked(
                "I didn't catch that. Please @mention the recruit or paste their Discord ID (or type `cancel`)."
            )

    async def _prompt_date(self, *, reprompt: bool = False) -> dt.date:
        prompt = (
            "Until which date do you want to reserve the spot?"
            " Please use `YYYY-MM-DD` (no time). Type `cancel` to abort."
        )
        if not reprompt:
            await self._send_tracked(prompt)
        else:
            await self._send_tracked(
                "Please provide the reservation end date in `YYYY-MM-DD` (or type `cancel`)."
            )

        while True:
            message = await self._next_message()
            content = (message.content or "").strip()
            lowered = content.lower()
            if lowered in self.CANCEL_KEYWORDS:
                await self.ctx.send("Reservation cancelled. No changes made.")
                raise ReservationFlowAbort("cancelled")
            try:
                parsed = dt.date.fromisoformat(content)
            except ValueError:
                parsed = None
            today = dt.date.today()
            if parsed is None or parsed < today:
                await self._send_tracked(
                    "Please use `YYYY-MM-DD` and make sure the date is today or later."
                )
                continue
            return parsed

    async def _prompt_reason(self) -> str:
        prompt = (
            f"Heads up: `{self._clan_label}` currently has 0 effective open spots when existing"
            " reservations are included. You can still reserve a seat, but please add a short"
            " reason (or type `cancel` to abort)."
        )
        await self._send_tracked(prompt)

        while True:
            message = await self._next_message()
            content = (message.content or "").strip()
            if content.lower() in self.CANCEL_KEYWORDS:
                await self.ctx.send("Reservation cancelled. No changes made.")
                raise ReservationFlowAbort("cancelled")
            if content:
                return content
            await self._send_tracked(
                "Please add a short reason (or type `cancel` to abort)."
            )

    async def _confirm(self, display: str, reserved_until: dt.date, notes: str) -> str:
        lines = [
            f"Reserve 1 spot in `{self._clan_label}` for {display} until `{reserved_until.isoformat()}`.",
        ]
        if notes:
            lines.append(f"Reason: {notes}")
        lines.append(
            "Type `yes` to save, `no` to cancel, or `change` to edit the recruit/date."
        )
        await self._send_tracked("\n".join(lines))

        while True:
            message = await self._next_message()
            content = (message.content or "").strip().lower()
            if content in self.CANCEL_KEYWORDS or content in {"no", "n"}:
                await self.ctx.send("Reservation cancelled. No changes made.")
                raise ReservationFlowAbort("cancelled")
            if content in {"yes", "y"}:
                return "yes"
            if content in {"change", "c"}:
                await self._send_tracked(
                    "Type `user` to change the recruit or `date` to change the end date."
                )
                while True:
                    followup = await self._next_message()
                    follow_content = (followup.content or "").strip().lower()
                    if follow_content in self.CANCEL_KEYWORDS:
                        await self.ctx.send("Reservation cancelled. No changes made.")
                        raise ReservationFlowAbort("cancelled")
                    if "user" in follow_content:
                        return "user"
                    if "date" in follow_content:
                        return "date"
                    await self._send_tracked(
                        "Please type `user` or `date`, or `cancel` to abort."
                    )
                continue
            await self._send_tracked(
                "Please reply with `yes`, `no`, or `change` (or type `cancel`)."
            )

    async def _next_message(self) -> discord.Message:
        if self._wait_for is None:
            raise ReservationFlowAbort("missing-wait-for")

        try:
            message = await self._wait_for(
                "message",
                timeout=PROMPT_TIMEOUT_SECONDS,
                check=lambda m: (
                    getattr(m.author, "id", None) == self._author_id
                    and getattr(m.channel, "id", None) == self._channel_id
                ),
            )
        except asyncio.TimeoutError:
            await self.ctx.send(
                "Reservation timed out. Please run `!reserve` again if you still need it."
            )
            raise ReservationFlowAbort("timeout")
        self.prompt_tracker.track(message)
        return message

    async def _send_tracked(self, content: str) -> discord.Message:
        message = await self.ctx.send(content)
        self.prompt_tracker.track(message)
        return message


def _display_name(member: object) -> Optional[str]:
    for attr in ("display_name", "nick", "name"):
        value = getattr(member, attr, None)
        if value:
            text = str(value).strip()
            if text:
                return text
    return None


def _resolve_member(guild: object, member_id: int):
    getter = getattr(guild, "get_member", None)
    if callable(getter):
        try:
            return getter(member_id)
        except Exception:  # pragma: no cover - defensive guard
            log.exception(
                "guild get_member failed",
                extra={"member_id": member_id},
            )
    return None


def _resolve_configured_reservation_clan_tag(clan_tag: str) -> str:
    """Resolve the sheet clan tag through the availability Config clan_tag header."""
    return availability.resolve_configured_clan_tag(clan_tag)


async def _aresolve_configured_reservation_clan_tag(clan_tag: str) -> str:
    """Async resolve of the sheet clan tag through the availability Config clan_tag header."""
    return await availability.aresolve_configured_clan_tag(clan_tag)


def _normalize_tag(tag: str | None) -> str:
    text = "" if tag is None else str(tag).strip().upper()
    return "".join(ch for ch in text if ch.isalnum())


def _parse_manual_open(row: list[str]) -> int:
    if len(row) <= 4:
        return 0
    return _to_int(row[4])


def _to_int(value: str | None) -> int:
    if not value:
        return 0
    match = re.search(r"-?\d+", str(value))
    if not match:
        return 0
    try:
        return int(match.group(0))
    except ValueError:
        return 0


def _ticket_parent_ids() -> set[int]:
    parents: set[int] = set()
    for getter in (get_welcome_channel_id, get_promo_channel_id):
        value = getter()
        if value is None:
            continue
        try:
            parents.add(int(value))
        except (TypeError, ValueError):
            continue
    return parents


def _is_ticket_thread(channel: object) -> bool:
    if channel is None:
        return False

    channel_type = getattr(channel, "type", None)
    thread_types = {
        getattr(discord.ChannelType, "public_thread", None),
        getattr(discord.ChannelType, "private_thread", None),
        getattr(discord.ChannelType, "news_thread", None),
    }
    if channel_type not in thread_types:
        return False

    parent_id = getattr(channel, "parent_id", None)
    if parent_id is None:
        return False

    try:
        parent_value = int(parent_id)
    except (TypeError, ValueError):
        return False

    return parent_value in _ticket_parent_ids()


def _reservation_ticket_id(channel: object) -> str | None:
    parts = parse_welcome_thread_name(getattr(channel, "name", None))
    if parts and parts.ticket_code:
        return parts.ticket_code
    return None


def _reservations_enabled() -> bool:
    for key in (
        "FEATURE_RESERVATIONS",
        "feature_reservations",
        "placement_reservations",
    ):
        try:
            if feature_flags.is_enabled(key):
                return True
        except Exception:
            log.exception("feature toggle check failed", extra={"feature": key})
    return False


async def _ensure_fresh_clans_for_reservations(
    *, actor: str, clan_tag: str, user: str, source: str
) -> bool:
    """Require a sync-fresh clans cache before reservation availability recompute."""
    try:
        await cache_telemetry.refresh_now("clans", actor=actor)
    except Exception:
        log.exception(
            "failed to refresh clans cache for reservation availability recompute",
            extra={
                "actor": actor,
                "clan_tag": _normalize_tag(clan_tag),
                "user": user,
                "source": source,
            },
        )
        return False
    snapshot = cache_telemetry.get_snapshot("clans")
    if (not snapshot.available) or snapshot.last_result not in {"ok", "retry_ok"}:
        log.warning(
            "reservation availability recompute blocked due to unavailable/failed clans cache refresh",
            extra={
                "actor": actor,
                "clan_tag": _normalize_tag(clan_tag),
                "user": user,
                "source": source,
                "reason": "fresh_clans_unavailable",
                "last_result": snapshot.last_result,
                "last_error": snapshot.last_error,
            },
        )
        return False
    return True


@dataclass(slots=True)
class _ThreadContext:
    ticket_code: str
    username: str
    thread: object
    guild: discord.Guild | None


@dataclass(slots=True)
class _ThreadReservationView:
    context: _ThreadContext
    status: str
    reservation: reservations.ReservationRow | None
    matches: list[reservations.ReservationRow]
    normalized_thread_tag: str | None
    thread_tag_display: str | None


def _normalize_date(value: dt.date | None) -> dt.date:
    if value is not None:
        return value
    return dt.date.max


def _reservation_sort_key(row: reservations.ReservationRow) -> tuple[dt.date, int]:
    return (_normalize_date(row.reserved_until), row.row_number)


def _reservation_display_date(reserved_until: dt.date | None) -> str:
    if reserved_until is None:
        return "unknown"
    return reserved_until.isoformat()


def _reservation_creator_display(
    guild: discord.Guild | None, recruiter_id: Optional[int]
) -> str:
    if recruiter_id is None:
        return "unknown recruiter"
    if guild is not None:
        try:
            member = guild.get_member(recruiter_id)
        except Exception:  # pragma: no cover - guild lookup best effort
            member = None
        if member is not None and getattr(member, "mention", None):
            return getattr(member, "mention")
    return f"<@{recruiter_id}>"


def _reservation_user_display(
    guild: discord.Guild | None,
    row: reservations.ReservationRow,
    *,
    fallback_username: str,
) -> str:
    if row.ticket_user_id is not None:
        if guild is not None:
            try:
                member = guild.get_member(row.ticket_user_id)
            except Exception:  # pragma: no cover - guild lookup best effort
                member = None
            if member is not None and getattr(member, "mention", None):
                return getattr(member, "mention")
        return f"<@{row.ticket_user_id}>"
    snapshot = (row.username_snapshot or "").strip()
    if snapshot:
        return snapshot
    return fallback_username


def _reservation_status_display(status: str) -> str:
    status_text = (status or "").strip()
    if not status_text:
        return "status=unknown"
    return f"status={status_text}"


def _thread_recruit_display(
    guild: discord.Guild | None,
    rows: list[reservations.ReservationRow],
    *,
    parsed_username: str,
) -> str:
    for row in rows:
        display = _reservation_user_display(
            guild, row, fallback_username=parsed_username
        )
        if display:
            return display
    return parsed_username


def _thread_owner_id(thread: discord.Thread | None) -> Optional[int]:
    if thread is None:
        return None
    owner_id = getattr(thread, "owner_id", None)
    try:
        return int(owner_id) if owner_id is not None else None
    except (TypeError, ValueError):
        return None


def _parse_thread_context(channel: object) -> Optional[_ThreadContext]:
    if not _is_ticket_thread(channel):
        return None

    parts = parse_welcome_thread_name(getattr(channel, "name", None))
    if parts is None:
        return None

    return _ThreadContext(
        ticket_code=parts.ticket_code,
        username=parts.username,
        thread=channel,
        guild=getattr(channel, "guild", None),
    )


def _thread_clan_tag_info(thread: object) -> tuple[str | None, str | None]:
    parts = parse_welcome_thread_name(getattr(thread, "name", None))
    if parts is None:
        return None, None
    clan_tag = (parts.clan_tag or "").strip()
    if not clan_tag:
        return None, None
    display = clan_tag.upper()
    normalized = _normalize_tag(clan_tag)
    return (normalized or None), (display or normalized or None)


def _thread_name(thread: object) -> str:
    name = getattr(thread, "name", None)
    if isinstance(name, str) and name.strip():
        return name
    identifier = getattr(thread, "id", None)
    return f"thread:{identifier}" if identifier is not None else "thread:unknown"


def _control_thread_hint() -> str:
    channel_id = get_recruiters_thread_id()
    if channel_id:
        return f"<#{channel_id}>"
    return "the recruiter control thread"


def _interact_channel_hint() -> str:
    channel_id = get_recruitment_interact_channel_id()
    if channel_id:
        return f"<#{channel_id}>"
    return "the recruitment interact channel"


def _is_clan_lead_user(user: object) -> bool:
    user_id = getattr(user, "id", None)
    if user_id is None:
        return False
    try:
        numeric = int(user_id)
    except (TypeError, ValueError):
        return False
    return numeric in get_clan_lead_ids()


def _parse_member_token(token: str) -> Optional[int]:
    text = (token or "").strip()
    if not text:
        return None
    match = re.search(r"\d+", text)
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


async def _get_thread_reservation(
    channel: object,
    *,
    parsed_username: str | None,
) -> Optional[_ThreadReservationView]:
    context = _parse_thread_context(channel)
    if context is None:
        return None

    owner_id = _thread_owner_id(context.thread)
    matches = await reservations.find_active_reservations_for_recruit(
        ticket_user_id=owner_id,
        username=parsed_username or context.username,
    )
    matches = [row for row in matches if row.is_active]
    matches.sort(key=_reservation_sort_key)

    normalized_tag, display_tag = _thread_clan_tag_info(context.thread)
    if not matches:
        return _ThreadReservationView(
            context=context,
            status="none",
            reservation=None,
            matches=[],
            normalized_thread_tag=normalized_tag,
            thread_tag_display=display_tag,
        )

    if len(matches) > 1:
        _log_thread_anomaly(context, matches, reason="multiple_active")
        return _ThreadReservationView(
            context=context,
            status="ambiguous",
            reservation=None,
            matches=matches,
            normalized_thread_tag=normalized_tag,
            thread_tag_display=display_tag,
        )

    reservation = matches[0]
    row_tag = reservation.normalized_clan_tag or _normalize_tag(reservation.clan_tag)
    if normalized_tag and row_tag and normalized_tag != row_tag:
        _log_thread_anomaly(
            context,
            [reservation],
            reason="mismatch",
            extra={"thread_tag": normalized_tag, "row_tag": row_tag},
        )
        return _ThreadReservationView(
            context=context,
            status="mismatch",
            reservation=reservation,
            matches=matches,
            normalized_thread_tag=normalized_tag,
            thread_tag_display=display_tag,
        )

    return _ThreadReservationView(
        context=context,
        status="ok",
        reservation=reservation,
        matches=matches,
        normalized_thread_tag=normalized_tag,
        thread_tag_display=display_tag,
    )


def _log_thread_anomaly(
    context: _ThreadContext,
    rows: list[reservations.ReservationRow],
    *,
    reason: str,
    extra: dict | None = None,
) -> None:
    row_numbers = [row.row_number for row in rows]
    payload = {
        "ticket": context.ticket_code,
        "user": context.username,
        "thread_name": _thread_name(context.thread),
        "rows": row_numbers,
        "reason": reason,
    }
    if extra:
        payload.update(extra)
    log.warning("thread reservation anomaly", extra=payload)
    human_log.human(
        "warning",
        "⚠️ reservation_thread_check — ticket=%s • user=%s • thread=%s • reason=%s • rows=%s"
        % (
            context.ticket_code,
            context.username,
            _thread_name(context.thread),
            reason,
            ",".join(str(number) for number in row_numbers) or "none",
        ),
    )


def _is_authorized(ctx: commands.Context) -> bool:
    return bool(is_recruiter(ctx) or is_admin_member(ctx))


class ReservationCog(commands.Cog):
    """Cog hosting reservation commands for recruitment staff."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @tier("staff")
    @help_metadata(
        function_group="recruitment",
        section="recruitment",
        access_tier="staff",
    )
    @commands.command(
        name="reserve",
        help="Reserve, release, or extend a clan seat for a recruit.",
        brief="Reservation management for a recruit.",
    )
    async def reserve(self, ctx: commands.Context, *args: str) -> None:
        if not args:
            await ctx.reply(
                "Usage: `!reserve <clantag>` (run inside the recruit's ticket thread).",
                mention_author=False,
            )
            return

        subcommand = (args[0] or "").strip()

        if subcommand.lower() == "release":
            await self._handle_release(ctx, list(args[1:]))
            return

        if subcommand.lower() == "extend":
            await self._handle_extend(ctx, list(args[1:]))
            return

        if len(args) > 2:
            await ctx.reply(
                "Usage: `!reserve <clantag>` or `!reserve <clantag> @recruit` (run inside the recruit's ticket thread).",
                mention_author=False,
            )
            return

        clan_tag = subcommand
        recruit_token = args[1] if len(args) == 2 else None

        if not _reservations_enabled():
            await ctx.reply(
                "Reservations are currently disabled. Please poke an admin if you think this is wrong.",
                mention_author=False,
            )
            return

        if ctx.guild is None:
            await ctx.reply(
                "⚠️ `!reserve` can only be used inside a server thread.",
                mention_author=False,
            )
            return

        if not (is_recruiter(ctx) or is_admin_member(ctx)):
            await ctx.reply(
                "Only Recruiters (or Admins) can reserve clan spots. If you need a hold, poke the Recruitment Crew.",
                mention_author=False,
            )
            return

        if not _is_ticket_thread(ctx.channel):
            await ctx.reply(
                "Please run `!reserve` inside the recruit’s ticket thread so I know who you’re talking about.",
                mention_author=False,
            )
            return

        preset_user: tuple[Optional[int], str, Optional[str]] | None = None
        if recruit_token:
            member_id = _parse_member_token(recruit_token)
            if member_id is None:
                await ctx.reply(
                    "I couldn’t understand that recruit reference. Mention them directly (for example `!reserve C1C9 @user`) "
                    "or just run `!reserve C1C9` and I’ll ask who it’s for.",
                    mention_author=False,
                )
                return
            member = _resolve_member(ctx.guild, member_id)
            mention_text = getattr(member, "mention", f"<@{member_id}>")
            preset_user = (member_id, mention_text, _display_name(member))

        try:
            preflight_plan = await availability.preflight_clan_availability_update(
                clan_tag
            )
        except ValueError as exc:
            reason = str(exc)
            if reason.startswith("Unknown clan tag"):
                await ctx.reply(
                    f"I don’t know the clan tag `{clan_tag}`. Please check the tag and try again.",
                    mention_author=False,
                )
            else:
                log.exception(
                    "reservation availability preflight failed before prompt",
                    extra={"clan_tag": _normalize_tag(clan_tag), "reason": reason},
                )
                await ctx.reply(
                    "I could not verify the clan availability sheet configuration/value, so no reservation was created. Please contact an admin and try again after it is fixed.",
                    mention_author=False,
                )
            return
        except Exception as exc:
            log.exception(
                "reservation availability preflight failed before prompt",
                extra={"clan_tag": _normalize_tag(clan_tag), "reason": repr(exc)},
            )
            await ctx.reply(
                "I could not verify the clan availability sheet configuration/value, so no reservation was created. Please contact an admin and try again after it is fixed.",
                mention_author=False,
            )
            return

        tag_index = preflight_plan.headers.header_map["clan_tag"]
        sheet_tag = (
            preflight_plan.row[tag_index]
            if tag_index < len(preflight_plan.row) and preflight_plan.row[tag_index]
            else clan_tag
        )
        manual_open = preflight_plan.numeric_values["manual_open_spots"]

        try:
            active_reservations = await reservations.count_active_reservations_for_clan(
                sheet_tag
            )
        except Exception:
            log.exception(
                "failed to count active reservations",
                extra={"clan_tag": _normalize_tag(sheet_tag)},
            )
            await ctx.reply(
                "Something went wrong while checking current reservations. Please try again later or contact an admin.",
                mention_author=False,
            )
            return

        wait_for = getattr(self.bot, "wait_for", None)
        if wait_for is None:
            await ctx.reply(
                "I can't start the reservation flow right now. Please try again later.",
                mention_author=False,
            )
            return

        prompt_tracker = ReservationPromptTracker()
        prompt_tracker.track(getattr(ctx, "message", None))
        conversation = ReservationConversation(
            ctx,
            clan_label=sheet_tag,
            manual_open=manual_open,
            active_reservations=active_reservations,
            wait_for=wait_for,
            preset_user=preset_user,
            prompt_tracker=prompt_tracker,
        )

        try:
            details = await conversation.run()
        except ReservationFlowAbort:
            return

        try:
            existing_rows = await reservations.find_active_reservations_for_recruit(
                ticket_user_id=details.ticket_user_id,
                username=details.ticket_username,
            )
        except Exception:
            log.exception(
                "failed to check existing reservations",
                extra={"thread_id": getattr(ctx.channel, "id", None)},
            )
            await ctx.reply(
                "Something went wrong while checking existing reservations for this recruit. Please try again later.",
                mention_author=False,
            )
            return

        existing_matches = [row for row in existing_rows if row.is_active]
        if existing_matches:
            existing_matches.sort(key=_reservation_sort_key)
            blocker = existing_matches[0]
            blocker_tag = blocker.normalized_clan_tag or (
                blocker.clan_tag or "unknown clan"
            )
            ticket_label = "unknown"
            blocker_thread = await _resolve_thread(self.bot, blocker.thread_id)
            thread_label = (
                _thread_name(blocker_thread)
                if blocker_thread is not None
                else "unknown thread"
            )
            parts = (
                parse_welcome_thread_name(getattr(blocker_thread, "name", None))
                if blocker_thread
                else None
            )
            if parts and parts.ticket_code:
                ticket_label = parts.ticket_code
            control_hint = _control_thread_hint()
            await ctx.reply(
                (
                    f"{details.ticket_display} already has an active reservation in `{blocker_tag}` "
                    f"(ticket `{ticket_label}` / `{thread_label}`).\n"
                    f"Please go to {control_hint} and release or extend that hold before creating a new reservation."
                ),
                mention_author=False,
            )
            human_log.human(
                "warning",
                "⚠️ reservation_create_blocked — thread=%s • recruit=%s • clan=%s • result=duplicate"
                % (
                    _thread_name(ctx.channel),
                    details.ticket_display,
                    blocker_tag,
                ),
            )
            return

        now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        username_snapshot = details.ticket_username or details.ticket_display

        row_values = [
            str(getattr(ctx.channel, "id", "")),
            str(details.ticket_user_id or ""),
            str(getattr(ctx.author, "id", "")),
            sheet_tag,
            details.reserved_until.isoformat(),
            now.isoformat(),
            ACTIVE_STATUS,
            details.notes,
            username_snapshot or "",
        ]

        try:
            preflight_plan = await availability.preflight_clan_availability_update(
                sheet_tag
            )
        except Exception as exc:
            log.exception(
                "reservation availability preflight failed before append",
                extra={
                    "clan_tag": _normalize_tag(sheet_tag),
                    "thread_id": getattr(ctx.channel, "id", None),
                    "ticket_user_id": details.ticket_user_id,
                    "error_type": type(exc).__name__,
                    "error": repr(exc),
                },
            )
            await ctx.send(
                "I could not verify the clan availability sheet configuration/value, so no reservation row was added. Please fix the sheet configuration/value and try again."
            )
            return

        try:
            await reservations.append_reservation_row(row_values)
        except Exception:
            log.exception(
                "failed to append reservation row",
                extra={
                    "clan_tag": _normalize_tag(sheet_tag),
                    "thread_id": getattr(ctx.channel, "id", None),
                },
            )
            await ctx.send(
                "Something went wrong while saving the reservation to the sheet. No changes were made. Please try again or poke an admin."
            )
            return

        source = "reserve"
        actor = "placement_reservation"
        actor_user = f"{ctx.author} ({getattr(ctx.author, 'id', '-')})"
        clans_fresh = await _ensure_fresh_clans_for_reservations(
            actor=actor,
            clan_tag=sheet_tag,
            user=actor_user,
            source=source,
        )
        reservation_row_ref = f"append:{now.isoformat()}"
        if not clans_fresh:
            log.warning(
                "skipped recompute clan availability",
                extra={
                    "reason": "fresh_clans_unavailable",
                    "clan_tag": _normalize_tag(sheet_tag),
                    "reservation_row": reservation_row_ref,
                    "thread_id": getattr(ctx.channel, "id", None),
                    "ticket_user_id": details.ticket_user_id,
                    "user": actor_user,
                    "source": source,
                },
            )
            human_log.human(
                "warning",
                "⚠️ reservation_partial_success — source=reserve • clan=%s • reservation_row=%s • thread=%s • ticket_user=%s • error_type=FreshClansUnavailable • error=fresh_clans_unavailable • action=rerun recompute/refresh for clan and verify recruiter-facing bot_info open spots"
                % (
                    _normalize_tag(sheet_tag),
                    reservation_row_ref,
                    getattr(ctx.channel, "id", "-"),
                    details.ticket_user_id or "-",
                ),
            )
            await ctx.send(
                "Reservation row was added, but recruiter-facing availability was NOT updated. Admin hint: rerun recompute/refresh for this clan and manually verify bot_info/open spots."
            )
            return
        try:
            await availability.recompute_clan_availability(sheet_tag, guild=ctx.guild)
        except Exception as exc:
            error_type = type(exc).__name__
            error_text = str(exc) or repr(exc)
            log.exception(
                "failed to recompute clan availability",
                extra={
                    "clan_tag": _normalize_tag(sheet_tag),
                    "reservation_row": reservation_row_ref,
                    "thread_id": getattr(ctx.channel, "id", None),
                    "ticket_user_id": details.ticket_user_id,
                    "user": actor_user,
                    "source": source,
                    "error_type": error_type,
                    "error": repr(exc),
                },
            )
            human_log.human(
                "error",
                "❌ reservation_partial_success — source=reserve • clan=%s • reservation_row=%s • thread=%s • ticket_user=%s • error_type=%s • error=%s • action=rerun recompute/refresh for clan and verify recruiter-facing bot_info open spots"
                % (
                    _normalize_tag(sheet_tag),
                    reservation_row_ref,
                    getattr(ctx.channel, "id", "-"),
                    details.ticket_user_id or "-",
                    error_type,
                    error_text,
                ),
            )
            await ctx.send(
                "Reservation row was added, but recruiter-facing availability was NOT updated. Admin hint: rerun recompute/refresh for this clan and manually verify bot_info/open spots."
            )
            return

        fallback_reserved = active_reservations + 1
        fallback_available = max(manual_open - fallback_reserved, 0)

        updated_entry = await recruitment.afind_clan_row(sheet_tag, force=True)
        updated_row = updated_entry[1] if updated_entry is not None else None
        if updated_row is not None:
            ah_value = _safe_cell(updated_row, 33, fallback_reserved)
            af_value = _safe_cell(updated_row, 31, fallback_available)
        else:
            ah_value = str(fallback_reserved)
            af_value = str(fallback_available)

        await ctx.send(
            (
                f"✅ Reserved 1 spot in `{sheet_tag}` for {details.ticket_display} "
                f"until `{details.reserved_until.isoformat()}`. Reserved for this clan: "
                f"`{ah_value}`. Effective open spots: `{af_value}`."
            )
        )

        rename_succeeded = False
        thread = ctx.channel if _is_ticket_thread(ctx.channel) else None
        if thread is not None:
            try:
                rename_succeeded = await rename_thread_to_reserved(thread, sheet_tag)
            except Exception:
                log.exception(
                    "failed to rename welcome thread for reservation",
                    extra={
                        "thread_id": getattr(thread, "id", None),
                        "clan_tag": _normalize_tag(sheet_tag),
                    },
                )

        if rename_succeeded:
            cleanup_result = await prompt_tracker.cleanup()
            log.info(
                "reservation_prompt_cleanup",
                extra={
                    "ticket_id": _reservation_ticket_id(ctx.channel),
                    "thread_id": getattr(ctx.channel, "id", None),
                    "clan_tag": _normalize_tag(sheet_tag),
                    "recruit_user_id": details.ticket_user_id,
                    "message_ids_attempted": cleanup_result.attempted_message_ids,
                    "deleted_count": cleanup_result.deleted_count,
                    "failed_count": cleanup_result.failed_count,
                    "result": cleanup_result.result,
                },
            )

        log.info(
            "[reserve] reservation created",
            extra={
                "clan_tag": _normalize_tag(sheet_tag),
                "thread_id": getattr(ctx.channel, "id", None),
                "recruiter_id": getattr(ctx.author, "id", None),
                "ticket_user_id": details.ticket_user_id,
                "reserved_until": details.reserved_until.isoformat(),
                "notes": details.notes,
            },
        )

    async def _handle_release(self, ctx: commands.Context, args: list[str]) -> None:
        if not _reservations_enabled():
            await ctx.reply(
                "Reservations are currently disabled. Please poke an admin if you think this is wrong.",
                mention_author=False,
            )
            return

        if ctx.guild is None or not _is_authorized(ctx):
            await ctx.reply(
                "Only Recruiters (or Admins) can reserve clan spots. If you need a hold, poke the Recruitment Crew.",
                mention_author=False,
            )
            return

        await self._handle_global_release(ctx, args)

    async def _handle_global_release(
        self, ctx: commands.Context, args: list[str]
    ) -> None:
        if len(args) < 2:
            await ctx.reply(
                "Usage: `!reserve release @user <clan_tag>`.",
                mention_author=False,
            )
            return

        member_id = _parse_member_token(args[0])
        if member_id is None:
            await ctx.reply(
                "Please @mention the recruit or paste their Discord ID before the clan tag.",
                mention_author=False,
            )
            return

        member = _resolve_member(ctx.guild, member_id)
        if member is None:
            await ctx.reply(
                "I couldn't find that member in this server. Please double-check the mention and try again.",
                mention_author=False,
            )
            return

        clan_tag = args[1]
        try:
            sheet_tag = await _aresolve_configured_reservation_clan_tag(clan_tag)
        except ValueError:
            await ctx.reply(
                f"I don’t know the clan tag `{clan_tag}`. Please check the tag and try again.",
                mention_author=False,
            )
            return

        normalized_tag = _normalize_tag(sheet_tag)
        display_member = getattr(member, "mention", f"<@{member_id}>")
        member_name = _display_name(member)

        try:
            matches = await reservations.find_active_reservations_for_recruit(
                ticket_user_id=member_id,
                username=member_name,
            )
        except Exception:
            log.exception(
                "failed to load reservations for global release",
                extra={
                    "clan_tag": normalized_tag,
                    "member": member_id,
                    "channel": _thread_name(ctx.channel),
                    "channel_id": getattr(ctx.channel, "id", None),
                },
            )
            await ctx.reply(
                "Something went wrong while looking up reservations for that recruit. Please try again later.",
                mention_author=False,
            )
            return

        filtered = [row for row in matches if row.normalized_clan_tag == normalized_tag]
        if not filtered:
            await ctx.reply(
                f"No active reservation in `{sheet_tag}` found for {display_member}.",
                mention_author=False,
            )
            human_log.human(
                "warning",
                "⚠️ reservation_release — channel=%s • user=%s • clan=%s • result=not_found • source=global"
                % (
                    channel_label(ctx.guild, getattr(ctx.channel, "id", None)),
                    display_member,
                    sheet_tag,
                ),
            )
            return

        if len(filtered) > 1:
            log.warning(
                "multiple reservations matched global release",
                extra={
                    "member": member_id,
                    "clan_tag": normalized_tag,
                    "rows": [row.row_number for row in filtered],
                    "channel": _thread_name(ctx.channel),
                    "channel_id": getattr(ctx.channel, "id", None),
                },
            )
            await ctx.reply(
                "Multiple reservations matched that recruit and clan. Please fix the ledger in Sheets before retrying.",
                mention_author=False,
            )
            human_log.human(
                "warning",
                "⚠️ reservation_release — channel=%s • user=%s • clan=%s • result=error • reason=multiple_rows • source=global"
                % (
                    channel_label(ctx.guild, getattr(ctx.channel, "id", None)),
                    display_member,
                    sheet_tag,
                ),
            )
            return

        target = filtered[0]
        try:
            await reservations.update_reservation_status(target.row_number, "released")
        except Exception:
            log.exception(
                "failed to release reservation globally",
                extra={
                    "row": target.row_number,
                    "clan_tag": normalized_tag,
                    "channel": _thread_name(ctx.channel),
                    "channel_id": getattr(ctx.channel, "id", None),
                },
            )
            await ctx.reply(
                "I couldn't mark the reservation as released. Please try again later or contact an admin.",
                mention_author=False,
            )
            return

        if sheet_tag:
            try:
                await availability.adjust_manual_open_spots(sheet_tag, 1)
            except Exception:
                log.exception(
                    "failed to adjust manual open spots after global release",
                    extra={
                        "clan_tag": normalized_tag,
                        "channel": _thread_name(ctx.channel),
                        "channel_id": getattr(ctx.channel, "id", None),
                    },
                )
            try:
                await availability.recompute_clan_availability(
                    sheet_tag, guild=ctx.guild
                )
            except Exception:
                log.exception(
                    "failed to recompute availability after global release",
                    extra={
                        "clan_tag": normalized_tag,
                        "channel": _thread_name(ctx.channel),
                        "channel_id": getattr(ctx.channel, "id", None),
                    },
                )

        await ctx.send(
            f"Released the reserved seat in `{sheet_tag}` for {display_member} and returned it to the open pool."
        )

        human_log.human(
            "info",
            "🧭 reservation_release — channel=%s • user=%s • clan=%s • result=ok • source=global"
            % (
                channel_label(ctx.guild, getattr(ctx.channel, "id", None)),
                display_member,
                sheet_tag,
            ),
        )

    async def _handle_extend(self, ctx: commands.Context, args: list[str]) -> None:
        if not _reservations_enabled():
            await ctx.reply(
                "Reservations are currently disabled. Please poke an admin if you think this is wrong.",
                mention_author=False,
            )
            return

        if ctx.guild is None or not _is_authorized(ctx):
            await ctx.reply(
                "Only Recruiters (or Admins) can reserve clan spots. If you need a hold, poke the Recruitment Crew.",
                mention_author=False,
            )
            return

        await self._handle_global_extend(ctx, args)

    async def _handle_global_extend(
        self, ctx: commands.Context, args: list[str]
    ) -> None:
        if len(args) < 3:
            await ctx.reply(
                "Usage: `!reserve extend @user <clan_tag> <YYYY-MM-DD>`.",
                mention_author=False,
            )
            return

        member_id = _parse_member_token(args[0])
        if member_id is None:
            await ctx.reply(
                "Please @mention the recruit or paste their Discord ID before the clan tag and date.",
                mention_author=False,
            )
            return

        member = _resolve_member(ctx.guild, member_id)
        if member is None:
            await ctx.reply(
                "I couldn't find that member in this server. Please double-check the mention and try again.",
                mention_author=False,
            )
            return

        clan_tag = args[1]
        try:
            sheet_tag = await _aresolve_configured_reservation_clan_tag(clan_tag)
        except ValueError:
            await ctx.reply(
                f"I don’t know the clan tag `{clan_tag}`. Please check the tag and try again.",
                mention_author=False,
            )
            return

        normalized_tag = _normalize_tag(sheet_tag)
        display_member = getattr(member, "mention", f"<@{member_id}>")
        member_name = _display_name(member)

        date_token = (args[2] or "").strip()
        try:
            new_date = dt.date.fromisoformat(date_token)
        except ValueError:
            new_date = None
        today = dt.date.today()
        if new_date is None or new_date < today:
            await ctx.reply(
                "Please provide a valid date in `YYYY-MM-DD` that is today or later.",
                mention_author=False,
            )
            human_log.human(
                "warning",
                "⚠️ reservation_extend — channel=%s • user=%s • clan=%s • result=error • reason=invalid_date • source=global"
                % (
                    channel_label(ctx.guild, getattr(ctx.channel, "id", None)),
                    display_member,
                    sheet_tag,
                ),
            )
            return

        try:
            matches = await reservations.find_active_reservations_for_recruit(
                ticket_user_id=member_id,
                username=member_name,
            )
        except Exception:
            log.exception(
                "failed to load reservations for global extend",
                extra={
                    "clan_tag": normalized_tag,
                    "member": member_id,
                    "channel": _thread_name(ctx.channel),
                    "channel_id": getattr(ctx.channel, "id", None),
                },
            )
            await ctx.reply(
                "Something went wrong while looking up reservations for that recruit. Please try again later.",
                mention_author=False,
            )
            return

        filtered = [row for row in matches if row.normalized_clan_tag == normalized_tag]
        if not filtered:
            await ctx.reply(
                f"No active reservation in `{sheet_tag}` found for {display_member}.",
                mention_author=False,
            )
            human_log.human(
                "warning",
                "⚠️ reservation_extend — channel=%s • user=%s • clan=%s • result=not_found • source=global"
                % (
                    channel_label(ctx.guild, getattr(ctx.channel, "id", None)),
                    display_member,
                    sheet_tag,
                ),
            )
            return

        if len(filtered) > 1:
            log.warning(
                "multiple reservations matched global extend",
                extra={
                    "member": member_id,
                    "clan_tag": normalized_tag,
                    "rows": [row.row_number for row in filtered],
                    "channel": _thread_name(ctx.channel),
                    "channel_id": getattr(ctx.channel, "id", None),
                },
            )
            await ctx.reply(
                "Multiple reservations matched that recruit and clan. Please fix the ledger in Sheets before retrying.",
                mention_author=False,
            )
            human_log.human(
                "warning",
                "⚠️ reservation_extend — channel=%s • user=%s • clan=%s • result=error • reason=multiple_rows • source=global"
                % (
                    channel_label(ctx.guild, getattr(ctx.channel, "id", None)),
                    display_member,
                    sheet_tag,
                ),
            )
            return

        target = filtered[0]
        try:
            await reservations.update_reservation_expiry(target.row_number, new_date)
        except Exception:
            log.exception(
                "failed to update reservation expiry globally",
                extra={
                    "row": target.row_number,
                    "clan_tag": normalized_tag,
                    "channel": _thread_name(ctx.channel),
                    "channel_id": getattr(ctx.channel, "id", None),
                },
            )
            await ctx.reply(
                "Something went wrong while extending the reservation. Please try again later.",
                mention_author=False,
            )
            human_log.human(
                "error",
                "⚠️ reservation_extend — channel=%s • user=%s • clan=%s • result=error • reason=update_failed • source=global"
                % (
                    channel_label(ctx.guild, getattr(ctx.channel, "id", None)),
                    display_member,
                    sheet_tag,
                ),
            )
            return

        await ctx.send(
            f"Extended the reservation in `{sheet_tag}` for {display_member} until `{new_date.isoformat()}`."
        )

        human_log.human(
            "info",
            "🧭 reservation_extend — channel=%s • user=%s • clan=%s • old=%s • new=%s • result=ok • source=global"
            % (
                channel_label(ctx.guild, getattr(ctx.channel, "id", None)),
                display_member,
                sheet_tag,
                _reservation_display_date(target.reserved_until),
                new_date.isoformat(),
            ),
        )

    @tier("staff")
    @help_metadata(
        function_group="recruitment",
        section="recruitment",
        access_tier="staff",
    )
    @commands.command(
        name="reservations",
        help="List active reservations for a recruit or clan.",
        brief="Show active reservation details.",
    )
    async def reservations_command(
        self, ctx: commands.Context, clan_tag: str | None = None
    ) -> None:
        if not _reservations_enabled():
            await ctx.reply(
                "Reservations are currently disabled. Please poke an admin if you think this is wrong.",
                mention_author=False,
            )
            return

        if ctx.guild is None:
            await ctx.reply(
                "Only Recruiters (or Admins) can inspect reservation details.",
                mention_author=False,
            )
            return

        actor_is_staff = _is_authorized(ctx)
        actor_is_lead = _is_clan_lead_user(ctx.author)
        if not (actor_is_staff or actor_is_lead):
            await ctx.reply(
                "Only Recruiters (or Admins) can inspect reservation details.",
                mention_author=False,
            )
            return

        channel_id = getattr(ctx.channel, "id", None)
        control_thread_id = get_recruiters_thread_id()
        if clan_tag is None or not str(clan_tag).strip():
            if not actor_is_staff:
                await ctx.reply(
                    "Ticket-level reservation lookups are limited to Recruiters and Admins.",
                    mention_author=False,
                )
                return
            if control_thread_id and control_thread_id == channel_id:
                await self._handle_global_reservations(ctx)
                return
            await self._handle_thread_reservations(ctx)
            return

        interact_channel_id = get_recruitment_interact_channel_id()
        if interact_channel_id is None or interact_channel_id != channel_id:
            await ctx.reply(
                f"Clan-level reservation lookups are only available in {_interact_channel_hint()}.",
                mention_author=False,
            )
            return

        if not (actor_is_staff or actor_is_lead):
            await ctx.reply(
                "Only Recruiters/Admins or the configured clan leads can run this command here.",
                mention_author=False,
            )
            return

        await self._handle_clan_reservations(ctx, clan_tag)

    async def _handle_global_reservations(self, ctx: commands.Context) -> None:
        try:
            ledger = await reservations.load_reservation_ledger()
        except Exception:
            log.exception("failed to load ledger for global reservations")
            await ctx.reply(
                "Something went wrong while loading reservations. Please try again later.",
                mention_author=False,
            )
            return

        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=28)

        def _timestamp(row: reservations.ReservationRow) -> dt.datetime | None:
            stamp = row.created_at
            if stamp is not None and stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=dt.timezone.utc)
            if stamp is None and row.reserved_until is not None:
                stamp = dt.datetime.combine(
                    row.reserved_until, dt.time.min, dt.timezone.utc
                )
            return stamp

        recent: list[tuple[dt.datetime, reservations.ReservationRow]] = []
        for row in ledger.rows:
            stamp = _timestamp(row)
            if stamp is None or stamp < cutoff:
                continue
            recent.append((stamp, row))

        if not recent:
            await ctx.send(
                "No reservations have been created or updated in the last 28 days."
            )
            human_log.human(
                "info",
                "🧭 reservations_global — channel=%s • count=0 • window=28d • result=empty"
                % channel_label(ctx.guild, getattr(ctx.channel, "id", None)),
            )
            return

        recent.sort(key=lambda item: (item[0], item[1].row_number), reverse=True)

        thread_cache: dict[str, tuple[str, str]] = {}

        async def _ticket_context(row: reservations.ReservationRow) -> tuple[str, str]:
            thread_id = str(row.thread_id or "")
            cached = thread_cache.get(thread_id)
            if cached is not None:
                return cached
            ticket = "unknown"
            thread_label = "unknown thread"
            thread = await _resolve_thread(self.bot, row.thread_id)
            if thread is not None:
                thread_label = _thread_name(thread)
                parts = parse_welcome_thread_name(getattr(thread, "name", None))
                if parts and parts.ticket_code:
                    ticket = parts.ticket_code
            thread_cache[thread_id] = (ticket, thread_label)
            return ticket, thread_label

        entries: list[str] = []
        for _, row in recent:
            user_display = _reservation_user_display(
                ctx.guild,
                row,
                fallback_username=row.username_snapshot or "unknown recruit",
            )
            tag = row.normalized_clan_tag or (row.clan_tag or "?")
            status_text = _reservation_status_display(row.status)
            expiry = _reservation_display_date(row.reserved_until)
            ticket_code, thread_label = await _ticket_context(row)
            entries.append(
                "• %s — `%s` — %s — expires %s — ticket %s (%s)"
                % (user_display, tag, status_text, expiry, ticket_code, thread_label)
            )

        header = f"Reservations in the last 28 days ({len(entries)} rows):"
        chunk_size = 10
        for index in range(0, len(entries), chunk_size):
            chunk = entries[index : index + chunk_size]
            prefix = header if index == 0 else "…continued:"
            await ctx.send("\n".join([prefix, *chunk]))

        human_log.human(
            "info",
            "🧭 reservations_global — channel=%s • count=%d • window=28d • result=ok"
            % (
                channel_label(ctx.guild, getattr(ctx.channel, "id", None)),
                len(entries),
            ),
        )

    async def _handle_thread_reservations(self, ctx: commands.Context) -> None:
        if not _is_ticket_thread(ctx.channel):
            await ctx.reply(
                "`!reservations` without a clan tag only works inside a recruit’s ticket thread.",
                mention_author=False,
            )
            return

        try:
            view = await _get_thread_reservation(ctx.channel, parsed_username=None)
        except Exception:
            log.exception(
                "failed to look up reservations for thread listing",
                extra={"thread_id": getattr(ctx.channel, "id", None)},
            )
            await ctx.reply(
                "Something went wrong while loading reservations for this recruit. Please try again later.",
                mention_author=False,
            )
            human_log.human(
                "error",
                "⚠️ reservations_list — thread=%s • result=error • reason=lookup_failed"
                % _thread_name(ctx.channel),
            )
            return

        if view is None:
            await ctx.reply(
                "I couldn't parse this thread's ticket information. Please check the name and try again.",
                mention_author=False,
            )
            human_log.human(
                "warning",
                "⚠️ reservations_list — thread=%s • result=error • reason=unparsed_thread"
                % _thread_name(ctx.channel),
            )
            return

        control_hint = _control_thread_hint()
        recruit_display = _thread_recruit_display(
            ctx.guild,
            view.matches,
            parsed_username=view.context.username,
        )
        thread_label = _thread_name(view.context.thread)

        if view.status == "ambiguous":
            await ctx.send(
                f"I found multiple active reservations for {recruit_display}. Please review them in {control_hint} or fix the ledger."
            )
            human_log.human(
                "warning",
                "⚠️ reservations_list — ticket=%s • user=%s • thread=%s • result=error • reason=multiple_active"
                % (view.context.ticket_code, view.context.username, thread_label),
            )
            return

        if view.status == "mismatch" and view.reservation is not None:
            ledger_tag = view.reservation.normalized_clan_tag or (
                view.reservation.clan_tag or "unknown"
            )
            thread_tag = view.thread_tag_display or "unknown"
            await ctx.send(
                (
                    f"This thread is labeled for `{thread_tag}`, but the ledger shows `{ledger_tag}`.\n"
                    f"Please confirm the correct clan in {control_hint} before making changes."
                )
            )
            human_log.human(
                "warning",
                "⚠️ reservations_list — ticket=%s • user=%s • thread=%s • result=error • reason=tag_mismatch"
                % (view.context.ticket_code, view.context.username, thread_label),
            )
            return

        if view.status == "none":
            await ctx.send(f"No active reservations found for {recruit_display}.")
            human_log.human(
                "info",
                "🧭 reservations_list — ticket=%s • user=%s • thread=%s • count=0 • scope=user • result=empty"
                % (view.context.ticket_code, view.context.username, thread_label),
            )
            return

        reservation = view.reservation
        if reservation is None:
            await ctx.send(f"No active reservations found for {recruit_display}.")
            human_log.human(
                "warning",
                "⚠️ reservations_list — ticket=%s • user=%s • thread=%s • result=error • reason=missing_row"
                % (view.context.ticket_code, view.context.username, thread_label),
            )
            return

        tag = reservation.normalized_clan_tag or (reservation.clan_tag or "?")
        creator = _reservation_creator_display(ctx.guild, reservation.recruiter_id)
        expiry = _reservation_display_date(reservation.reserved_until)
        status_text = _reservation_status_display(reservation.status)
        lines = [
            f"Active reservation for {recruit_display}:",
            f"• `{tag}` — expires {expiry} (created by {creator} • {status_text})",
        ]
        await ctx.send("\n".join(lines))

        human_log.human(
            "info",
            "🧭 reservations_list — ticket=%s • user=%s • thread=%s • count=1 • scope=user • result=ok"
            % (view.context.ticket_code, view.context.username, thread_label),
        )

    async def _handle_clan_reservations(
        self, ctx: commands.Context, clan_tag: str
    ) -> None:
        try:
            sheet_tag = await _aresolve_configured_reservation_clan_tag(clan_tag)
        except ValueError:
            await ctx.reply(
                f"I don’t know the clan tag `{clan_tag}`. Please check the tag and try again.",
                mention_author=False,
            )
            human_log.human(
                "warning",
                "⚠️ reservations_list — clan=%s • count=0 • scope=clan • result=error • reason=unknown_clan"
                % clan_tag,
            )
            return

        try:
            rows = await reservations.get_active_reservations_for_clan(sheet_tag)
        except Exception:
            log.exception(
                "failed to fetch clan reservations",
                extra={"clan_tag": _normalize_tag(sheet_tag)},
            )
            await ctx.reply(
                "Something went wrong while loading reservations for that clan. Please try again later.",
                mention_author=False,
            )
            human_log.human(
                "error",
                "⚠️ reservations_list — clan=%s • count=0 • scope=clan • result=error • reason=lookup_failed"
                % sheet_tag,
            )
            return

        rows = [row for row in rows if row.is_active]
        rows.sort(key=_reservation_sort_key)

        if not rows:
            await ctx.send(f"No active reservations for `{sheet_tag}`.")
            human_log.human(
                "info",
                "🧭 reservations_list — clan=%s • count=0 • scope=clan • result=empty"
                % sheet_tag,
            )
            return

        lines = [f"Active reservations for `{sheet_tag}`:"]
        for row in rows:
            thread = await _resolve_thread(self.bot, row.thread_id)
            ticket_code = None
            username = row.username_snapshot or "unknown recruit"
            if thread is not None:
                parts = parse_welcome_thread_name(getattr(thread, "name", None))
                if parts is not None:
                    ticket_code = parts.ticket_code
                    if parts.username:
                        username = parts.username
            user_display = _reservation_user_display(
                ctx.guild, row, fallback_username=username
            )
            expiry = _reservation_display_date(row.reserved_until)
            ticket_label = ticket_code or "unknown"
            lines.append(f"• {user_display} — expires {expiry} (ticket {ticket_label})")

        await ctx.send("\n".join(lines))

        human_log.human(
            "info",
            "🧭 reservations_list — clan=%s • count=%d • scope=clan • result=ok"
            % (sheet_tag, len(rows)),
        )


async def _resolve_thread(
    bot: commands.Bot, thread_id: str | int | None
) -> Optional[object]:
    if thread_id is None:
        return None
    try:
        numeric_id = int(thread_id)
    except (TypeError, ValueError):
        return None

    getter = getattr(bot, "get_channel", None)
    if callable(getter):
        try:
            channel = getter(numeric_id)
        except Exception:  # pragma: no cover - cache lookup best effort
            channel = None
        if isinstance(channel, discord.Thread) or _is_ticket_thread(channel):
            return channel

    thread_getter = getattr(bot, "get_thread", None)
    if callable(thread_getter):
        try:
            channel = thread_getter(numeric_id)
        except Exception:  # pragma: no cover - cache lookup best effort
            channel = None
        if isinstance(channel, discord.Thread) or _is_ticket_thread(channel):
            return channel

    fetcher = getattr(bot, "fetch_channel", None)
    if callable(fetcher):
        try:
            channel = await fetcher(numeric_id)
        except Exception:
            channel = None
        if isinstance(channel, discord.Thread) or _is_ticket_thread(channel):
            return channel

    return None


def _safe_cell(row: list[str], index: int, fallback: int) -> str:
    if 0 <= index < len(row):
        value = row[index]
        text = str(value).strip()
        if text:
            return text
    return str(fallback)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReservationCog(bot))
    log.info("placement.reservations command loaded")


__all__ = ["ReservationCog", "setup"]
