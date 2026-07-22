"""Recruitment welcome command helpers ported from Matchmaker."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Optional

import discord
from discord.abc import Messageable
from discord.ext import commands

from modules.common import runtime as rt
from shared.dedupe import EventDeduper
from shared.logfmt import LogTemplates, channel_label, human_reason, user_label
from shared.obs.events import (
    format_refresh_message,
    refresh_bucket_results,
    refresh_dedupe_key,
    refresh_deduper,
)
from modules.recruitment import emoji_pipeline
from shared.cache import telemetry as cache_telemetry
from shared.config import (
    get_refresh_timezone,
    get_welcome_general_channel_id,
)
from shared.sheets import async_facade as sheets

try:  # Python 3.9+ optional zoneinfo
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - fallback on narrow runtimes
    ZoneInfo = None  # type: ignore[assignment]


_EMOJI_TOKEN = re.compile(r"{EMOJI:([^}]+)}")
_ROLE_LINE_RE = re.compile(r"(Clan\s*Lead|Deput(?:y|ies))\s*[:：]\s*(.*)$", re.IGNORECASE)
_EMPTY_ROLE_VALUES = {"", "-", "—", "n/a", "na", "none", "notfound", "not found"}
_DEFAULT_GENERAL_NOTICE = (
    "A new flame joins the cult — welcome {MENTION} to {CLAN}!\n"
    "Be loud, be nerdy, and maybe even helpful. You know the drill, C1C."
)

_WELCOME_DEDUPER = EventDeduper()

log = logging.getLogger(__name__)


class _WelcomeSummary:
    def __init__(
        self,
        *,
        guild: discord.Guild,
        tag: str,
        recruit: discord.Member | discord.User | None,
        channel: discord.abc.GuildChannel | discord.Thread | None,
    ) -> None:
        self.guild = guild
        self.tag = tag
        self.recruit = recruit
        self.channel = channel
        self._actions: dict[str, tuple[str, Optional[str]]] = {}

    def record(self, action: str, status: str, detail: Optional[str] = None) -> None:
        self._actions[action] = (status, detail)

    async def emit(self) -> None:
        recruit_id = getattr(self.recruit, "id", None)
        key = f"welcome:{self.tag}:{recruit_id or 0}"
        if not _WELCOME_DEDUPER.should_emit(key):
            return
        recruit_label = user_label(self.guild, recruit_id)
        channel_id = getattr(self.channel, "id", None)
        channel_text = channel_label(self.guild, channel_id)
        details: list[str] = []
        has_error = False
        has_ok = False
        for action, (status, detail) in self._actions.items():
            if status == "error":
                has_error = True
                text = f"{action}=error"
                if detail:
                    text = f"{text} ({detail})"
                details.append(text)
            elif status == "ok":
                has_ok = True
        if not self._actions:
            has_error = True
            details.append("no_actions")
        result = "ok"
        if has_error and has_ok:
            result = "partial"
        elif has_error and not has_ok:
            result = "error"
        message = LogTemplates.welcome(
            tag=self.tag,
            recruit=recruit_label,
            channel=channel_text,
            result=result,
            details=details,
        )
        try:
            await rt.send_log_message(message)
        except Exception:  # pragma: no cover - log delivery failures shouldn't raise
            log.exception(
                "failed to emit welcome summary", extra={"tag": self.tag, "recruit": recruit_id}
            )


@dataclass(frozen=True)
class WelcomeTemplate:
    """Normalized welcome template row."""

    tag: str
    title: str
    body: str
    footer: str
    target_channel_id: Optional[int]
    crest_url: str
    ping_user: bool
    active: bool
    clan: str
    clanlead: str
    deputies: str
    general_notice: str
    notes: str

    raw: Mapping[str, Any]

    def merged_with(self, default: "WelcomeTemplate | None") -> "WelcomeTemplate":
        """Return a copy where blank TITLE/BODY/FOOTER fall back to *default*."""

        if default is None:
            return self
        if self.tag == default.tag:
            return self
        if self.title.strip() and self.body.strip() and self.footer.strip():
            return self
        return WelcomeTemplate(
            tag=self.tag,
            title=self.title.strip() or default.title,
            body=self.body.strip() or default.body,
            footer=self.footer.strip() or default.footer,
            target_channel_id=self.target_channel_id,
            crest_url=self.crest_url,
            ping_user=self.ping_user,
            active=self.active,
            clan=self.clan,
            clanlead=self.clanlead,
            deputies=self.deputies,
            general_notice=self.general_notice or default.general_notice,
            notes=self.notes,
            raw=self.raw,
        )


def _parse_bool(value: Any) -> bool:
    return str(value or "").strip().upper() in {"Y", "YES", "TRUE", "1"}


def _parse_int(value: Any) -> Optional[int]:
    text = str(value or "").strip()
    return int(text) if text.isdigit() else None


def _normalize_tag(raw: Any) -> str:
    return str(raw or "").strip().upper()


def _build_template(row: Mapping[str, Any]) -> Optional[WelcomeTemplate]:
    tag = (
        _normalize_tag(row.get("TAG"))
        or _normalize_tag(row.get("ClanTag"))
        or _normalize_tag(row.get("clan"))
    )
    if not tag:
        return None

    target_id = _parse_int(row.get("TARGET_CHANNEL_ID"))
    return WelcomeTemplate(
        tag=tag,
        title=str(row.get("TITLE", "") or ""),
        body=str(row.get("BODY", "") or ""),
        footer=str(row.get("FOOTER", "") or ""),
        target_channel_id=target_id,
        crest_url=str(row.get("CREST_URL", "") or ""),
        ping_user=_parse_bool(row.get("PING_USER")),
        active=_parse_bool(row.get("ACTIVE", "Y")),
        clan=str(row.get("CLAN", "") or ""),
        clanlead=str(row.get("CLANLEAD", "") or ""),
        deputies=str(row.get("DEPUTIES", "") or ""),
        general_notice=str(row.get("GENERAL_NOTICE", "") or ""),
        notes=str(row.get("NOTES", "") or ""),
        raw=row,
    )


async def _load_templates() -> tuple[dict[str, WelcomeTemplate], Optional[WelcomeTemplate]]:
    rows = await sheets.get_cached_welcome_templates()
    templates: dict[str, WelcomeTemplate] = {}
    default_row: WelcomeTemplate | None = None
    for row in rows or []:
        template = _build_template(row)
        if template is None:
            continue
        key = template.tag
        if key == "C1C":
            default_row = template
            continue
        if key == "DEFAULT":
            continue
        templates[key] = template

    return templates, default_row


def _timezone() -> timezone:
    tz_name = get_refresh_timezone()
    if ZoneInfo is not None:
        try:
            return ZoneInfo(tz_name)
        except Exception:  # pragma: no cover - defensive guard
            pass
    return timezone.utc


def _format_now() -> str:
    tz = _timezone()
    return datetime.now(tz).strftime("%a, %d %b %Y %H:%M %Z")


def _replace_emoji_tokens(text: str, guild: discord.Guild | None) -> str:
    if not text:
        return ""

    def repl(match: re.Match[str]) -> str:
        token = (match.group(1) or "").strip()
        if not token:
            return ""
        if token.isdigit():
            emoji_id = int(token)
            if guild:
                found = discord.utils.get(getattr(guild, "emojis", []), id=emoji_id)
                if found:
                    return str(found)
            return f"<:emoji:{emoji_id}>"
        emoji = emoji_pipeline.emoji_for_tag(guild, token)
        return str(emoji) if emoji else token

    return _EMOJI_TOKEN.sub(repl, text)


def _strip_empty_role_lines(text: str) -> str:
    lines = (text or "").splitlines()
    kept: list[str] = []
    kept_role = False

    def _normalize(value: str) -> str:
        return value.replace("\u00A0", " ").strip()

    def _strip_md(value: str) -> str:
        return re.sub(r"[`*_~]", "", value)

    for line in lines:
        raw = _normalize(line)
        plain = _strip_md(raw)
        match = _ROLE_LINE_RE.search(plain)
        if match:
            tail = match.group(2).strip().lower()
            if tail in _EMPTY_ROLE_VALUES:
                continue
            kept_role = True
        kept.append(line)

    if not kept_role:
        cleaned: list[str] = []
        i = 0
        while i < len(kept):
            raw = _normalize(kept[i])
            plain = _strip_md(raw)
            if re.search(r"\byour\b.*\bcrew\s*[:：]\s*$", plain, re.IGNORECASE):
                i += 1
                if i < len(kept) and not _normalize(kept[i]):
                    i += 1
                continue
            cleaned.append(kept[i])
            i += 1
        kept = cleaned

    text = "\n".join(kept)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip("\n")


def _expand_tokens(
    text: str,
    *,
    guild: discord.Guild | None,
    template: WelcomeTemplate,
    tag: str,
    inviter: discord.Member | discord.User | None,
    target: discord.Member | discord.User | None,
) -> str:
    if not text:
        return ""
    clan_name = template.clan or tag
    parts = {
        "{MENTION}": target.mention if target else "",
        "{USERNAME}": target.display_name if target else "",
        "{CLAN}": clan_name,
        "{CLANTAG}": tag,
        "{GUILD}": guild.name if guild else "",
        "{NOW}": _format_now(),
        "{INVITER}": inviter.display_name if inviter else "",
        "{CLANLEAD}": template.clanlead,
        "{DEPUTIES}": template.deputies,
    }
    for key, value in parts.items():
        text = text.replace(key, value or "")
    return _strip_empty_role_lines(_replace_emoji_tokens(text, guild))


def _build_welcome_embed(
    template: WelcomeTemplate,
    *,
    guild: discord.Guild,
    tag: str,
    inviter: discord.Member | discord.User | None,
    target: discord.Member | discord.User | None,
    extra_body: str = "",
    include_crest: bool = False,
) -> discord.Embed:
    """Build one welcome embed using only the supplied template row."""

    title = _expand_tokens(
        template.title,
        guild=guild,
        template=template,
        tag=tag,
        inviter=inviter,
        target=target,
    )
    body = _expand_tokens(
        template.body,
        guild=guild,
        template=template,
        tag=tag,
        inviter=inviter,
        target=target,
    )
    footer = _expand_tokens(
        template.footer,
        guild=guild,
        template=template,
        tag=tag,
        inviter=inviter,
        target=target,
    )
    description = f"{body}\n\n{extra_body}".strip() if extra_body else body
    embed = discord.Embed(title=title or None, description=description, colour=discord.Colour.blue())
    embed.timestamp = datetime.now(timezone.utc)
    if footer:
        embed.set_footer(text=footer)
    if include_crest and template.crest_url.strip():
        embed.set_thumbnail(url=template.crest_url.strip())
    return embed


async def _log(level: str, **kv: Any) -> None:
    payload = " ".join(f"{key}={value}" for key, value in kv.items() if value is not None)
    message = f"[welcome/{level}] {payload}" if payload else f"[welcome/{level}]"
    if level == "error":
        log.error(message)
    elif level == "warn":
        log.warning(message)
    else:
        log.info(message)


def _extract_note_text(ctx: commands.Context, tail: str) -> str:
    note = tail or ""
    if not note:
        return ""
    message = getattr(ctx, "message", None)
    mentions: Iterable[Any] = getattr(message, "mentions", []) or []
    for member in mentions:
        for pattern in (f"<@{member.id}>", f"<@!{member.id}>"):
            if pattern in note:
                note = note.replace(pattern, "").strip()
    return note.strip()


async def _resolve_target_member(ctx: commands.Context) -> Optional[discord.Member]:
    message = getattr(ctx, "message", None)
    if message is None:
        return None
    mentions = getattr(message, "mentions", None)
    if mentions:
        return mentions[0]
    reference = getattr(message, "reference", None)
    if reference and reference.resolved is not None:
        resolved = reference.resolved
        author = getattr(resolved, "author", None)
        if author is not None:
            try:
                return await ctx.guild.fetch_member(author.id)
            except Exception:  # pragma: no cover - Discord lookup failure
                return None
    return None


async def _delete_message(message: discord.Message) -> None:
    try:
        await asyncio.sleep(2)
        await message.delete()
    except Exception:
        await _log("warn", cause="cleanup_failed", message_id=getattr(message, "id", None))


class WelcomeCommandService:
    """High-level coordinator for the recruitment welcome command."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def post_welcome(
        self,
        ctx: commands.Context,
        clan: Optional[str],
        *,
        tail: Optional[str] = None,
    ) -> None:
        if ctx.guild is None:
            await ctx.reply("This command only works inside a guild.")
            return

        tag = _normalize_tag(clan)
        if not tag:
            await ctx.reply("Please provide a clan tag to welcome.")
            return

        summary: Optional[_WelcomeSummary] = None

        try:
            templates, default_row = await _load_templates()
        except Exception as exc:
            await _log("error", actor=getattr(ctx.author, "id", None), tag=tag, error=repr(exc))
            await ctx.reply("⚠️ Failed to load welcome templates. Try again after the next refresh.")
            return

        template = templates.get(tag)
        if template is None:
            await _log("error", actor=getattr(ctx.author, "id", None), tag=tag, cause="missing_row")
            await ctx.reply(f"I can't find a configured welcome for **{tag}**. Add it in the sheet.")
            return

        if not template.active:
            await _log("error", actor=getattr(ctx.author, "id", None), tag=tag, cause="inactive")
            await ctx.reply(
                f"The welcome template for **{tag}** is inactive. Flip ACTIVE to Y in WelcomeTemplates."
            )
            return

        if default_row is None or not default_row.active:
            cause = "missing_cluster_row" if default_row is None else "inactive_cluster_row"
            await _log("error", actor=getattr(ctx.author, "id", None), tag=tag, cause=cause)
            error = discord.Embed(
                title="Shared C1C welcome template unavailable",
                description=(
                    "The active **C1C** row in WelcomeTemplates is missing or unavailable. "
                    "No welcome was posted."
                ),
                colour=discord.Colour.red(),
            )
            await ctx.reply(embed=error)
            return

        effective = template
        raw_channel_value = str(template.raw.get("TARGET_CHANNEL_ID", "") or "").strip()
        if raw_channel_value and not effective.target_channel_id:
            await _log(
                "error",
                actor=getattr(ctx.author, "id", None),
                tag=tag,
                cause="invalid_channel",
                raw=raw_channel_value,
            )
            await ctx.reply(f"No target channel configured for **{tag}**.")
            return

        channel: Messageable
        if effective.target_channel_id:
            try:
                channel = (
                    ctx.guild.get_channel(effective.target_channel_id)
                    or await self.bot.fetch_channel(effective.target_channel_id)
                )
            except Exception:
                await _log(
                    "error",
                    actor=getattr(ctx.author, "id", None),
                    tag=tag,
                    cause="channel_unavailable",
                    channel_id=effective.target_channel_id,
                )
                await ctx.reply("Couldn't access the clan channel.")
                return
        else:
            channel = ctx.channel

        target_member = await _resolve_target_member(ctx)
        note_text = _extract_note_text(ctx, tail or "")

        summary_channel: discord.abc.GuildChannel | discord.Thread | None = None
        if isinstance(channel, (discord.abc.GuildChannel, discord.Thread)):
            summary_channel = channel
        elif isinstance(getattr(channel, "channel", None), (discord.abc.GuildChannel, discord.Thread)):
            summary_channel = getattr(channel, "channel", None)
        summary = _WelcomeSummary(
            guild=ctx.guild,
            tag=tag,
            recruit=target_member,
            channel=summary_channel,
        )

        cluster_embed = _build_welcome_embed(
            default_row,
            guild=ctx.guild,
            tag=default_row.tag,
            inviter=getattr(ctx, "author", None),
            target=target_member,
        )
        clan_embed = _build_welcome_embed(
            effective,
            guild=ctx.guild,
            tag=tag,
            inviter=getattr(ctx, "author", None),
            target=target_member,
            extra_body=note_text,
            include_crest=True,
        )

        if not (cluster_embed.description or "").strip():
            await _log("error", actor=getattr(ctx.author, "id", None), tag=tag, cause="empty_body")
            error = discord.Embed(
                title="Shared C1C welcome template unavailable",
                description="The **C1C** row has no usable welcome text. No welcome was posted.",
                colour=discord.Colour.red(),
            )
            await ctx.reply(embed=error)
            if summary is not None:
                summary.record("command", "error", "empty_cluster_body")
                await summary.emit()
                summary = None
            return

        if not (clan_embed.description or "").strip():
            await _log("error", actor=getattr(ctx.author, "id", None), tag=tag, cause="empty_body")
            await ctx.reply(f"Missing welcome text for **{tag}**. Please check WelcomeTemplates.")
            if summary is not None:
                summary.record("command", "error", "empty_body")
                await summary.emit()
                summary = None
            return

        ping_content = target_member.mention if (target_member and effective.ping_user) else None
        try:
            await channel.send(
                content=ping_content,
                embeds=[cluster_embed, clan_embed],
                allowed_mentions=None,
            )
        except Exception as exc:
            await _log(
                "error",
                actor=getattr(ctx.author, "id", None),
                tag=tag,
                cause="send_failed",
                channel_id=getattr(channel, "id", None),
                error=repr(exc),
            )
            if summary is not None:
                summary.record("clan_channel", "error", human_reason(exc))
                await summary.emit()
                summary = None
            await ctx.reply("Couldn't post the welcome in the clan channel.")
            return

        if summary is not None:
            summary.record("clan_channel", "ok")

        notice_status, notice_detail = await self._post_general_notice(
            guild=ctx.guild,
            text=effective.general_notice or (default_row.general_notice if default_row else ""),
            target=target_member,
            tag=tag,
            template=effective,
        )
        if summary is not None:
            summary.record("general_notice", notice_status, notice_detail)

        try:
            await _log(
                "info",
                actor=getattr(ctx.author, "id", None),
                tag=tag,
                channel_id=getattr(channel, "id", None),
                recruit=getattr(target_member, "id", None),
                note=bool(note_text),
                result="success",
            )
        finally:
            message = getattr(ctx, "message", None)
            if message is not None and hasattr(message, "delete"):
                asyncio.create_task(_delete_message(message))

        if note_text and summary is not None:
            summary.record("note", "ok")

        if summary is not None:
            await summary.emit()

    async def refresh_templates(self, ctx: commands.Context) -> None:
        actor = getattr(ctx.author, "mention", None) or getattr(ctx.author, "id", None)
        result = await cache_telemetry.refresh_now("templates", actor=str(actor))
        bucket_results = refresh_bucket_results([result])
        deduper = refresh_deduper()
        bucket_name = getattr(result, "name", "templates") or "templates"
        key = refresh_dedupe_key("templates", None, [bucket_name])
        duration_ms = getattr(result, "duration_ms", None)
        total_s = None
        if duration_ms is not None:
            total_s = (duration_ms or 0) / 1000.0
        if deduper.should_emit(key):
            await rt.send_log_message(
                format_refresh_message(
                    "templates",
                    bucket_results,
                    total_s=total_s,
                )
            )
        if result.ok:
            await ctx.reply("Welcome templates reloaded. ✅")
        else:
            error = result.error or "unknown error"
            await ctx.reply(f"Reload failed: `{error}`")

    async def _post_general_notice(
        self,
        *,
        guild: discord.Guild,
        text: str,
        target: discord.Member | discord.User | None,
        tag: str,
        template: WelcomeTemplate,
    ) -> tuple[str, Optional[str]]:
        channel_id = get_welcome_general_channel_id()
        if not channel_id:
            await _log("info", tag=tag, cause="general_notice_skipped", reason="channel_unconfigured")
            return "skip", "channel_unconfigured"
        try:
            channel = guild.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        except Exception as exc:
            await _log(
                "warn",
                tag=tag,
                cause="general_notice_channel_error",
                channel_id=channel_id,
                error=repr(exc),
            )
            return "error", human_reason(exc)

        notice_text = text or template.general_notice or _DEFAULT_GENERAL_NOTICE
        expanded = _expand_tokens(
            notice_text,
            guild=guild,
            template=template,
            tag=tag,
            inviter=None,
            target=target,
        )
        try:
            await channel.send(expanded)
        except Exception as exc:
            await _log(
                "warn",
                tag=tag,
                cause="general_notice_failed",
                channel_id=channel_id,
                error=repr(exc),
            )
            return "error", human_reason(exc)

        return "ok", None


__all__ = ["WelcomeCommandService"]
