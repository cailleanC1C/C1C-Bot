"""Embed rendering helpers kept import-safe for C-03 compliance."""

from __future__ import annotations

import datetime as dt
import os
import platform
from dataclasses import dataclass
from typing import Sequence

import discord

from c1c_coreops.help import COREOPS_VERSION, build_coreops_footer
from shared import logfmt
from shared.utils import humanize_duration


_DISCORD_FIELD_NAME_LIMIT = 256
_DISCORD_FIELD_VALUE_LIMIT = 1024
_DISCORD_FIELDS_PER_EMBED = 25
_DISCORD_EMBED_TEXT_LIMIT = 6000


def _embed_text_len(embed: discord.Embed) -> int:
    total = len(embed.title or "") + len(embed.description or "")
    total += len(getattr(embed.footer, "text", None) or "")
    total += len(getattr(embed.author, "name", None) or "")
    for field in embed.fields:
        total += len(field.name or "") + len(field.value or "")
    return total


def _split_field_value(value: str, limit: int = _DISCORD_FIELD_VALUE_LIMIT) -> list[str]:
    text = value or "—"
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            if current:
                chunks.append(current.rstrip("\n") or "—")
                current = ""
            start = 0
            while start < len(line):
                chunks.append(line[start : start + limit].rstrip("\n") or "—")
                start += limit
            continue
        if len(current) + len(line) > limit:
            chunks.append(current.rstrip("\n") or "—")
            current = line
        else:
            current += line
    if current or not chunks:
        chunks.append(current.rstrip("\n") or "—")
    return chunks


def _continued_field_name(base_name: str, index: int) -> str:
    clean = _sanitize_inline(base_name, allow_empty=True)[:_DISCORD_FIELD_NAME_LIMIT] or "​"
    if index == 0:
        return clean
    stem = "Continued" if clean == "​" else f"{clean} continued"
    suffix = "" if index == 1 else f" {index}"
    return f"{stem}{suffix}"[:_DISCORD_FIELD_NAME_LIMIT]


def _new_checksheet_embed(colour: discord.Colour, footer_text: str, *, index: int = 0) -> discord.Embed:
    title = "Checksheet — Tabs & Headers"
    if index:
        title = f"{title} ({index + 1})"
    embed = discord.Embed(title=title, colour=colour)
    embed.set_footer(text=footer_text)
    return embed


def _add_paginated_field(
    embeds: list[discord.Embed],
    *,
    colour: discord.Colour,
    footer_text: str,
    name: str,
    value: str,
    inline: bool = False,
) -> None:
    for idx, chunk in enumerate(_split_field_value(value)):
        field_name = _continued_field_name(name, idx)
        current = embeds[-1]
        projected = _embed_text_len(current) + len(field_name) + len(chunk)
        if len(current.fields) >= _DISCORD_FIELDS_PER_EMBED or projected > _DISCORD_EMBED_TEXT_LIMIT:
            embeds.append(_new_checksheet_embed(colour, footer_text, index=len(embeds)))
            current = embeds[-1]
        current.add_field(name=field_name, value=chunk, inline=inline)

def _hms(seconds: float) -> str:
    s = int(max(0, seconds))
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return f"{h:d}h {m:02d}m {s:02d}s"

def build_digest_line(
    *, env: str, uptime_sec: float | None, latency_s: float | None, last_event_age: float | None
) -> str:
    uptime_text = _format_humanized(int(uptime_sec) if uptime_sec is not None else None)
    latency_text = _format_latency_seconds(latency_s)
    gateway_text = _format_humanized(int(last_event_age) if last_event_age is not None else None)
    return (
        f"env: {_sanitize_inline(env)} · uptime: {uptime_text} · "
        f"latency: {latency_text} · gateway: last {gateway_text}"
    )


_EM_DOT = " • "


def _sanitize_inline(text: object, *, allow_empty: bool = False) -> str:
    cleaned = str(text or "").strip()
    if not cleaned and not allow_empty:
        return "n/a"
    return cleaned.replace("`", "ʼ")


