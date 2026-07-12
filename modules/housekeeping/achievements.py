from __future__ import annotations

import asyncio
import datetime as dt
import io
import logging
from dataclasses import dataclass
import discord

from shared.sheets import async_core, recruitment
from shared.sheets.export_utils import ImageExportError, export_pdf_as_png, get_tab_gid

log = logging.getLogger("c1c.housekeeping.achievements")

BASE_CONFIG_KEYS: tuple[str, ...] = (
    "achievement_tab",
    "achievement_post_channel_id",
    "achievement_range_count",
)


@dataclass(frozen=True)
class AchievementRangeConfig:
    index: int
    range_key: str
    message_id_key: str
    cell_range: str
    message_id: int | None


@dataclass(frozen=True)
class AchievementsConfig:
    tab: str
    channel_id: int
    ranges: tuple[AchievementRangeConfig, ...]

    @property
    def message_ids(self) -> tuple[int | None, ...]:
        return tuple(item.message_id for item in self.ranges)


@dataclass(frozen=True)
class AchievementsResult:
    status: str
    message: str
    message_ids: tuple[int, ...] = ()


class AchievementsConfigError(RuntimeError):
    pass


def _column_label(index: int) -> str:
    label = ""
    value = index
    while value > 0:
        value, rem = divmod(value - 1, 26)
        label = chr(ord("A") + rem) + label
    return label


def _normalize_key(value: object) -> str:
    return str(value or "").strip().lower()


def _header_map(header: list[object]) -> dict[str, int]:
    return {
        _normalize_key(value): index
        for index, value in enumerate(header)
        if _normalize_key(value)
    }


async def _read_config_entries() -> tuple[dict[str, tuple[str, int, int]], str, str]:
    sheet_id = recruitment.get_recruitment_sheet_id()
    tab_name = recruitment.get_config_tab_name()
    matrix = await async_core.afetch_values(sheet_id, tab_name)
    if not matrix:
        raise AchievementsConfigError("Recruitment Config worksheet is empty.")
    headers = _header_map(list(matrix[0]))
    if "key" not in headers:
        raise AchievementsConfigError(
            "Recruitment Config worksheet missing Key column."
        )
    key_col = headers["key"]
    value_col = headers.get("value", key_col + 1)
    entries: dict[str, tuple[str, int, int]] = {}
    for row_index, row in enumerate(matrix[1:], start=2):
        key = _normalize_key(row[key_col] if key_col < len(row) else "")
        if not key:
            continue
        raw_value = row[value_col] if value_col < len(row) else ""
        entries[key] = (str(raw_value or "").strip(), row_index, value_col + 1)
    return entries, sheet_id, tab_name


async def _write_config_value(key: str, value: str) -> None:
    entries, sheet_id, tab_name = await _read_config_entries()
    normalized = _normalize_key(key)
    if normalized not in entries:
        raise AchievementsConfigError(
            f"Missing required Config key {key}; create a blank row before publishing."
        )
    _, row_index, value_col = entries[normalized]
    worksheet = await async_core.aget_worksheet(sheet_id, tab_name)
    target = f"{_column_label(value_col)}{row_index}"
    await async_core.acall_with_backoff(
        worksheet.update,
        target,
        [[value]],
        value_input_option="RAW",
    )


def _require_config_key(entries: dict[str, tuple[str, int, int]], key: str) -> str:
    if key not in entries:
        raise AchievementsConfigError(f"Missing required Config key(s): {key}")
    return entries[key][0]


async def resolve_config(*, require_message_id: bool) -> AchievementsConfig:
    entries, _, _ = await _read_config_entries()
    missing_keys = [key for key in BASE_CONFIG_KEYS if key not in entries]
    if missing_keys:
        raise AchievementsConfigError(
            "Missing required Config key(s): " + ", ".join(missing_keys)
        )

    tab = entries["achievement_tab"][0]
    channel_value = entries["achievement_post_channel_id"][0]
    count_value = entries["achievement_range_count"][0]
    blank_base = [
        key
        for key, value in (
            ("achievement_tab", tab),
            ("achievement_post_channel_id", channel_value),
            ("achievement_range_count", count_value),
        )
        if not value
    ]
    if blank_base:
        raise AchievementsConfigError(
            "Blank required Config key(s): " + ", ".join(blank_base)
        )
    try:
        channel_id = int(channel_value)
    except ValueError as exc:
        raise AchievementsConfigError(
            "Config key achievement_post_channel_id must be a Discord snowflake ID."
        ) from exc
    try:
        range_count = int(count_value)
    except ValueError as exc:
        raise AchievementsConfigError(
            "Config key achievement_range_count must be a positive integer."
        ) from exc
    if range_count <= 0:
        raise AchievementsConfigError(
            "Config key achievement_range_count must be a positive integer."
        )

    ranges: list[AchievementRangeConfig] = []
    for index in range(1, range_count + 1):
        range_key = f"achievement_range_{index}"
        message_id_key = f"achievement_post_message_id_{index}"
        cell_range = _require_config_key(entries, range_key)
        message_value = _require_config_key(entries, message_id_key)
        if not cell_range:
            raise AchievementsConfigError(f"Config key {range_key} must not be blank.")
        message_id: int | None = None
        if message_value:
            try:
                message_id = int(message_value)
            except ValueError as exc:
                raise AchievementsConfigError(
                    f"Config key {message_id_key} is invalid. Run !achievements publish."
                ) from exc
        elif require_message_id:
            raise AchievementsConfigError(
                f"{message_id_key} is missing. Run !achievements publish."
            )
        ranges.append(
            AchievementRangeConfig(
                index=index,
                range_key=range_key,
                message_id_key=message_id_key,
                cell_range=cell_range,
                message_id=message_id,
            )
        )
    return AchievementsConfig(tab=tab, channel_id=channel_id, ranges=tuple(ranges))


