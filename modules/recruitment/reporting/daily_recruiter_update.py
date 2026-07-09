from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, time, timezone
from typing import Dict, List, NamedTuple, Optional, Sequence, Tuple

import discord
from discord.ext import tasks

from shared.cache import telemetry as cache_telemetry
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

DETAILS_FILTER_FOOTER = (
    "Clans with 0 openings, 0 inactives, and 0 reserved seats are hidden here."
)

_BOT_REFERENCE: Optional[discord.Client] = None
_PERSISTENT_VIEW_REGISTERED = False
_PERSISTENT_VIEW_ATTR = "_c1c_open_spots_pager_registered"

DISCORD_FIELD_VALUE_LIMIT = 1024
DISCORD_FIELD_NAME_LIMIT = 256
DISCORD_EMBED_FIELD_LIMIT = 25
DISCORD_EMBED_TOTAL_LIMIT = 6000
DISCORD_MESSAGE_EMBED_TOTAL_LIMIT = 6000
DISCORD_EMBEDS_PER_MESSAGE_LIMIT = 10


class DailyReportSectionError(RuntimeError):
    """Raised when the summary/bracket section cannot be built or sent safely."""

    def __init__(self, section: str, phase: str, message: str) -> None:
        super().__init__(message)
        self.section = section
        self.phase = phase


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
_REPORT_REQUIRED_HEADERS = (
    "h1 headline",
    "h2 headline",
    "key",
    "open spots",
    "inactives",
    "reserved spots",
)


class ReportFetchContext(NamedTuple):
    config_key: str
    tab_name: str
    sheet_id_source: str
    row_count: int
    first_row: List[str]
    data_source: str
    clans_cache_state: str
    underlying_exception_type: Optional[str] = None


class ReportFetchError(RuntimeError):
    """Raised when the Statistics sheet cannot be fetched reliably."""

    def __init__(self, message: str, *, context: ReportFetchContext) -> None:
        super().__init__(message)
        self.context = context


class ReportSchemaError(ValueError):
    """Raised when fetched Statistics rows do not contain the report schema."""

    def __init__(
        self,
        message: str,
        *,
        required: Sequence[str],
        actual_first_row: Sequence[str],
        context: ReportFetchContext,
        missing: Sequence[str],
    ) -> None:
        super().__init__(message)
        self.required = tuple(required)
        self.actual_first_row = tuple(actual_first_row)
        self.context = context
        self.missing = tuple(missing)


_REPORT_ROWS_CACHE: Optional[List[List[str]]] = None
_REPORT_HEADERS_CACHE: HeadersMap = {}
_REPORT_CONTEXT_CACHE: Optional[ReportFetchContext] = None


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


def _find_row_equals(rows: Sequence[Sequence[str]], column: int | None, needle: str) -> int:
    if column is None:
        return -1
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
    stop_column: int | None,
    stop_value: str,
) -> List[Sequence[str]]:
    collected: List[Sequence[str]] = []
    stop_normalized = (stop_value or "").strip().lower()
    for idx in range(start_row + 1, len(rows)):
        row = rows[idx]
        value = _column(row, stop_column).strip().lower()
        if value == stop_normalized:
            break
        collected.append(row)
    return collected


def _collect_bracket_sections(
    rows: Sequence[Sequence[str]],
    *,
    start_row: int,
    headers: HeadersMap,
) -> List[Tuple[str, List[Sequence[str]]]]:
    """Collect Bracket Details groups in the same order they appear in the sheet.

    The Statistics sheet marks Bracket Details groups in the configured
    ``H2_Headline`` column, then lists clan rows below each heading using the
    configured ``Key`` column. Do not hardcode bracket names or column
    positions here; the sheet layout is allowed to reorder columns as long as
    the existing header row supplies the schema.
    """

    h1_index = _resolve_index(headers, "h1 headline")
    h2_index = _resolve_index(headers, "h2 headline")
    key_index = _resolve_index(headers, "key")
    if h2_index is None or key_index is None:
        return []

    sections: List[Tuple[str, List[Sequence[str]]]] = []
    active_index: int | None = None

    for row in rows[start_row:]:
        if not any(str(cell).strip() for cell in row):
            active_index = None
            continue

        section_label = _column(row, h1_index).strip()
        bracket_label = _column(row, h2_index).strip()
        key_label = _column(row, key_index).strip()

        if section_label and not bracket_label and not key_label:
            active_index = None
            continue

        if bracket_label and not key_label:
            sections.append((bracket_label, []))
            active_index = len(sections) - 1
            continue

        if active_index is None or not key_label:
            continue

        sections[active_index][1].append(row)

    return sections