def _format_humanized(seconds: int | None) -> str:
    if seconds is None:
        return "n/a"
    return humanize_duration(int(max(0, seconds)))


def _format_latency_ms(latency_ms: int | None) -> str:
    if latency_ms is None:
        return "n/a"
    return f"{int(latency_ms)}ms"


def _format_latency_seconds(latency_s: float | None) -> str:
    if latency_s is None:
        return "n/a"
    return f"{int(max(0, latency_s) * 1000):d}ms"


def _format_next_eta(delta: int | None, at: dt.datetime | None) -> str:
    if delta is None and at is not None:
        try:
            delta = int((at - dt.datetime.now(dt.timezone.utc)).total_seconds())
        except Exception:
            delta = None
    if delta is None:
        return "n/a"
    if delta == 0:
        return "now"
    human = _format_humanized(abs(delta))
    if human == "n/a":
        return "n/a"
    return f"in {human}" if delta > 0 else f"{human} ago"


def _prefix_estimated(text: str, estimated: bool) -> str:
    if estimated and text not in {"n/a", "-"}:
        return f"~{text}"
    return text


def _build_sheets_field(entries: Sequence[DigestSheetEntry]) -> str:
    if not entries:
        return "n/a"

    lines = []
    for entry in entries:
        status = entry.status or "n/a"
        age_text = _prefix_estimated(
            _format_humanized(entry.age_seconds), entry.age_estimated
        )
        next_text = _prefix_estimated(
            _format_next_eta(entry.next_refresh_delta_seconds, entry.next_refresh_at),
            entry.next_refresh_estimated,
        )
        retries_text = "n/a" if entry.retries is None else str(entry.retries)
        error_raw = _sanitize_inline(entry.error, allow_empty=True)
        error_text = error_raw if error_raw else "n/a"
        line = (
            f"{entry.display_name} — {status} · "
            f"age {age_text} · next {next_text} · "
            f"retries {retries_text} · err {error_text}"
        )
        lines.append(line)
    return "\n".join(lines)


def _build_sheets_client_field(summary: DigestSheetsClientSummary | None) -> str:
    if summary is None:
        return "last success: n/a · latency: n/a · retries: n/a"

    success_text = _format_humanized(summary.last_success_age)
    if success_text != "n/a":
        success_text = f"{success_text} ago"
    latency_text = _format_latency_ms(summary.latency_ms)
    retries_text = "n/a" if summary.retries is None else str(summary.retries)

    lines = [f"last success: {success_text} · latency: {latency_text} · retries: {retries_text}"]
    if summary.last_error:
        lines.append(f"last error: {_sanitize_inline(summary.last_error)}")
    return "\n".join(lines)


def _format_description(data: DigestEmbedData) -> str:
    uptime = _format_humanized(data.uptime_seconds if data.uptime_seconds is not None else None)
    latency = _format_latency_seconds(data.latency_seconds)
    gateway = _format_humanized(data.gateway_age_seconds if data.gateway_age_seconds is not None else None)
    return (
        f"{_sanitize_inline(data.env)}{_EM_DOT}uptime {uptime}{_EM_DOT}"
        f"latency {latency}{_EM_DOT}gateway last {gateway}"
    )


def build_digest_embed(data: DigestEmbedData) -> discord.Embed:
    colour_factory = getattr(discord.Colour, "blurple", None)
    color = colour_factory() if callable(colour_factory) else discord.Colour.blue()
    embed = discord.Embed(title="Digest", description=_format_description(data), colour=color)

    embed.add_field(name="Sheets", value=_build_sheets_field(data.sheets), inline=False)
    embed.add_field(name="Sheets client", value=_build_sheets_client_field(data.sheets_client), inline=False)

    tip_text = _maybe_build_tip(data.sheets)
    if tip_text:
        embed.add_field(name="​", value=tip_text, inline=False)

    footer_parts = [
        logfmt.LOG_EMOJI["lifecycle"],
        f"Bot v{_sanitize_inline(data.bot_version)}",
        f"CoreOps v{_sanitize_inline(data.coreops_version)}",
    ]
    embed.set_footer(text=" · ".join(part for part in footer_parts if part))
    return embed


