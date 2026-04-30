from __future__ import annotations

import logging
import os
from datetime import datetime, time, timezone
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

import discord
from discord.ext import tasks

from modules.common import feature_flags
from modules.common import runtime as runtime_helpers
from shared.config import (
    get_recruiter_role_ids,
    get_recruitment_sheet_id,
)
from shared.logfmt import LogTemplates, channel_label, guild_label, human_reason, user_label
from shared.sheets.recruitment import (
    afetch_reports_tab,
    get_reports_tab_name,
)
from modules.recruitment.reporting.destinations import get_report_destination_id
from modules.recruitment.reporting.open_ticket_report import (
    send_currently_open_tickets_report,
)
from modules.housekeeping.role_audit import run_role_and_visitor_audit
from modules.housekeeping.role_audit import resolve_audit_destination

log = logging.getLogger("c1c.recruitment.reporting.daily")

UTC = timezone.utc

_BOT_REFERENCE: Optional[discord.Client] = None


def feature_enabled() -> bool:
    """Return True when the recruitment_reports feature toggle is enabled."""

    try:
        return feature_flags.is_enabled("recruitment_reports")
    except Exception:
        log.debug("feature toggle lookup failed", exc_info=True)
        return False


def _parse_utc_time(value: str) -> time:
    text = (value or "").strip()
    if not text:
        raise ValueError("time string is empty")
    hour, minute = text.split(":", 1)
    return time(hour=int(hour), minute=int(minute), tzinfo=UTC)


def _scheduled_time() -> time:
    raw = os.getenv("REPORT_DAILY_POST_TIME", "09:30")
    try:
        return _parse_utc_time(raw)
    except Exception:
        log.warning(
            "invalid REPORT_DAILY_POST_TIME %r; falling back to 09:30", raw, exc_info=True
        )
        return time(hour=9, minute=30, tzinfo=UTC)


def _role_mentions() -> Sequence[str]:
    try:
        role_ids = sorted(get_recruiter_role_ids())
    except Exception:
        log.debug("failed to resolve recruiter role IDs", exc_info=True)
        return ()
    return tuple(f"<@&{role_id}>" for role_id in role_ids)


HeadersMap = Dict[str, int]


def _normalize_header(label: str) -> str:
    return " ".join((label or "").strip().lower().replace("_", " ").split())


def _headers_map(row: Sequence[str]) -> HeadersMap:
    mapping: HeadersMap = {}
    for index, cell in enumerate(row):
        key = _normalize_header(str(cell or ""))
        if key:
            mapping[key] = index
    return mapping


def _column(row: Sequence[str], index: int | None) -> str:
    if index is None:
        return ""
    return str(row[index]) if 0 <= index < len(row) else ""


def _parse_int(text: str) -> int:
    try:
        return int(str(text).strip())
    except Exception:
        return 0


def _find_row_equals(rows: Sequence[Sequence[str]], column: int, needle: str) -> int:
    target = (needle or "").strip().lower()
    for idx, row in enumerate(rows):
        value = str(row[column] if column < len(row) else "").strip().lower()
        if value == target:
            return idx
    return -1


def _collect_block(
    rows: Sequence[Sequence[str]],
    *,
    start_row: int,
    stop_column: int,
    stop_value: str,
) -> List[Sequence[str]]:
    collected: List[Sequence[str]] = []
    stop_normalized = (stop_value or "").strip().lower()
    for idx in range(start_row + 1, len(rows)):
        row = rows[idx]
        value = str(row[stop_column] if stop_column < len(row) else "").strip().lower()
        if value == stop_normalized:
            break
        collected.append(row)
    return collected


def _collect_bracket_sections(
    rows: Sequence[Sequence[str]],
    *,
    start_row: int,
) -> Tuple[Dict[str, List[Sequence[str]]], Dict[str, Tuple[int, int, int]]]:
    wanted = [
        "elite end game",
        "early end game",
        "late game",
        "mid game",
        "early game",
        "beginners",
    ]
    sections: Dict[str, List[Sequence[str]]] = {label: [] for label in wanted}
    totals: Dict[str, Tuple[int, int, int]] = {label: (0, 0, 0) for label in wanted}
    active: Optional[str] = None
    for idx in range(start_row, len(rows)):
        row = rows[idx]
        group = str(row[1] if len(row) > 1 else "").strip().lower()
        if group in sections:
            active = group
            open_total = _parse_int(row[3] if len(row) > 3 else "0")
            inactive_total = _parse_int(row[4] if len(row) > 4 else "0")
            reserved_total = _parse_int(row[5] if len(row) > 5 else "0")
            totals[group] = (open_total, inactive_total, reserved_total)
            continue
        if active is None:
            continue
        if not any(str(cell).strip() for cell in row):
            active = None
            continue
        sections[active].append(row)
    return sections, totals