def _resolve_index(headers: HeadersMap, name: str) -> Optional[int]:
    normalized = _normalize_header(name)
    return headers.get(normalized)


def _clans_cache_state() -> str:
    try:
        snapshot = cache_telemetry.get_snapshot("clans")
    except Exception:
        log.debug("failed to inspect clans cache snapshot", exc_info=True)
        return "unavailable"
    if not snapshot.available:
        return "unavailable"
    result = (snapshot.last_result or "").lower()
    if result and not result.startswith("ok") and result != "retry_ok":
        return f"failed:{snapshot.last_error or snapshot.last_result}"
    if snapshot.ttl_expired:
        return f"stale:{snapshot.age_human or snapshot.age_seconds or '-'}"
    if snapshot.last_refresh_at is not None:
        return f"fresh:{snapshot.age_human or snapshot.age_seconds or '0s'}"
    return "empty"


def _report_fetch_context(
    *,
    tab_name: str,
    rows: Sequence[Sequence[str]] = (),
    data_source: str = "direct",
    underlying_exception_type: Optional[str] = None,
) -> ReportFetchContext:
    return ReportFetchContext(
        config_key="REPORTS_TAB",
        tab_name=tab_name,
        sheet_id_source="RECRUITMENT_SHEET_ID",
        row_count=len(rows),
        first_row=list(rows[0]) if rows else [],
        data_source=data_source,
        clans_cache_state=_clans_cache_state(),
        underlying_exception_type=underlying_exception_type,
    )


def _log_report_diagnostics(
    message: str,
    *,
    context: ReportFetchContext,
    required: Sequence[str] = _REPORT_REQUIRED_HEADERS,
    exc_info: bool = False,
) -> None:
    log.warning(
        "%s; required=%s actual_first_row=%r config_key=%s tab=%r sheet_id_source=%s "
        "row_count=%s data_source=%s clans_cache=%s underlying_exception_type=%s",
        message,
        list(required),
        context.first_row,
        context.config_key,
        context.tab_name,
        context.sheet_id_source,
        context.row_count,
        context.data_source,
        context.clans_cache_state,
        context.underlying_exception_type or "-",
        exc_info=exc_info,
    )


def _has_required_report_headers(headers: HeadersMap) -> bool:
    return all(_resolve_index(headers, name) is not None for name in _REPORT_REQUIRED_HEADERS)


def _require_report_headers(
    headers: HeadersMap, *, context: Optional[ReportFetchContext] = None
) -> None:
    missing = [
        name
        for name in _REPORT_REQUIRED_HEADERS
        if _resolve_index(headers, name) is None
    ]
    if missing:
        ctx = context or _report_fetch_context(tab_name=get_reports_tab_name("Statistics"))
        raise ReportSchemaError(
            f"Statistics report missing required header(s): {', '.join(missing)}",
            required=_REPORT_REQUIRED_HEADERS,
            actual_first_row=ctx.first_row,
            context=ctx,
            missing=missing,
        )


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


def _split_text_losslessly(text: str, *, limit: int = DISCORD_FIELD_VALUE_LIMIT) -> List[str]:
    """Split text into Discord-sized chunks without dropping or rewriting characters."""

    if limit <= 0:
        raise ValueError("limit must be positive")
    value = str(text)
    if not value:
        return [""]
    return [value[index : index + limit] for index in range(0, len(value), limit)]