def _format_config_sheets(entries: Sequence[dict[str, object]]) -> str:
    if not entries:
        return "n/a"

    lines: list[str] = []
    for entry in entries:
        label = _sanitize_inline(entry.get("label", "Sheet"))
        ok = bool(entry.get("ok"))
        status = entry.get("status")
        status_text = _sanitize_inline(status or ("Connected" if ok else "Missing"))
        if ok:
            note_parts: list[str] = []
            for key in ("hint", "detail"):
                value = entry.get(key)
                if isinstance(value, str) and value.strip():
                    note_parts.append(_sanitize_inline(value))
            short_id = entry.get("short_id")
            if isinstance(short_id, str) and short_id.strip():
                short_clean = _sanitize_inline(short_id)
                if short_clean not in note_parts:
                    note_parts.append(short_clean)
            hint_text = f" *({' — '.join(note_parts)})*" if note_parts else ""
            lines.append(f"{label} → ✅ {status_text}{hint_text}")
        else:
            reason = entry.get("reason")
            reason_text = (
                f" — {_sanitize_inline(reason)}"
                if isinstance(reason, str) and reason.strip()
                else ""
            )
            lines.append(f"{label} → ⚠️ {status_text}{reason_text}")
    return "\n".join(lines)


def _format_config_guilds(connected: dict[str, object], allow: dict[str, object]) -> str:
    connected_items = connected.get("items")
    if isinstance(connected_items, Sequence) and not isinstance(connected_items, (str, bytes)):
        connected_lines = ", ".join(_sanitize_inline(item) for item in connected_items if item)
    else:
        connected_lines = ""
    if not connected_lines:
        connected_lines = "n/a"

    allow_total = allow.get("count")
    if not isinstance(allow_total, int):
        allow_items = allow.get("items")
        if isinstance(allow_items, Sequence) and not isinstance(allow_items, (str, bytes)):
            allow_total = len([item for item in allow_items if item])
        else:
            allow_total = 0
    summary = allow.get("summary")
    summary_text = (
        f" ({_sanitize_inline(summary)})"
        if isinstance(summary, str) and summary.strip()
        else ""
    )

    lines = [f"Connected guilds: {connected_lines}", f"Allow-listed: {allow_total} total{summary_text}"]
    return "\n".join(lines)


def _format_config_source(
    source_info: dict[str, object],
    default_source: str | None,
) -> str:
    loaded_from = source_info.get("loaded_from")
    if not isinstance(loaded_from, str) or not loaded_from.strip():
        loaded_from = default_source or "n/a"
    loaded_line = f"Loaded from: {_sanitize_inline(loaded_from)}"

    override_items = source_info.get("overrides")
    overrides: list[str] = []
    if isinstance(override_items, dict):
        overrides = [str(key) for key in override_items.keys()]
    elif isinstance(override_items, (set, tuple, list)):
        overrides = [str(item) for item in override_items]
    elif isinstance(override_items, str) and override_items.strip():
        overrides = [override_items.strip()]

    overrides = [item for item in overrides if item.strip()]
    if not overrides:
        override_line = "Overrides: none"
    else:
        overrides_sorted = sorted(overrides)
        overrides_text = ", ".join(_sanitize_inline(item) for item in overrides_sorted)
        override_line = f"Overrides: {len(overrides_sorted)} keys — {overrides_text}"

    return "\n".join([loaded_line, override_line])