async def resolve_message_target(
    bot: discord.Client, channel_id: int
) -> discord.abc.Messageable | None:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)  # type: ignore[attr-defined]
        except Exception:
            return None
    return (
        channel
        if hasattr(channel, "send") or hasattr(channel, "fetch_message")
        else None
    )


async def render_achievement_files(config: AchievementsConfig) -> list[discord.File]:
    sheet_id = recruitment.get_recruitment_sheet_id()
    loop = asyncio.get_running_loop()
    gid = await loop.run_in_executor(None, get_tab_gid, sheet_id, config.tab)
    if gid is None:
        raise AchievementsConfigError(
            f"Configured achievement_tab {config.tab!r} could not be resolved."
        )

    files: list[discord.File] = []
    for item in config.ranges:
        if ":" not in item.cell_range:
            raise AchievementsConfigError(
                f"Config key {item.range_key} must be an A1 range like A1:Z99."
            )
        try:
            png = await export_pdf_as_png(
                sheet_id,
                gid,
                item.cell_range,
                log_context={
                    "label": item.range_key,
                    "tab": config.tab,
                    "range": item.cell_range,
                },
                fit_range_to_one_page=True,
                fail_on_multi_page=True,
                crop_to_content=True,
            )
        except ImageExportError as exc:
            raise AchievementsConfigError(str(exc)) from exc
        if not png:
            raise AchievementsConfigError(
                f"Failed to render {item.range_key} as a one-page image."
            )
        files.append(
            discord.File(io.BytesIO(png), filename=f"achievements_{item.index}.png")
        )
    return files


def build_message_content(index: int) -> str:
    if index != 1:
        return ""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"# Achievements\n-# Last updated {stamp}"


async def publish_achievements(bot: discord.Client) -> AchievementsResult:
    config = await resolve_config(require_message_id=False)
    channel = await resolve_message_target(bot, config.channel_id)
    if channel is None or not hasattr(channel, "send"):
        return AchievementsResult(
            "error",
            "Configured achievement_post_channel_id is not a messageable channel/thread.",
        )
    files = await render_achievement_files(config)
    messages = []
    for item, file in zip(config.ranges, files, strict=True):
        messages.append(
            await channel.send(content=build_message_content(item.index), files=[file])  # type: ignore[attr-defined]
        )
    try:
        for message, item in zip(messages, config.ranges, strict=True):
            try:
                await _write_config_value(item.message_id_key, str(message.id))
            except Exception as exc:
                raise AchievementsConfigError(f"{item.message_id_key}: {exc}") from exc
    except Exception as exc:
        ids = tuple(message.id for message in messages)
        log.exception(
            "achievement messages posted but Config writeback failed",
            extra={"message_ids": ids},
        )
        return AchievementsResult(
            "error",
            f"Posted achievements messages {', '.join(str(i) for i in ids)}, but failed to write Config message IDs: {exc}",
            message_ids=ids,
        )
    ids = tuple(message.id for message in messages)
    return AchievementsResult(
        "success",
        f"Published achievements messages {', '.join(str(i) for i in ids)}.",
        message_ids=ids,
    )


async def refresh_achievements(bot: discord.Client) -> AchievementsResult:
    config = await resolve_config(require_message_id=True)
    channel = await resolve_message_target(bot, config.channel_id)
    if channel is None or not hasattr(channel, "fetch_message"):
        return AchievementsResult(
            "error",
            "Configured achievement_post_channel_id is not fetchable. Run !achievements publish.",
        )
    messages = []
    for item in config.ranges:
        try:
            messages.append(await channel.fetch_message(item.message_id))  # type: ignore[attr-defined,arg-type]
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return AchievementsResult(
                "error",
                f"{item.message_id_key} is missing, invalid, deleted, or not fetchable. Run !achievements publish.",
            )
    files = await render_achievement_files(config)
    for item, message, file in zip(config.ranges, messages, files, strict=True):
        await message.edit(
            content=build_message_content(item.index), attachments=[file]
        )
    ids = tuple(message.id for message in messages)
    return AchievementsResult(
        "success",
        f"Refreshed achievements messages {', '.join(str(i) for i in ids)}.",
        ids,
    )


__all__ = [
    "AchievementRangeConfig",
    "AchievementsConfig",
    "AchievementsConfigError",
    "AchievementsResult",
    "publish_achievements",
    "refresh_achievements",
    "render_achievement_files",
    "resolve_config",
]