def _resolve_index(headers: HeadersMap, name: str) -> Optional[int]:
    normalized = _normalize_header(name)
    return headers.get(normalized)


def _format_line(
    headers: HeadersMap, row: Sequence[str], *, always: bool = False
) -> Optional[str]:
    key_idx = _resolve_index(headers, "key")
    open_idx = _resolve_index(headers, "open spots")
    inactive_idx = _resolve_index(headers, "inactives")
    reserved_idx = _resolve_index(headers, "reserved spots")

    if None in {key_idx, open_idx, inactive_idx, reserved_idx}:
        return None

    label = _column(row, key_idx).strip()
    open_value = _parse_int(_column(row, open_idx))
    inactive_value = _parse_int(_column(row, inactive_idx))
    reserved_value = _parse_int(_column(row, reserved_idx))

    if not always and (open_value, inactive_value, reserved_value) == (0, 0, 0):
        return None
    return (
        f"\U0001F539 **{label}:** open {open_value} "
        f"| inactives {inactive_value} | reserved {reserved_value}"
    )


# --- Embed helpers ---------------------------------------------------------------
def add_fullwidth_field(embed: discord.Embed, *, name: str, value: str) -> None:
    """Always add a full-width (boxed) field."""

    embed.add_field(name=name, value=value, inline=False)


def _add_block_divider(embed: discord.Embed) -> None:
    """Insert the spacer/divider sequence between logical blocks."""

    add_fullwidth_field(embed, name="\n", value="\u25AB\u25AA\u25AB\u25AA\u25AB\u25AA\u25AB")


class ReportSections(NamedTuple):
    general_lines: List[str]
    per_bracket_lines: List[str]
    detail_blocks: List[Tuple[str, List[str]]]

async def _fetch_report_rows() -> Tuple[List[List[str]], HeadersMap]:
    sheet_id = get_recruitment_sheet_id().strip()
    if not sheet_id:
        raise RuntimeError("RECRUITMENT_SHEET_ID is not configured")
    tab_name = get_reports_tab_name("Statistics")
    rows = await afetch_reports_tab(tab_name)
    matrix: List[List[str]] = [list(map(str, row)) for row in rows or []]
    headers = _headers_map(matrix[0]) if matrix else {}
    return matrix, headers


def _extract_report_sections(
    rows: Sequence[Sequence[str]], headers: HeadersMap
) -> ReportSections:
    general_index = _find_row_equals(rows, 0, "general overview")
    per_bracket_index = _find_row_equals(rows, 0, "per bracket")
    details_index = _find_row_equals(rows, 0, "bracket details")

    general_lines: List[str] = []
    if general_index != -1:
        stop_column = 0
        if per_bracket_index != -1:
            stop_value = "per bracket"
        elif details_index != -1:
            stop_value = "bracket details"
        else:
            stop_value = "__stop__"
        block = _collect_block(
            rows,
            start_row=general_index,
            stop_column=stop_column,
            stop_value=stop_value,
        )
        key_index = _resolve_index(headers, "key")
        always_visible = {"overall", "top 10", "top 5"}
        for row in block:
            label = _column(row, key_index).strip().lower() if key_index is not None else ""
            line = _format_line(headers, row, always=label in always_visible)
            if line:
                general_lines.append(line)

    per_bracket_lines: List[str] = []
    if per_bracket_index != -1:
        stop_value = "bracket details" if details_index != -1 else "__stop__"
        block = _collect_block(
            rows,
            start_row=per_bracket_index,
            stop_column=0,
            stop_value=stop_value,
        )
        key_index = _resolve_index(headers, "key")
        for row in block:
            label = _column(row, key_index).strip() if key_index is not None else ""
            if not label:
                continue
            line = _format_line(headers, row, always=True)
            if line:
                per_bracket_lines.append(line)

    details_start = -1
    if details_index != -1:
        details_start = details_index + 1
    elif per_bracket_index != -1:
        details_start = per_bracket_index + 1

    sections: Dict[str, List[Sequence[str]]] = {}
    if details_start > 0 and details_start < len(rows):
        sections, _ = _collect_bracket_sections(rows, start_row=details_start)

    order = [
        "elite end game",
        "early end game",
        "late game",
        "mid game",
        "early game",
        "beginners",
    ]
    detail_blocks: List[Tuple[str, List[str]]] = []
    for key in order:
        entries = sections.get(key) or []
        formatted = [line for row in entries if (line := _format_line(headers, row))]
        if formatted:
            detail_blocks.append((key, formatted))

    return ReportSections(
        general_lines=general_lines,
        per_bracket_lines=per_bracket_lines,
        detail_blocks=detail_blocks,
    )