def _chunk_lines_for_field(lines: Sequence[str], *, limit: int = DISCORD_FIELD_VALUE_LIMIT) -> List[str]:
    """Split lines into field values while preserving every report character."""

    chunks: List[str] = []
    current = ""
    for raw_line in lines:
        line = str(raw_line)
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
            current = ""
        if len(line) <= limit:
            current = line
            continue
        chunks.extend(_split_text_losslessly(line, limit=limit))
    if current:
        chunks.append(current)
    return chunks


def _new_report_embed(
    *,
    title: str,
    footer: Optional[str] = None,
    page: int = 1,
) -> discord.Embed:
    embed_title = title if page == 1 else f"{title} (cont. {page})"
    embed = discord.Embed(title=embed_title, colour=discord.Colour.dark_teal())
    if footer:
        embed.set_footer(text=footer)
    return embed


def _append_field_paginated(
    embeds: List[discord.Embed],
    *,
    title: str,
    footer: Optional[str],
    field_name: str,
    lines: Sequence[str],
    section: str,
) -> None:
    """Append full-width fields, opening new embeds whenever Discord limits require it."""

    chunks = _chunk_lines_for_field(lines)
    if not chunks:
        return
    if not embeds:
        embeds.append(_new_report_embed(title=title, footer=footer))
    for index, chunk in enumerate(chunks):
        name = field_name if index == 0 else f"{field_name} (cont.)"
        if len(name) > DISCORD_FIELD_NAME_LIMIT:
            raise DailyReportSectionError(
                section,
                "pagination",
                f"field name {field_name!r} exceeds Discord field name limit",
            )
        if len(chunk) > DISCORD_FIELD_VALUE_LIMIT:
            raise DailyReportSectionError(
                section,
                "pagination",
                f"field {field_name!r} could not be split within Discord field value limit",
            )
        embed = embeds[-1]
        if (
            len(getattr(embed, "fields", [])) >= DISCORD_EMBED_FIELD_LIMIT
            or len(embed) + len(name) + len(chunk) > DISCORD_EMBED_TOTAL_LIMIT
        ):
            embed = _new_report_embed(title=title, footer=footer, page=len(embeds) + 1)
            embeds.append(embed)
        add_fullwidth_field(embed, name=name, value=chunk)


class ReportSections(NamedTuple):
    general_lines: List[str]
    per_bracket_lines: List[str]
    detail_blocks: List[Tuple[str, List[str]]]

