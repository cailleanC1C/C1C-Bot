from __future__ import annotations

import datetime as dt
import io
import logging
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

import discord

from modules.common import feature_flags
from shared.sheets import core as sheets_core
from shared.sheets import recruitment
from shared.sheets.recruitment import afetch_reports_tab
from shared.sheets.export_utils import ImageExportError, export_pdf_as_png, get_tab_gid

log = logging.getLogger("c1c.housekeeping.c1c_ad")

FEATURE_KEY = "c1c_ad"
CONFIG_KEYS = (
    "C1C_AD_TAB",
    "C1C_AD_IMAGE_RANGE",
    "C1C_AD_TEXT_TAB",
    "C1C_AD_TEXT_ROW",
    "C1C_AD_TARGET_THREAD_ID",
    "C1C_AD_REFRESH_DAYS",
)

PLACEHOLDER_CONFIG = {
    "OPEN_SPOTS_ENDGAME": "open_spots_endgame_brackets",
    "OPEN_SPOTS_LATEGAME": "open_spots_lategame_brackets",
    "OPEN_SPOTS_MIDGAME": "open_spots_midgame_brackets",
    "OPEN_SPOTS_EARLY": "open_spots_early_brackets",
}
PLACEHOLDER_TOKENS = {key: f"[{key}]" for key in PLACEHOLDER_CONFIG}
OPEN_SPOTS_EMPTY_TEXT_HEADER = "open_spots_empty_text"
DISCORD_TEXT_LIMIT = 2000
RESOLVED_TEXT_TOO_LONG_ERROR = "resolved ad text exceeds Discord 2000 character limit"
STATISTICS_REQUIRED_HEADERS = (
    "h1_headline",
    "h2_headline",
    "key",
    "open_spots",
)

REQUIRED_HEADERS = (
    "ad_text",
    "last_posted_at_utc",
    "last_image_message_id",
    "last_text_message_id",
    "last_post_status",
    "last_post_error",
    "updated_at_utc",
)


@dataclass(frozen=True)
class C1CAdConfig:
    image_tab: str
    image_range: str
    text_tab: str
    text_row: int
    target_thread_id: int
    refresh_days: int


@dataclass(frozen=True)
class C1CAdResult:
    status: str
    message: str
    posted: bool = False


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _timestamp(now: dt.datetime | None = None) -> str:
    return (now or _utc_now()).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _column_letter(index: int) -> str:
    label = ""
    value = index
    while value > 0:
        value, rem = divmod(value - 1, 26)
        label = chr(ord("A") + rem) + label
    return label


def _header_map(headers: list[Any]) -> dict[str, int]:
    return {
        str(cell or "").strip(): idx
        for idx, cell in enumerate(headers, start=1)
        if str(cell or "").strip()
    }


def _resolve_config() -> tuple[C1CAdConfig | None, str | None]:
    values: dict[str, str] = {}
    for key in CONFIG_KEYS:
        value = recruitment.get_config_value(key, None)
        if not value:
            return None, f"missing Config key {key}"
        values[key] = value
    try:
        text_row = int(values["C1C_AD_TEXT_ROW"])
        thread_id = int(values["C1C_AD_TARGET_THREAD_ID"])
        refresh_days = int(values["C1C_AD_REFRESH_DAYS"])
    except ValueError as exc:
        return None, f"invalid Config value {exc}"
    if text_row < 2:
        return None, "C1C_AD_TEXT_ROW must be 2 or greater"
    if refresh_days < 1:
        return None, "C1C_AD_REFRESH_DAYS must be positive"
    return (
        C1CAdConfig(
            image_tab=values["C1C_AD_TAB"],
            image_range=values["C1C_AD_IMAGE_RANGE"],
            text_tab=values["C1C_AD_TEXT_TAB"],
            text_row=text_row,
            target_thread_id=thread_id,
            refresh_days=refresh_days,
        ),
        None,
    )


def _get_sheet_id() -> tuple[str | None, str | None]:
    sheet_id = recruitment.get_recruitment_sheet_id()
    if not sheet_id:
        return None, "Recruitment sheet ID missing"
    return sheet_id, None