def _build_summary_embed(sections: ReportSections) -> discord.Embed:
    embed = discord.Embed(
        title="Summary Open Spots",
        colour=discord.Colour.dark_teal(),
    )

    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    embed.set_footer(
        text=(
            "last updated "
            f"{timestamp} • daily snapshot, for most accurate numbers use `!clanmatch`"
        )
    )

    if sections.general_lines:
        add_fullwidth_field(
            embed,
            name="General Overview",
            value="\n".join(sections.general_lines),
        )

    if sections.general_lines and sections.per_bracket_lines:
        _add_block_divider(embed)

    if sections.per_bracket_lines:
        add_fullwidth_field(
            embed,
            name="Per Bracket",
            value="\n".join(sections.per_bracket_lines),
        )

    for field in getattr(embed, "fields", []):
        try:
            field.inline = False
        except Exception:
            pass

    return embed


def _build_details_embed(sections: ReportSections) -> discord.Embed:
    embed = discord.Embed(
        title="Bracket Details",
        colour=discord.Colour.dark_teal(),
    )

    for key, formatted in sections.detail_blocks:
        add_fullwidth_field(
            embed,
            name=key.title(),
            value="\n".join(formatted),
        )

    for field in getattr(embed, "fields", []):
        try:
            field.inline = False
        except Exception:
            pass

    return embed


class OpenSpotsPager(discord.ui.View):
    def __init__(self, sections: ReportSections) -> None:
        super().__init__(timeout=None)
        self.sections = sections
        self.current_page = "summary"

        self.summary_button.disabled = True
        self.details_button.disabled = False

    def _set_page_state(self, page: str) -> None:
        self.current_page = page
        self.summary_button.disabled = page == "summary"
        self.details_button.disabled = page == "details"

    async def set_summary(self, interaction: discord.Interaction) -> None:
        if self.current_page == "summary":
            await interaction.response.defer()
            return
        self._set_page_state("summary")
        embed = _build_summary_embed(self.sections)
        await interaction.response.edit_message(embeds=[embed], view=self)

    async def set_details(self, interaction: discord.Interaction) -> None:
        if self.current_page == "details":
            await interaction.response.defer()
            return
        self._set_page_state("details")
        embed = _build_details_embed(self.sections)
        await interaction.response.edit_message(embeds=[embed], view=self)

    @discord.ui.button(
        label="Summary",
        style=discord.ButtonStyle.secondary,
        custom_id="open_spots_summary",
    )
    async def summary_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.set_summary(interaction)

    @discord.ui.button(
        label="Bracket Details",
        style=discord.ButtonStyle.secondary,
        custom_id="open_spots_details",
    )
    async def details_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await self.set_details(interaction)


def _build_embeds_from_rows(
    rows: Sequence[Sequence[str]], headers: HeadersMap
) -> Tuple[discord.Embed, discord.Embed]:
    sections = _extract_report_sections(rows, headers)
    summary_embed = _build_summary_embed(sections)
    details_embed = _build_details_embed(sections)
    return summary_embed, details_embed


def _report_content(date_text: str) -> str:
    header = f"# Update {date_text}"
    mentions = list(_role_mentions())
    if mentions:
        return "\n".join([header, *mentions])
    return header


def _format_error(exc: BaseException) -> str:
    text = f"{type(exc).__name__}: {exc}".strip()
    return text or type(exc).__name__


async def _log_event(
    *,
    bot: discord.Client,
    actor: str,
    result: str,
    error: str,
    user_id: Optional[int] = None,
    note: Optional[str] = None,
    destination_source: str = "env",
    destination_key: Optional[str] = None,
    destination_id: Optional[int] = None,
) -> None:
    dest_id = destination_id if destination_id is not None else (get_report_destination_id() or 0)
    guild_id: Optional[int] = None
    guild: Optional[discord.Guild] = None
    if dest_id:
        channel = bot.get_channel(dest_id)
        if isinstance(channel, (discord.TextChannel, discord.Thread)) and channel.guild:
            guild_id = channel.guild.id
            guild = channel.guild
    if guild is None and guild_id:
        guild = bot.get_guild(guild_id)
    date_text = datetime.now(UTC).strftime("%Y-%m-%d")
    user_text = user_label(guild, user_id) if user_id is not None else "-"
    guild_text = guild_label(bot, guild_id) if guild_id else "unknown guild"
    dest_text = channel_label(guild, dest_id) if dest_id else "#unknown"
    reason_text = human_reason(error)
    if note:
        reason_text = (
            f"{reason_text}; note={note}" if reason_text and reason_text != "-" else f"note={note}"
        )
    if destination_key:
        reason_text = (
            f"{reason_text}; dest_source={destination_source}; dest_key={destination_key}; dest_id={dest_id}"
            if reason_text and reason_text != "-"
            else f"dest_source={destination_source}; dest_key={destination_key}; dest_id={dest_id}"
        )
    ok = result.lower() == "ok"
    message = LogTemplates.report(
        kind="recruiters",
        actor=actor,
        user=user_text,
        guild=guild_text,
        dest=dest_text,
        date=date_text,
        ok=ok,
        reason=reason_text,
    )
    try:
        await runtime_helpers.send_log_message(message)
    except Exception:
        log.debug("failed to send report log line", exc_info=True)