async def _fetch_report_rows() -> Tuple[List[List[str]], HeadersMap]:
    global _REPORT_ROWS_CACHE, _REPORT_HEADERS_CACHE, _REPORT_CONTEXT_CACHE
    sheet_id = get_recruitment_sheet_id().strip()
    if not sheet_id:
        context = _report_fetch_context(
            tab_name=get_reports_tab_name("Statistics"),
            underlying_exception_type="RuntimeError",
        )
        raise ReportFetchError("RECRUITMENT_SHEET_ID is not configured", context=context)
    tab_name = get_reports_tab_name("Statistics")
    try:
        rows = await afetch_reports_tab(tab_name)
    except asyncio.TimeoutError as exc:
        context = _report_fetch_context(
            tab_name=tab_name,
            data_source="direct",
            underlying_exception_type=type(exc).__name__,
        )
        if _REPORT_ROWS_CACHE and _has_required_report_headers(_REPORT_HEADERS_CACHE):
            stale_context = context._replace(
                row_count=len(_REPORT_ROWS_CACHE),
                first_row=list(_REPORT_ROWS_CACHE[0]) if _REPORT_ROWS_CACHE else [],
                data_source="cache",
            )
            log.warning(
                "recruiter Statistics fetch timed out; using last valid cached rows; "
                "tab=%r row_count=%s clans_cache=%s",
                tab_name,
                stale_context.row_count,
                stale_context.clans_cache_state,
            )
            _REPORT_CONTEXT_CACHE = stale_context
            return [list(row) for row in _REPORT_ROWS_CACHE], dict(_REPORT_HEADERS_CACHE)
        raise ReportFetchError(
            "Recruiter report skipped because Google Sheets/cache fetch timed out. "
            "Existing sheet schema was not changed.",
            context=context,
        ) from exc
    except Exception as exc:
        context = _report_fetch_context(
            tab_name=tab_name,
            data_source="direct",
            underlying_exception_type=type(exc).__name__,
        )
        if _REPORT_ROWS_CACHE and _has_required_report_headers(_REPORT_HEADERS_CACHE):
            stale_context = context._replace(
                row_count=len(_REPORT_ROWS_CACHE),
                first_row=list(_REPORT_ROWS_CACHE[0]) if _REPORT_ROWS_CACHE else [],
                data_source="cache",
            )
            log.warning(
                "recruiter Statistics fetch failed; using last valid cached rows; "
                "tab=%r row_count=%s error_type=%s clans_cache=%s",
                tab_name,
                stale_context.row_count,
                type(exc).__name__,
                stale_context.clans_cache_state,
                exc_info=True,
            )
            _REPORT_CONTEXT_CACHE = stale_context
            return [list(row) for row in _REPORT_ROWS_CACHE], dict(_REPORT_HEADERS_CACHE)
        raise ReportFetchError(
            f"Recruiter report skipped because Google Sheets/cache fetch failed ({type(exc).__name__}).",
            context=context,
        ) from exc
    matrix: List[List[str]] = [list(map(str, row)) for row in rows or []]
    context = _report_fetch_context(tab_name=tab_name, rows=matrix, data_source="direct")
    log.info(
        "recruiter Statistics rows fetched; config_key=%s tab=%r sheet_id_source=%s "
        "row_count=%s first_row=%r data_source=%s clans_cache=%s",
        context.config_key,
        context.tab_name,
        context.sheet_id_source,
        context.row_count,
        context.first_row,
        context.data_source,
        context.clans_cache_state,
    )
    if not matrix:
        raise ReportFetchError(
            "Recruiter report skipped because the Statistics sheet returned no rows.",
            context=context,
        )
    headers = _headers_map(matrix[0]) if matrix else {}
    _require_report_headers(headers, context=context)
    _REPORT_ROWS_CACHE = [list(row) for row in matrix]
    _REPORT_HEADERS_CACHE = dict(headers)
    _REPORT_CONTEXT_CACHE = context
    return matrix, headers


def _extract_report_sections(
    rows: Sequence[Sequence[str]], headers: HeadersMap, context: Optional[ReportFetchContext] = None
) -> ReportSections:
    if not rows:
        ctx = context or _report_fetch_context(tab_name=get_reports_tab_name("Statistics"), rows=rows)
        raise ReportFetchError(
            "Recruiter report skipped because the Statistics sheet returned no rows.",
            context=ctx,
        )
    _require_report_headers(headers, context=context)
    h1_index = _resolve_index(headers, "h1 headline")
    key_index = _resolve_index(headers, "key")
    general_index = _find_row_equals(rows, h1_index, "general overview")
    per_bracket_index = _find_row_equals(rows, h1_index, "per bracket")
    details_index = _find_row_equals(rows, h1_index, "bracket details")

    general_lines: List[str] = []
    if general_index != -1:
        stop_column = h1_index
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
            stop_column=h1_index,
            stop_value=stop_value,
        )
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

    sections: List[Tuple[str, List[Sequence[str]]]] = []
    if details_start > 0 and details_start < len(rows):
        sections = _collect_bracket_sections(
            rows, start_row=details_start, headers=headers
        )

    detail_blocks: List[Tuple[str, List[str]]] = []
    for label, entries in sections:
        formatted = [
            line
            for row in entries
            if (line := _format_line(headers, row, always=False))
        ]
        if formatted:
            detail_blocks.append((label, formatted))

    return ReportSections(
        general_lines=general_lines,
        per_bracket_lines=per_bracket_lines,
        detail_blocks=detail_blocks,
    )


def _summary_footer() -> str:
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return (
        "last updated "
        f"{timestamp} • daily snapshot, for most accurate numbers use `!clanmatch`"
    )


def _finalize_embeds(embeds: Sequence[discord.Embed]) -> None:
    for embed in embeds:
        for field in getattr(embed, "fields", []):
            try:
                field.inline = False
            except Exception:
                pass