def _read_text_row(
    sheet_id: str, config: C1CAdConfig
) -> tuple[dict[str, str] | None, dict[str, int] | None, str | None]:
    values = (
        sheets_core.sheets_read(sheet_id, f"{config.text_tab}!1:{config.text_row}")
        or []
    )
    if not values:
        return None, None, "missing C1C_AD_TEXT headers"
    headers = _header_map(list(values[0]))
    missing = [header for header in REQUIRED_HEADERS if header not in headers]
    if missing:
        return None, headers, f"missing C1C_AD_TEXT header {missing[0]}"
    row_values = (
        list(values[config.text_row - 1]) if len(values) >= config.text_row else []
    )
    row: dict[str, str] = {}
    for header, col in headers.items():
        row[header] = str(row_values[col - 1]).strip() if len(row_values) >= col else ""
    return row, headers, None


def _write_status(
    sheet_id: str,
    config: C1CAdConfig,
    headers: Mapping[str, int],
    updates: Mapping[str, str],
) -> None:
    worksheet = sheets_core.get_worksheet(sheet_id, config.text_tab)
    cells = []
    for header, value in updates.items():
        col = headers.get(header)
        if col is None:
            continue
        cells.append(
            {"range": f"{_column_letter(col)}{config.text_row}", "values": [[value]]}
        )
    if cells:
        sheets_core.call_with_backoff(worksheet.batch_update, cells)


def _normalize_header(label: str) -> str:
    return " ".join(str(label or "").strip().lower().replace("_", " ").split())


def _cell(row: Sequence[Any], index: int | None) -> str:
    if index is None:
        return ""
    return str(row[index]).strip() if 0 <= index < len(row) else ""


def _parse_int(value: Any) -> int:
    try:
        return int(str(value or "").strip())
    except Exception:
        return 0


def _stats_header_map(rows: Sequence[Sequence[Any]]) -> dict[str, int]:
    required = {_normalize_header(header) for header in STATISTICS_REQUIRED_HEADERS}
    best: dict[str, int] = {}
    best_count = 0
    for row in rows:
        headers: dict[str, int] = {}
        for idx, cell in enumerate(row):
            key = _normalize_header(str(cell or ""))
            if key:
                headers[key] = idx
        count = len(required.intersection(headers))
        if count > best_count:
            best = headers
            best_count = count
        if count == len(required):
            return headers
    return best


def _find_bracket_details_start(rows: Sequence[Sequence[Any]]) -> int:
    for idx, row in enumerate(rows):
        if any(str(cell or "").strip().lower() == "bracket details" for cell in row):
            return idx
    return -1


def _open_clans_by_bracket(rows: Sequence[Sequence[Any]]) -> dict[str, list[str]]:
    headers = _stats_header_map(rows)
    missing = [
        header
        for header in STATISTICS_REQUIRED_HEADERS
        if _normalize_header(header) not in headers
    ]
    if missing:
        raise ValueError(f"Statistics headers missing {missing[0]}")

    start = _find_bracket_details_start(rows)
    if start < 0:
        raise ValueError("Statistics Bracket Details section missing")

    h1_idx = headers[_normalize_header("h1_headline")]
    h2_idx = headers[_normalize_header("h2_headline")]
    key_idx = headers[_normalize_header("key")]
    open_idx = headers[_normalize_header("open_spots")]

    current_bracket = ""
    result: dict[str, list[str]] = {}
    for row in rows[start + 1 :]:
        if not any(str(cell or "").strip() for cell in row):
            current_bracket = ""
            continue

        section = _cell(row, h1_idx)
        bracket = _cell(row, h2_idx)
        clan = _cell(row, key_idx)
        if (
            section
            and section.lower() != "bracket details"
            and not bracket
            and not clan
        ):
            break
        if bracket and not clan:
            current_bracket = bracket
            result.setdefault(current_bracket, [])
            continue
        if current_bracket and clan and _parse_int(_cell(row, open_idx)) > 0:
            result.setdefault(current_bracket, []).append(clan)
    return result