def _format_ops_channel(ops_info: dict[str, object]) -> str:
    configured = bool(ops_info.get("ok") or ops_info.get("configured"))
    detail = ops_info.get("detail")
    detail_text = (
        f" *({_sanitize_inline(detail)})*"
        if isinstance(detail, str) and detail.strip()
        else ""
    )
    if configured:
        return f"Logs → ✅ Configured{detail_text}"

    missing_hint = ops_info.get("missing_hint")
    if not isinstance(missing_hint, str) or not missing_hint.strip():
        missing_hint = detail if isinstance(detail, str) else ""
    hint_text = (
        f" *({_sanitize_inline(missing_hint)})*"
        if missing_hint and missing_hint.strip()
        else ""
    )
    return f"Logs → ⚠️ Missing{hint_text}"


def build_config_embed(
    snapshot: dict[str, object],
    meta: dict[str, object] | None,
    *,
    bot_version: str,
    coreops_version: str = COREOPS_VERSION,
) -> discord.Embed:
    meta = meta or {}
    overview = meta.get("overview") if isinstance(meta, dict) else None
    if not isinstance(overview, dict):
        overview = {}

    env_value = overview.get("env") or snapshot.get("ENV_NAME") or meta.get("env")
    env_text = _sanitize_inline(env_value or "n/a")

    connected_info = overview.get("connected")
    if not isinstance(connected_info, dict):
        connected_info = {}

    allow_info = overview.get("allow")
    if not isinstance(allow_info, dict):
        allow_info = {}

    connected_count = connected_info.get("count")
    if not isinstance(connected_count, int):
        connected_items = connected_info.get("items")
        if isinstance(connected_items, Sequence) and not isinstance(connected_items, (str, bytes)):
            connected_count = len([item for item in connected_items if item])
        else:
            connected_count = 0

    allow_count = allow_info.get("count")
    if not isinstance(allow_count, int):
        allow_items = allow_info.get("items")
        if isinstance(allow_items, Sequence) and not isinstance(allow_items, (str, bytes)):
            allow_count = len([item for item in allow_items if item])
        else:
            allow_count = 0

    description = (
        f"Environment: {env_text}{_EM_DOT}"
        f"Connected guilds: {connected_count}{_EM_DOT}"
        f"Allow-list: {allow_count}"
    )

    colour_factory = getattr(discord.Colour, "blurple", None)
    colour = colour_factory() if callable(colour_factory) else discord.Colour.blue()
    embed = discord.Embed(title="Config Overview", description=description, colour=colour)

    sheets_entries = overview.get("sheets")
    if isinstance(sheets_entries, Sequence) and not isinstance(sheets_entries, (str, bytes)):
        sheets_value = _format_config_sheets(sheets_entries)
    else:
        sheets_value = "n/a"
    embed.add_field(name="Sheets", value=sheets_value, inline=False)

    guilds_value = _format_config_guilds(connected_info, allow_info)
    embed.add_field(name="Guild Access", value=guilds_value, inline=False)

    source_info = overview.get("source")
    if not isinstance(source_info, dict):
        source_info = {}
    default_source = meta.get("source") if isinstance(meta, dict) else None
    source_value = _format_config_source(source_info, default_source if isinstance(default_source, str) else None)
    embed.add_field(name="Source", value=source_value, inline=False)

    ops_info = overview.get("ops")
    if not isinstance(ops_info, dict):
        ops_info = {}
    ops_value = _format_ops_channel(ops_info)
    embed.add_field(name="Ops Channel", value=ops_value, inline=False)

    footer_parts = [
        logfmt.LOG_EMOJI["lifecycle"],
        f"Bot v{_sanitize_inline(bot_version)}",
        f"CoreOps v{_sanitize_inline(coreops_version)}",
    ]
    embed.set_footer(text=" · ".join(part for part in footer_parts if part))
    return embed


def _maybe_build_tip(entries: Sequence[DigestSheetEntry]) -> str | None:
    failed = [entry for entry in entries if str(entry.status or "").lower() == "fail"]
    if not failed:
        return None
    failed_names = {str(entry.display_name or "").strip().lower() for entry in failed}
    failed_errors = " ".join(str(entry.error or "") for entry in failed).lower()
    if "_retry_with_backoff must not run inside an active event loop" in failed_errors:
        return "Sheet refresh hit an event-loop guard; this needs code attention, not a ClanInfo refresh."
    if failed_names == {"claninfo"}:
        return 'Need latest openings? run "!ops refresh clansinfo" and retry.'
    if failed_names == {"templates"}:
        return "Templates refresh failed; this likely needs template sheet/config attention."
    if "fusion" in failed_errors or "config" in failed_errors:
        return "Sheet Config/client issue detected; check the failing bucket and Config key shown above."
    return "One or more sheet buckets failed; refresh or investigate the specific failing bucket shown above."