def _build_summary_embeds(sections: ReportSections) -> List[discord.Embed]:
    title = "Summary Open Spots"
    footer = _summary_footer()
    embeds = [_new_report_embed(title=title, footer=footer)]

    if sections.general_lines:
        _append_field_paginated(
            embeds,
            title=title,
            footer=footer,
            field_name="General Overview",
            lines=sections.general_lines,
            section="summary_bracket",
        )

    if sections.general_lines and sections.per_bracket_lines:
        _append_field_paginated(
            embeds,
            title=title,
            footer=footer,
            field_name="\n",
            lines=["\u25AB\u25AA\u25AB\u25AA\u25AB\u25AA\u25AB"],
            section="summary_bracket",
        )

    if sections.per_bracket_lines:
        _append_field_paginated(
            embeds,
            title=title,
            footer=footer,
            field_name="Per Bracket",
            lines=sections.per_bracket_lines,
            section="summary_bracket",
        )

    _finalize_embeds(embeds)
    return embeds


def _build_summary_embed(sections: ReportSections) -> discord.Embed:
    return _build_summary_embeds(sections)[0]


def _build_details_embeds(sections: ReportSections) -> List[discord.Embed]:
    title = "Bracket Details"
    embeds = [_new_report_embed(title=title, footer=DETAILS_FILTER_FOOTER)]
    for key, formatted in sections.detail_blocks:
        _append_field_paginated(
            embeds,
            title=title,
            footer=DETAILS_FILTER_FOOTER,
            field_name=key.title(),
            lines=formatted,
            section="bracket_details",
        )

    _finalize_embeds(embeds)
    return embeds


def _build_details_embed(sections: ReportSections) -> discord.Embed:
    return _build_details_embeds(sections)[0]


def _validate_embed_for_send(embed: discord.Embed, *, section: str) -> None:
    fields = list(getattr(embed, "fields", []))
    if len(fields) > DISCORD_EMBED_FIELD_LIMIT:
        raise DailyReportSectionError(
            section,
            "validation",
            f"{section} embed has {len(fields)} fields; Discord limit is {DISCORD_EMBED_FIELD_LIMIT}",
        )
    for field in fields:
        if len(str(getattr(field, "name", ""))) > DISCORD_FIELD_NAME_LIMIT:
            raise DailyReportSectionError(
                section,
                "validation",
                f"{section} embed field name exceeds Discord field name limit",
            )
        if len(str(getattr(field, "value", ""))) > DISCORD_FIELD_VALUE_LIMIT:
            raise DailyReportSectionError(
                section,
                "validation",
                f"{section} embed field {getattr(field, 'name', '-')!r} exceeds Discord field value limit",
            )
    try:
        embed_len = len(embed)
    except Exception:
        embed_len = 0
    if embed_len > DISCORD_EMBED_TOTAL_LIMIT:
        raise DailyReportSectionError(
            section,
            "validation",
            f"{section} embed length {embed_len} exceeds Discord limit {DISCORD_EMBED_TOTAL_LIMIT}",
        )


def _validate_embed_message_payload(
    embeds: Sequence[discord.Embed], *, section: str
) -> None:
    if len(embeds) > DISCORD_EMBEDS_PER_MESSAGE_LIMIT:
        raise DailyReportSectionError(
            section,
            "validation",
            f"{section} message has {len(embeds)} embeds; Discord limit is {DISCORD_EMBEDS_PER_MESSAGE_LIMIT}",
        )
    total = 0
    for embed in embeds:
        _validate_embed_for_send(embed, section=section)
        try:
            total += len(embed)
        except Exception:
            pass
    if total > DISCORD_MESSAGE_EMBED_TOTAL_LIMIT:
        raise DailyReportSectionError(
            section,
            "validation",
            f"{section} message embed payload length {total} exceeds Discord limit {DISCORD_MESSAGE_EMBED_TOTAL_LIMIT}",
        )