def _split_brackets(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def _get_reports_tab_name_required() -> str:
    tab_name = recruitment.get_config_value("REPORTS_TAB", None)
    if not str(tab_name or "").strip():
        raise ValueError("reports tab config missing")
    return str(tab_name).strip()


def _resolve_placeholder_text(
    *,
    placeholder: str,
    row: Mapping[str, str],
    open_by_bracket: Mapping[str, Sequence[str]],
    empty_text: str,
) -> tuple[str, int]:
    mapping_header = PLACEHOLDER_CONFIG[placeholder]
    brackets = _split_brackets(row.get(mapping_header, ""))
    if not brackets:
        log.warning(
            "⚠️ C1C ad placeholder skipped: missing bracket mapping placeholder=%s",
            placeholder,
        )
        return empty_text, 0

    clans: list[str] = []
    seen: set[str] = set()
    bracket_names = set(brackets)
    for bracket, open_clans in open_by_bracket.items():
        if bracket not in bracket_names:
            continue
        for clan in open_clans:
            if clan not in seen:
                clans.append(clan)
                seen.add(clan)
    if not clans:
        return empty_text, 0
    return f"Open right now: **{', '.join(clans)}**", len(clans)


async def _resolve_dynamic_placeholders(ad_text: str, row: Mapping[str, str]) -> str:
    present = [
        placeholder
        for placeholder, token in PLACEHOLDER_TOKENS.items()
        if token in ad_text
    ]
    if not present:
        return ad_text

    empty_text = str(row.get(OPEN_SPOTS_EMPTY_TEXT_HEADER, "")).strip()
    if not empty_text:
        raise ValueError("open_spots_empty_text empty")

    tab_name = _get_reports_tab_name_required()
    try:
        stats_rows = await afetch_reports_tab(tab_name)
    except Exception as exc:
        raise ValueError("Statistics tab read failed") from exc
    open_by_bracket = _open_clans_by_bracket(stats_rows or [])

    resolved = ad_text
    counts = {"endgame": 0, "lategame": 0, "midgame": 0, "early": 0}
    for placeholder in present:
        replacement, count = _resolve_placeholder_text(
            placeholder=placeholder,
            row=row,
            open_by_bracket=open_by_bracket,
            empty_text=empty_text,
        )
        resolved = resolved.replace(PLACEHOLDER_TOKENS[placeholder], replacement)
        counts[placeholder.removeprefix("OPEN_SPOTS_").lower()] = count
    log.info(
        "✅ C1C ad placeholders resolved: endgame=%s lategame=%s midgame=%s early=%s",
        counts["endgame"],
        counts["lategame"],
        counts["midgame"],
        counts["early"],
    )
    return resolved


def _parse_timestamp(value: str) -> dt.datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _due_for_refresh(
    row: Mapping[str, str], refresh_days: int, now: dt.datetime
) -> bool:
    last = _parse_timestamp(row.get("last_posted_at_utc", ""))
    if last is None:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=dt.timezone.utc)
    return now - last >= dt.timedelta(days=refresh_days)


