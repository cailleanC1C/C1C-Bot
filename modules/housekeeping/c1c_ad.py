from __future__ import annotations

import datetime as dt
import io
import logging
from dataclasses import dataclass
from typing import Any, Mapping

import discord

from modules.common import feature_flags
from shared.sheets import core as sheets_core
from shared.sheets import recruitment
from shared.sheets.export_utils import export_pdf_as_png, get_tab_gid

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

    ad_text = str(row.get("ad_text", "")).strip()
    if not ad_text:
        return fail("ad_text empty")

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
        )
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
        image_message = await channel.send(
            file=discord.File(io.BytesIO(png_bytes), filename="c1c_recruitment_ad.png")
        )
        text_message = await channel.send(ad_text)
    except Exception:
        log.exception("❌ C1C ad failed: Discord post failed")
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
    log.info("✅ C1C ad refreshed: image + text posted to #%s", channel_name)
    return C1CAdResult("success", "image + text posted", posted=True)


__all__ = ["C1CAdConfig", "C1CAdResult", "run_c1c_ad_job"]