def _group_embeds_for_message_payloads(
    embeds: Sequence[discord.Embed], *, section: str
) -> List[List[discord.Embed]]:
    payloads: List[List[discord.Embed]] = []
    current: List[discord.Embed] = []
    current_len = 0
    for embed in embeds:
        _validate_embed_for_send(embed, section=section)
        try:
            embed_len = len(embed)
        except Exception:
            embed_len = 0
        if embed_len > DISCORD_MESSAGE_EMBED_TOTAL_LIMIT:
            raise DailyReportSectionError(
                section,
                "pagination",
                f"{section} embed length {embed_len} cannot fit in one Discord message",
            )
        if (
            current
            and (
                len(current) >= DISCORD_EMBEDS_PER_MESSAGE_LIMIT
                or current_len + embed_len > DISCORD_MESSAGE_EMBED_TOTAL_LIMIT
            )
        ):
            _validate_embed_message_payload(current, section=section)
            payloads.append(current)
            current = []
            current_len = 0
        current.append(embed)
        current_len += embed_len
    if current:
        _validate_embed_message_payload(current, section=section)
        payloads.append(current)
    return payloads


def _validate_summary_section(
    sections: ReportSections,
    summary_embeds: Sequence[discord.Embed],
    details_embeds: Sequence[discord.Embed],
) -> None:
    if not (
        sections.general_lines
        or sections.per_bracket_lines
        or sections.detail_blocks
    ):
        raise DailyReportSectionError(
            "summary_bracket",
            "build",
            "summary/bracket builder returned no rows after applying sheet hide rules",
        )
    if not summary_embeds:
        raise DailyReportSectionError(
            "summary_bracket",
            "pagination",
            "summary/bracket pagination produced no summary embeds",
        )
    if not details_embeds:
        raise DailyReportSectionError(
            "bracket_details",
            "pagination",
            "summary/bracket pagination produced no bracket details embeds",
        )
    _group_embeds_for_message_payloads(summary_embeds, section="summary_bracket")
    details_payloads = _group_embeds_for_message_payloads(
        details_embeds, section="bracket_details"
    )
    if len(details_payloads) > 1:
        raise DailyReportSectionError(
            "bracket_details",
            "pagination",
            "Bracket Details button would require multiple Discord edit payloads",
        )


def _log_summary_section_failure(
    message: str,
    exc: BaseException,
    *,
    phase: str,
    context: Optional[ReportFetchContext] = None,
) -> None:
    section = getattr(exc, "section", "summary_bracket")
    failure_phase = getattr(exc, "phase", phase)
    if context is not None:
        log.warning(
            "%s; section=%s phase=%s config_key=%s tab=%r sheet_id_source=%s row_count=%s "
            "data_source=%s clans_cache=%s error=%s",
            message,
            section,
            failure_phase,
            context.config_key,
            context.tab_name,
            context.sheet_id_source,
            context.row_count,
            context.data_source,
            context.clans_cache_state,
            _format_error(exc),
            exc_info=True,
        )
        return
    log.warning(
        "%s; section=%s phase=%s error=%s",
        message,
        section,
        failure_phase,
        _format_error(exc),
        exc_info=True,
    )


async def _load_report_sections() -> ReportSections:
    rows, headers = await _fetch_report_rows()
    return _extract_report_sections(rows, headers, _REPORT_CONTEXT_CACHE)