async def _resolve_thread(bot: discord.Client, thread_id: int) -> Any | None:
    channel = bot.get_channel(thread_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(thread_id)
        except Exception:
            return None
    if not hasattr(channel, "send"):
        return None
    return channel


async def _delete_old_messages(channel: Any, row: Mapping[str, str]) -> None:
    for key in ("last_image_message_id", "last_text_message_id"):
        raw = str(row.get(key, "")).strip()
        if not raw:
            continue
        try:
            message_id = int(raw)
        except ValueError:
            log.warning("⚠️ C1C ad warning: invalid old message id %s=%s", key, raw)
            continue
        try:
            message = await channel.fetch_message(message_id)
            await message.delete()
        except discord.NotFound:
            log.warning(
                "⚠️ C1C ad warning: old message already deleted message_id=%s",
                message_id,
            )
        except Exception:
            log.exception(
                "⚠️ C1C ad warning: failed to delete old message message_id=%s",
                message_id,
            )


async def run_c1c_ad_job(
    bot: discord.Client, *, trigger: str = "scheduled", force: bool = False
) -> C1CAdResult:
    if not feature_flags.is_enabled(FEATURE_KEY):
        log.info("⚠️ C1C ad skipped: feature toggle off")
        return C1CAdResult("skipped", "feature toggle off")

    config, error = _resolve_config()
    if config is None:
        log.warning("⚠️ C1C ad skipped: %s", error)
        return C1CAdResult("skipped", str(error))
    sheet_id, error = _get_sheet_id()
    if sheet_id is None:
        log.warning("⚠️ C1C ad skipped: %s", error)
        return C1CAdResult("skipped", str(error))

    row, headers, error = _read_text_row(sheet_id, config)
    if row is None or headers is None:
        log.warning("⚠️ C1C ad skipped: %s", error)
        return C1CAdResult("skipped", str(error))

    now = _utc_now()
    if not force and not _due_for_refresh(row, config.refresh_days, now):
        log.info("⚠️ C1C ad skipped: refresh not due")
        return C1CAdResult("skipped", "refresh not due")

    def fail(reason: str) -> C1CAdResult:
        _write_status(
            sheet_id,
            config,
            headers,
            {
                "last_post_status": "failed",
                "last_post_error": reason,
                "updated_at_utc": _timestamp(now),
            },
        )
        log.error("❌ C1C ad failed: %s", reason)
        return C1CAdResult("failed", reason)

    def fail_resolved_text_too_long(char_count: int) -> C1CAdResult:
        _write_status(
            sheet_id,
            config,
            headers,
            {
                "last_post_status": "failed",
                "last_post_error": RESOLVED_TEXT_TOO_LONG_ERROR,
                "updated_at_utc": _timestamp(now),
            },
        )
        log.error(
            "❌ C1C ad failed: resolved ad text exceeds Discord limit chars=%s limit=%s",
            char_count,
            DISCORD_TEXT_LIMIT,
        )
        return C1CAdResult("failed", RESOLVED_TEXT_TOO_LONG_ERROR)

    ad_text = str(row.get("ad_text", "")).strip()
    if not ad_text:
        return fail("ad_text empty")

    try:
        ad_text = await _resolve_dynamic_placeholders(ad_text, row)
    except ValueError as exc:
        return fail(str(exc))

    resolved_text_length = len(ad_text)
    if resolved_text_length > DISCORD_TEXT_LIMIT:
        return fail_resolved_text_too_long(resolved_text_length)

    if ":" not in config.image_range:
        return fail("image range invalid")
    try:
        gid = get_tab_gid(sheet_id, config.image_tab)
        png_bytes = await export_pdf_as_png(
            sheet_id,
            gid,
            config.image_range,
            log_context={
                "label": "[C1C_AD]",
                "tab": config.image_tab,
                "range": config.image_range,
            },
            fit_range_to_one_page=True,
            fail_on_multi_page=True,
            crop_to_content=False,
        )
    except ImageExportError as exc:
        return fail(str(exc))
    except Exception:
        log.exception("❌ C1C ad failed: image render exception")
        return fail("image render failed")
    if not png_bytes:
        return fail("image render failed")

    channel = await _resolve_thread(bot, config.target_thread_id)
    if channel is None:
        reason = f"target thread not found thread_id={config.target_thread_id}"
        _write_status(
            sheet_id,
            config,
            headers,
            {
                "last_post_status": "skipped",
                "last_post_error": reason,
                "updated_at_utc": _timestamp(now),
            },
        )
        log.warning("⚠️ C1C ad skipped: %s", reason)
        return C1CAdResult("skipped", reason)

    await _delete_old_messages(channel, row)
    try:
        text_message = await channel.send(ad_text)
    except Exception:
        log.exception("❌ C1C ad failed: Discord text post failed")
        return fail("Discord post failed")

    try:
        image_message = await channel.send(
            file=discord.File(io.BytesIO(png_bytes), filename="c1c_recruitment_ad.png")
        )
    except Exception:
        log.exception("❌ C1C ad failed: Discord image post failed")
        try:
            await text_message.delete()
        except Exception:
            log.warning("⚠️ C1C ad cleanup failed: new text message delete failed")
        return fail("Discord post failed")

    stamp = _timestamp(now)
    _write_status(
        sheet_id,
        config,
        headers,
        {
            "last_posted_at_utc": stamp,
            "last_image_message_id": str(getattr(image_message, "id", "")),
            "last_text_message_id": str(getattr(text_message, "id", "")),
            "last_post_status": "success",
            "last_post_error": "",
            "updated_at_utc": stamp,
        },
    )
    channel_name = getattr(channel, "name", str(config.target_thread_id))
    log.info("✅ C1C ad refreshed: text + image posted to #%s", channel_name)
    return C1CAdResult("success", "text + image posted", posted=True)


__all__ = ["C1CAdConfig", "C1CAdResult", "run_c1c_ad_job"]