async def post_daily_recruiter_update(bot: discord.Client) -> Tuple[bool, str]:
    dest_id = get_report_destination_id()
    if not dest_id:
        return False, "dest-missing"

    await bot.wait_until_ready()

    try:
        channel = bot.get_channel(dest_id) or await bot.fetch_channel(dest_id)
    except Exception as exc:
        log.warning("failed to resolve report destination", exc_info=True)
        return False, _format_error(exc)

    if not isinstance(channel, (discord.TextChannel, discord.Thread)):
        return False, "dest-not-found"

    rows: List[List[str]] = []
    headers: HeadersMap = {}
    fetch_error: Optional[str] = None
    try:
        rows, headers = await _fetch_report_rows()
    except Exception as exc:
        fetch_error = _format_error(exc)
        log.warning("failed to fetch recruiter report rows", exc_info=True)

    sections = _extract_report_sections(rows, headers)
    summary_embed = _build_summary_embed(sections)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    content = _report_content(today)
    pager_view = OpenSpotsPager(sections)

    try:
        await channel.send(content=content, embeds=[summary_embed], view=pager_view)
    except Exception as exc:
        log.warning("failed to send recruiter report", exc_info=True)
        return False, _format_error(exc)

    if fetch_error:
        return False, fetch_error
    return True, "-"


async def run_full_recruiter_reports(
    bot: discord.Client, *, actor: str, user_id: Optional[int] = None
) -> Dict[str, Tuple[bool, str]]:
    ok, error = await post_daily_recruiter_update(bot)
    result = "ok" if ok else "fail"
    await _log_event(bot=bot, actor=actor, result=result, error=error, user_id=user_id)

    audit_dest_id, audit_dest_key = resolve_audit_destination()
    audit_dry_run = True
    audit_ok, audit_error = await run_role_and_visitor_audit(
        bot,
        actor=actor,
        dry_run=audit_dry_run,
    )
    audit_result = "ok" if audit_ok else "fail"
    await _log_event(
        bot=bot,
        actor=actor,
        result=audit_result,
        error=audit_error,
        note="role-audit",
        user_id=user_id,
        destination_key=audit_dest_key,
        destination_id=audit_dest_id,
    )

    tickets_ok, tickets_error = await send_currently_open_tickets_report(bot)
    tickets_result = "ok" if tickets_ok else "fail"
    await _log_event(
        bot=bot,
        actor=actor,
        result=tickets_result,
        error=tickets_error,
        note="open-tickets",
        user_id=user_id,
    )

    return {
        "report": (ok, error),
        "audit": (audit_ok, audit_error),
        "open_tickets": (tickets_ok, tickets_error),
    }


@tasks.loop(time=_scheduled_time())
async def scheduler_daily_recruiter_update() -> None:
    bot = _BOT_REFERENCE
    if bot is None:
        return
    await run_full_recruiter_reports(bot, actor="scheduled")


async def ensure_scheduler_started(bot: discord.Client) -> None:
    global _BOT_REFERENCE
    _BOT_REFERENCE = bot

    if not feature_enabled():
        if scheduler_daily_recruiter_update.is_running():
            scheduler_daily_recruiter_update.cancel()
        return

    if not get_report_destination_id():
        if scheduler_daily_recruiter_update.is_running():
            scheduler_daily_recruiter_update.cancel()
        return

    if not scheduler_daily_recruiter_update.is_running():
        scheduler_daily_recruiter_update.start()


async def log_manual_result(
    *,
    bot: discord.Client,
    user_id: int,
    result: str,
    error: str,
    note: Optional[str] = None,
) -> None:
    await _log_event(
        bot=bot,
        actor="manual",
        result=result,
        error=error,
        user_id=user_id,
        note=note,
    )


__all__ = [
    "ensure_scheduler_started",
    "feature_enabled",
    "log_manual_result",
    "OpenSpotsPager",
    "post_daily_recruiter_update",
    "run_full_recruiter_reports",
    "scheduler_daily_recruiter_update",
]
