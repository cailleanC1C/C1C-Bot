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

REQUIRED_CONFIG_KEYS: tuple[str, ...] = (
    "achievement_tab",
    "achievement_range",
    "achievement_champion_range",
    "achievement_post_channel_id",
    "achievement_post_message_id_1",
    "achievement_post_message_id_2",
)

RANGE_CONFIG_KEYS: tuple[tuple[str, str, str], ...] = (
    ("achievement_range", "achievements.png", "achievement_post_message_id_1"),
    (
        "achievement_champion_range",
        "achievement_champions.png",
        "achievement_post_message_id_2",
    ),
)


@dataclass(frozen=True)
class AchievementsConfig:
    tab: str
    achievement_range: str
    champion_range: str
    channel_id: int
    message_ids: tuple[int | None, int | None]


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


async def resolve_config(*, require_message_id: bool) -> AchievementsConfig:
    entries, _, _ = await _read_config_entries()
    missing_keys = [key for key in REQUIRED_CONFIG_KEYS if key not in entries]
    if missing_keys:
        raise AchievementsConfigError(
            "Missing required Config key(s): " + ", ".join(missing_keys)
        )
    values = {key: entries[key][0] for key in REQUIRED_CONFIG_KEYS}
    message_id_keys = {"achievement_post_message_id_1", "achievement_post_message_id_2"}
    blank = [
        key for key, value in values.items() if not value and key not in message_id_keys
    ]
    if blank:
        raise AchievementsConfigError(
            "Blank required Config key(s): " + ", ".join(blank)
        )
    try:
        channel_id = int(values["achievement_post_channel_id"])
    except ValueError as exc:
        raise AchievementsConfigError(
            "Config key achievement_post_channel_id must be a Discord snowflake ID."
        ) from exc

    message_ids: list[int | None] = []
    for key in ("achievement_post_message_id_1", "achievement_post_message_id_2"):
        value = values[key]
        if value:
            try:
                message_ids.append(int(value))
            except ValueError as exc:
                raise AchievementsConfigError(
                    f"Config key {key} is invalid. Run !achievements publish."
                ) from exc
        elif require_message_id:
            raise AchievementsConfigError(
                f"{key} is missing. Run !achievements publish."
            )
        else:
            message_ids.append(None)
    return AchievementsConfig(
        tab=values["achievement_tab"],
        achievement_range=values["achievement_range"],
        champion_range=values["achievement_champion_range"],
        channel_id=channel_id,
        message_ids=(message_ids[0], message_ids[1]),
    )


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
    ranges = {
        "achievement_range": config.achievement_range,
        "achievement_champion_range": config.champion_range,
    }
    for key, filename, _message_id_key in RANGE_CONFIG_KEYS:
        cell_range = ranges[key]
        if ":" not in cell_range:
            raise AchievementsConfigError(
                f"Config key {key} must be an A1 range like A1:Z99."
            )
        try:
            png = await export_pdf_as_png(
                sheet_id,
                gid,
                cell_range,
                log_context={"label": key, "tab": config.tab, "range": cell_range},
                fit_range_to_one_page=True,
                fail_on_multi_page=True,
                crop_to_content=True,
            )
        except ImageExportError as exc:
            raise AchievementsConfigError(str(exc)) from exc
        if not png:
            raise AchievementsConfigError(
                f"Failed to render {key} as a one-page image."
            )
        files.append(discord.File(io.BytesIO(png), filename=filename))
    return files


def build_message_content() -> str:
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
    for file in files:
        messages.append(
            await channel.send(content=build_message_content(), files=[file])  # type: ignore[attr-defined]
        )
    try:
        for message, (_range_key, _filename, message_id_key) in zip(
            messages, RANGE_CONFIG_KEYS, strict=True
        ):
            await _write_config_value(message_id_key, str(message.id))
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
        f"Published achievements messages {ids[0]} and {ids[1]}.",
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
    for message_id, (_range_key, _filename, message_id_key) in zip(
        config.message_ids, RANGE_CONFIG_KEYS, strict=True
    ):
        try:
            messages.append(await channel.fetch_message(message_id))  # type: ignore[attr-defined,arg-type]
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return AchievementsResult(
                "error",
                f"{message_id_key} is missing, invalid, deleted, or not fetchable. Run !achievements publish.",
            )
    files = await render_achievement_files(config)
    for message, file in zip(messages, files, strict=True):
        await message.edit(content=build_message_content(), attachments=[file])
    ids = tuple(message.id for message in messages)
    return AchievementsResult(
        "success",
        f"Refreshed achievements messages {ids[0]} and {ids[1]}.",
        ids,
    )


__all__ = [
    "AchievementsConfig",
    "AchievementsConfigError",
    "AchievementsResult",
    "publish_achievements",
    "refresh_achievements",
    "render_achievement_files",
    "resolve_config",
]