@dataclass(frozen=True)
class DigestEmbedData:
    env: str
    uptime_seconds: int | None
    latency_seconds: float | None
    gateway_age_seconds: int | None
    sheets: Sequence[DigestSheetEntry]
    sheets_client: DigestSheetsClientSummary | None
    bot_version: str
    coreops_version: str = COREOPS_VERSION


@dataclass(frozen=True)
class DigestSheetEntry:
    display_name: str
    status: str
    age_seconds: int | None
    next_refresh_delta_seconds: int | None
    next_refresh_at: dt.datetime | None
    retries: int | None
    error: str | None
    age_estimated: bool = False
    next_refresh_estimated: bool = False


@dataclass(frozen=True)
class DigestSheetsClientSummary:
    last_success_age: int | None = None
    latency_ms: int | None = None
    retries: int | None = None
    last_error: str | None = None


@dataclass(frozen=True)
class ChecksheetTabEntry:
    name: str
    ok: bool
    rows: str
    headers: str
    error: str | None = None
    first_headers: Sequence[str] | None = None


@dataclass(frozen=True)
class ChecksheetSheetEntry:
    title: str
    sheet_id: str
    tabs: Sequence[ChecksheetTabEntry]
    warnings: Sequence[str] = ()
    config_tab: str | None = None
    config_headers: str | None = None
    config_preview_rows: Sequence[Sequence[str]] = ()
    discovered_tabs: Sequence[str] = ()


@dataclass(frozen=True)
class ChecksheetEmbedData:
    sheets: Sequence[ChecksheetSheetEntry]
    bot_version: str
    coreops_version: str = COREOPS_VERSION
    debug: bool = False