class OpenSpotsPager(discord.ui.View):
    def __init__(self, sections: ReportSections | None = None) -> None:
        super().__init__(timeout=None)
        self.sections = sections
        self.current_page = "summary"

        self.summary_button.disabled = True
        self.details_button.disabled = False

    def _set_page_state(self, page: str) -> None:
        self.current_page = page
        self.summary_button.disabled = page == "summary"
        self.details_button.disabled = page == "details"

    async def _resolve_sections(self) -> ReportSections:
        try:
            sections = await _load_report_sections()
        except Exception:
            if self.sections is None:
                raise
            log.debug("failed to refresh recruiter report sections for pager", exc_info=True)
            return self.sections
        self.sections = sections
        return sections

    async def set_summary(self, interaction: discord.Interaction) -> None:
        sections = await self._resolve_sections()
        self._set_page_state("summary")
        embeds = _build_summary_embeds(sections)
        payloads = _group_embeds_for_message_payloads(embeds, section="summary_bracket")
        await interaction.response.edit_message(embeds=payloads[0], view=self)

    async def set_details(self, interaction: discord.Interaction) -> None:
        sections = await self._resolve_sections()
        self._set_page_state("details")
        embeds = _build_details_embeds(sections)
        payloads = _group_embeds_for_message_payloads(embeds, section="bracket_details")
        if len(payloads) > 1:
            raise DailyReportSectionError(
                "bracket_details",
                "pagination",
                "Bracket Details button would require multiple Discord edit payloads",
            )
        await interaction.response.edit_message(embeds=payloads[0], view=self)

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
    context = _report_fetch_context(
        tab_name="Statistics",
        rows=rows,
        data_source="test" if rows else "direct",
    )
    sections = _extract_report_sections(rows, headers, context)
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
    if isinstance(exc, ReportFetchError):
        return str(exc)
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
    try:
        rows, headers = await _fetch_report_rows()
    except Exception as exc:
        if isinstance(exc, ReportFetchError):
            _log_report_diagnostics(
                "failed to fetch recruiter report rows",
                context=exc.context,
                exc_info=True,
            )
        else:
            log.warning("failed to fetch recruiter report rows", exc_info=True)
        return False, _format_error(exc)

    try:
        sections = _extract_report_sections(rows, headers, _REPORT_CONTEXT_CACHE)
        summary_embeds = _build_summary_embeds(sections)
        details_embeds = _build_details_embeds(sections)
        pager_view = OpenSpotsPager(sections)
        _validate_summary_section(sections, summary_embeds, details_embeds)
        summary_payloads = _group_embeds_for_message_payloads(
            summary_embeds, section="summary_bracket"
        )
    except Exception as exc:
        if isinstance(exc, ReportSchemaError):
            _log_report_diagnostics(
                "failed to build daily recruiter report summary/bracket section: missing Statistics headers",
                context=exc.context,
                required=exc.required,
                exc_info=True,
            )
        elif isinstance(exc, ReportFetchError):
            _log_report_diagnostics(
                "failed to build daily recruiter report summary/bracket section: Statistics fetch/read failure",
                context=exc.context,
                exc_info=True,
            )
        else:
            _log_summary_section_failure(
                "failed to build daily recruiter report summary/bracket section",
                exc,
                phase="build",
                context=_REPORT_CONTEXT_CACHE,
            )
        return False, _format_error(exc)

    today = datetime.now(UTC).strftime("%Y-%m-%d")
    content = _report_content(today)

    try:
        await channel.send(content=content, embeds=summary_payloads[0], view=pager_view)
        for payload in summary_payloads[1:]:
            await channel.send(content="Summary Open Spots (continued)", embeds=payload)
    except Exception as exc:
        _log_summary_section_failure(
            "Discord rejected daily recruiter report summary/bracket section send",
            exc,
            phase="send",
            context=_REPORT_CONTEXT_CACHE,
        )
        return False, _format_error(exc)

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


def register_persistent_views(bot: discord.Client) -> None:
    global _PERSISTENT_VIEW_REGISTERED
    if _PERSISTENT_VIEW_REGISTERED or getattr(bot, _PERSISTENT_VIEW_ATTR, False):
        return
    try:
        bot.add_view(OpenSpotsPager())
    except ValueError as exc:
        if "already" not in str(exc).lower() and "duplicate" not in str(exc).lower():
            raise
        log.debug("open spots persistent view was already registered", exc_info=True)
    setattr(bot, _PERSISTENT_VIEW_ATTR, True)
    _PERSISTENT_VIEW_REGISTERED = True


async def ensure_scheduler_started(bot: discord.Client) -> None:
    global _BOT_REFERENCE
    _BOT_REFERENCE = bot
    register_persistent_views(bot)

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
    "register_persistent_views",
    "post_daily_recruiter_update",
    "run_full_recruiter_reports",
    "scheduler_daily_recruiter_update",
]