def build_checksheet_tabs_embeds(data: ChecksheetEmbedData) -> list[discord.Embed]:
    colour_factory = getattr(discord.Colour, "dark_teal", None)
    colour = colour_factory() if callable(colour_factory) else discord.Colour.teal()
    footer_parts = [
        logfmt.LOG_EMOJI["lifecycle"],
        f"Bot v{_sanitize_inline(data.bot_version)}",
        f"CoreOps v{_sanitize_inline(data.coreops_version)}",
    ]
    footer_text = " · ".join(part for part in footer_parts if part)
    embeds = [_new_checksheet_embed(colour, footer_text)]

    _add_paginated_field(
        embeds,
        colour=colour,
        footer_text=footer_text,
        name="Google Sheets",
        value="Public client",
        inline=False,
    )

    for sheet in data.sheets:
        lines: list[str] = []
        sheet_ok = not sheet.warnings and all(tab.ok for tab in sheet.tabs)
        icon = "✅" if sheet_ok else "🔴"
        title_text = _sanitize_inline(sheet.title)
        sheet_id = _sanitize_inline(sheet.sheet_id or "—")
        lines.append(f"{icon} {title_text} — {sheet_id}")

        for warning in sheet.warnings:
            lines.append(f"⚠️ {_sanitize_inline(warning)}")

        if sheet.warnings and sheet.tabs:
            lines.append("")

        for tab in sheet.tabs:
            tab_name = _sanitize_inline(tab.name)
            headers_preview = _sanitize_inline(tab.headers) if tab.headers else ""
            if tab.ok:
                rows_text = _sanitize_inline(tab.rows or "0")
                lines.append(f"✅ {tab_name} — {rows_text} rows")
            else:
                rows_text = _sanitize_inline(tab.rows or "n/a")
                lines.append(f"🔴 {tab_name} — rows {rows_text}")

            header_text = headers_preview if headers_preview else "—"
            header_line = f"Headers: {header_text}"
            if data.debug:
                sanitized_first: list[str] = []
                for raw_item in tab.first_headers or []:
                    cleaned = _sanitize_inline(raw_item, allow_empty=True)
                    if cleaned and cleaned.strip():
                        sanitized_first.append(cleaned.strip())
                first_headers = ", ".join(sanitized_first) if sanitized_first else "—"
                header_line = f"{header_line} • First headers: {first_headers}"
            lines.append(header_line)
            if not tab.ok and tab.error:
                lines.append(f"Error: {_sanitize_inline(tab.error)}")

        block = "\n".join(lines) if lines else "—"
        _add_paginated_field(
            embeds,
            colour=colour,
            footer_text=footer_text,
            name="​",
            value=block,
            inline=False,
        )

        if data.debug:
            config_tab = _sanitize_inline(sheet.config_tab or "Config") or "Config"
            preview_rows: list[str] = []
            for raw_row in sheet.config_preview_rows[:5]:
                if isinstance(raw_row, Sequence) and not isinstance(raw_row, (str, bytes)):
                    raw_cells = list(raw_row)[:2]
                else:
                    raw_cells = []
                formatted_cells: list[str] = []
                for cell in raw_cells:
                    cleaned = _sanitize_inline(cell, allow_empty=True)
                    cleaned = cleaned.strip()
                    formatted_cells.append(cleaned)
                if not formatted_cells:
                    preview_rows.append("[]")
                    continue
                formatted = ", ".join(
                    f'"{value}"' if value else '""' for value in formatted_cells
                )
                preview_rows.append(f"[{formatted}]")
            if preview_rows and all(item == "[]" for item in preview_rows):
                first_rows = "—"
            elif preview_rows:
                first_rows = "; ".join(preview_rows)
            else:
                first_rows = "—"
            if len(first_rows) > 120:
                first_rows = f"{first_rows[:117]}…"
            first_rows_value = _sanitize_inline(first_rows, allow_empty=True) or "—"

            sanitized_tabs: list[str] = []
            for raw_name in sheet.discovered_tabs:
                cleaned = _sanitize_inline(raw_name, allow_empty=True)
                if cleaned and cleaned.strip():
                    sanitized_tabs.append(cleaned.strip())
            joined_tabs = ", ".join(sanitized_tabs)
            if len(joined_tabs) > 120:
                joined_tabs = f"{joined_tabs[:117]}…"
            joined_tabs_value = _sanitize_inline(joined_tabs, allow_empty=True)

            debug_lines = [f"config_tab: {config_tab}", f"first_rows: {first_rows_value or '—'}"]
            if joined_tabs_value:
                debug_lines.append(f"discovered: {joined_tabs_value}")
            _add_paginated_field(
                embeds,
                colour=colour,
                footer_text=footer_text,
                name="Debug preview",
                value="\n".join(debug_lines),
                inline=False,
            )

    return embeds


def build_checksheet_tabs_embed(data: ChecksheetEmbedData) -> discord.Embed:
    return build_checksheet_tabs_embeds(data)[0]

def build_health_embed(
    *,
    bot_name: str,
    env: str,
    version: str,
    uptime_sec: float,
    latency_s: float|None,
    last_event_age: float,
    keepalive_sec: int,
    stall_after_sec: int,
    disconnect_grace_sec: int,
) -> discord.Embed:
    e = discord.Embed(title=f"{bot_name} · health", colour=discord.Colour.blurple())
    e.add_field(name="env", value=env, inline=True)
    e.add_field(name="version", value=version, inline=True)
    e.add_field(name="uptime", value=_hms(uptime_sec), inline=True)

    e.add_field(name="latency", value=("—" if latency_s is None else f"{latency_s*1000:.0f} ms"), inline=True)
    e.add_field(name="last event", value=f"{int(last_event_age)} s", inline=True)
    e.add_field(name="keepalive", value=f"{keepalive_sec}s", inline=True)

    e.add_field(name="stall after", value=f"{stall_after_sec}s", inline=True)
    e.add_field(name="disconnect grace", value=f"{disconnect_grace_sec}s", inline=True)
    e.add_field(name="pid", value=str(os.getpid()), inline=True)

    footer_notes = f" • {platform.system()} {platform.release()}"
    e.set_footer(text=build_coreops_footer(bot_version=version, notes=footer_notes))
    return e

def build_env_embed(*, bot_name: str, env: str, version: str, cfg_meta: dict[str, object]) -> discord.Embed:
    e = discord.Embed(title=f"{bot_name} · env", colour=discord.Colour.dark_teal())
    e.add_field(name="env", value=env, inline=True)
    e.add_field(name="version", value=version, inline=True)
    src = cfg_meta.get("source", "runtime-only")
    status = cfg_meta.get("status", "ok")
    e.add_field(name="config", value=f"{src} ({status})", inline=True)
    # Show a few safe vars for sanity (no secrets)
    safe = []
    for k in ("WATCHDOG_CHECK_SEC", "WATCHDOG_STALL_SEC", "WATCHDOG_DISCONNECT_GRACE_SEC"):
        v = os.getenv(k)
        if v:
            safe.append(f"{k}={v}")
    e.add_field(name="settings", value="\n".join(safe) if safe else "—", inline=False)
    e.set_footer(text=build_coreops_footer(bot_version=version))
    return e


@dataclass(frozen=True)
class RefreshEmbedRow:
    bucket: str
    duration: str
    result: str
    retries: str
    ttl_expired: str
    count: str
    error: str


def build_refresh_embed(
    *,
    scope: str,
    actor_display: str,
    trigger: str,
    rows: Sequence[RefreshEmbedRow],
    total_ms: int,
    bot_version: str,
    coreops_version: str = COREOPS_VERSION,
    now_utc: dt.datetime | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"Refresh • {scope}",
        colour=getattr(discord.Colour, "dark_theme", discord.Colour.dark_teal)(),
    )

    actor_line = f"actor: {actor_display.strip() or actor_display} • trigger: {trigger}"
    embed.description = actor_line

    headers = ["bucket", "duration", "result", "retries", "ttl", "count", "error"]
    data = [
        [
            row.bucket,
            row.duration,
            row.result,
            row.retries,
            row.ttl_expired,
            row.count,
            row.error,
        ]
        for row in rows
    ]

    if data:
        widths = [len(header) for header in headers]
        for row in data:
            for idx, cell in enumerate(row):
                widths[idx] = max(widths[idx], len(cell))

        header_line = " | ".join(
            header.ljust(widths[idx]) for idx, header in enumerate(headers)
        )
        separator_line = "-+-".join("-" * width for width in widths)
        body_lines = [
            " | ".join(cell.ljust(widths[idx]) for idx, cell in enumerate(row))
            for row in data
        ]
        table = "\n".join([header_line, separator_line, *body_lines])
    else:
        table = "no buckets"

    embed.add_field(name="Buckets", value=f"```{table}```", inline=False)
    footer_parts = [logfmt.LOG_EMOJI["lifecycle"]]
    if bot_version and coreops_version:
        footer_parts.extend([f"Bot v{bot_version}", f"CoreOps v{coreops_version}"])
    footer_parts.append(f"total: {total_ms} ms")
    embed.set_footer(text=" · ".join(part for part in footer_parts if part))
    return embed


__all__ = [
    "DigestEmbedData",
    "DigestSheetEntry",
    "DigestSheetsClientSummary",
    "ChecksheetTabEntry",
    "ChecksheetSheetEntry",
    "ChecksheetEmbedData",
    "RefreshEmbedRow",
    "build_digest_line",
    "build_digest_embed",
    "build_checksheet_tabs_embed",
    "build_checksheet_tabs_embeds",
    "build_health_embed",
    "build_env_embed",
    "build_refresh_embed",
    "build_config_embed",
]
