from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import re
from typing import Any, Mapping, Sequence
from urllib.parse import urlparse

import discord
from discord.ext import commands

from shared.config import get_milestones_sheet_id
from shared.sheets import milestones_config
from shared.sheets.async_core import (
    acall_with_backoff,
    afetch_records,
    afetch_values,
    aget_worksheet,
)

_FORUM_POSTS_KEY = "PROGRESS_FORUM_POSTS_TAB"
_GUIDES_KEY = "PROGRESS_GUIDES_TAB"
_FAQ_KEY = "PROGRESS_FAQ_TAB"
_ASSETS_KEY = "PROGRESS_ASSETS_TAB"
_MESSAGE_ID_COLUMN = "guide_panel_message_id"
_EMBED_LIMIT = 3900
_EMBED_TITLE_LIMIT = 256
_EMBED_DESCRIPTION_LIMIT = 4096
_FIELD_LIMIT = 900
_BUTTON_LABEL_LIMIT = 80
_URL_RE = re.compile(r"https?://\S+")
_FAQ_CUSTOM_ID_PREFIX = "progressguides:faq:"
_PERSISTENT_FAQ_CATEGORIES = ("ARB", "RAM", "MAR", "FW_N", "FW_H")
_DATA_CACHE: "ProgressGuideData | None" = None
_DATA_CACHE_LOCK = asyncio.Lock()


def _text(value: object) -> str:
    return str(value or "").strip()


def _truthy(value: object) -> bool:
    return _text(value).casefold() in {"1", "true", "yes", "y", "on", "enabled"}


def _sort_num(value: object) -> float:
    try:
        return float(_text(value) or 0)
    except ValueError:
        return 0


def _int_or_none(value: object) -> int | None:
    raw = _text(value)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _safe_url(value: object) -> str | None:
    raw = _text(value)
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        return raw
    return None


@dataclass(slots=True)
class ForumPost:
    row_number: int
    category: str
    label: str
    guide_title: str
    faq_title: str
    faq_description: str
    faq_button_label: str
    help_button_label: str
    guide_channel_id: int | None
    guide_thread_id: int | None
    guide_panel_message_id: int | None
    help_post_url: str
    guide_asset_key: str
    questions_enabled: bool
    sort_order: float
    enabled: bool

    @classmethod
    def from_row(cls, row_number: int, row: Mapping[str, object]) -> "ForumPost":
        return cls(
            row_number=row_number,
            category=_text(row.get("category")),
            label=_text(row.get("label")),
            guide_title=_text(row.get("guide_title")),
            faq_title=_text(row.get("faq_title")),
            faq_description=_text(row.get("faq_description")),
            faq_button_label=_text(row.get("faq_button_label")),
            help_button_label=_text(row.get("help_button_label")),
            guide_channel_id=_int_or_none(row.get("guide_channel_id")),
            guide_thread_id=_int_or_none(row.get("guide_thread_id")),
            guide_panel_message_id=_int_or_none(row.get("guide_panel_message_id")),
            help_post_url=_text(row.get("help_post_url")),
            guide_asset_key=_text(row.get("guide_asset_key")),
            questions_enabled=_truthy(row.get("questions_enabled")),
            sort_order=_sort_num(row.get("sort_order")),
            enabled=_truthy(row.get("enabled")),
        )


@dataclass(slots=True)
class PublishSummary:
    created: int = 0
    refreshed: int = 0
    skipped: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ProgressGuideData:
    posts: list[ForumPost]
    guides_by_category: dict[str, list[Mapping[str, object]]]
    faq_by_category: dict[str, list[Mapping[str, object]]]
    assets_by_category_key: dict[tuple[str, str], Mapping[str, object]]
    forum_posts_tab: str


def get_cached_progress_guide_data() -> ProgressGuideData | None:
    return _DATA_CACHE


def set_progress_guide_cache(data: ProgressGuideData) -> None:
    global _DATA_CACHE
    _DATA_CACHE = data


def clear_progress_guide_cache() -> None:
    global _DATA_CACHE
    _DATA_CACHE = None


async def get_or_load_progress_guide_data() -> ProgressGuideData:
    cached = get_cached_progress_guide_data()
    if cached is not None:
        return cached
    async with _DATA_CACHE_LOCK:
        cached = get_cached_progress_guide_data()
        if cached is not None:
            return cached
        data = await load_progress_guide_data()
        set_progress_guide_cache(data)
        return data


async def load_progress_guide_data() -> ProgressGuideData:
    sheet_id = get_milestones_sheet_id().strip()
    if not sheet_id:
        raise RuntimeError("MILESTONES_SHEET_ID not set")
    forum_tab = await milestones_config.arequire_value(_FORUM_POSTS_KEY)
    guides_tab = await milestones_config.arequire_value(_GUIDES_KEY)
    faq_tab = await milestones_config.arequire_value(_FAQ_KEY)
    assets_tab = await milestones_config.arequire_value(_ASSETS_KEY)
    post_rows, guide_rows, faq_rows, asset_rows = await _gather_rows(
        sheet_id, forum_tab, guides_tab, faq_tab, assets_tab
    )
    posts = [ForumPost.from_row(i, r) for i, r in enumerate(post_rows, start=2)]
    guides: dict[str, list[Mapping[str, object]]] = {}
    for row in guide_rows:
        if _truthy(row.get("enabled")):
            guides.setdefault(_text(row.get("category")), []).append(row)
    faq: dict[str, list[Mapping[str, object]]] = {}
    for row in faq_rows:
        if _truthy(row.get("enabled")):
            faq.setdefault(_text(row.get("category")), []).append(row)
    assets: dict[tuple[str, str], Mapping[str, object]] = {}
    for row in asset_rows:
        if _truthy(row.get("enabled")):
            assets[(_text(row.get("category")), _text(row.get("asset_key")))] = row
    for bucket in (guides, faq):
        for rows in bucket.values():
            rows.sort(
                key=lambda r: (
                    _sort_num(r.get("sort_order")),
                    _text(r.get("title") or r.get("question")),
                )
            )
    posts.sort(key=lambda p: (p.sort_order, p.label, p.category))
    return ProgressGuideData(posts, guides, faq, assets, forum_tab)


async def _gather_rows(sheet_id: str, *tabs: str) -> tuple[list[dict[str, Any]], ...]:
    import asyncio

    return tuple(await asyncio.gather(*(afetch_records(sheet_id, tab) for tab in tabs)))  # type: ignore[return-value]


def _strip_visible_urls(value: object) -> str:
    return _URL_RE.sub("", _text(value)).strip()


def _limit_text(value: object, limit: int) -> str:
    return _text(value)[:limit]


def _embed_title(value: object) -> str:
    return _limit_text(value, _EMBED_TITLE_LIMIT)


def _embed_description(value: object) -> str | None:
    description = _limit_text(value, _EMBED_DESCRIPTION_LIMIT)
    return description or None


def _button_label(value: object) -> str:
    return _limit_text(value, _BUTTON_LABEL_LIMIT)


def _post_for_category(category: str, data: ProgressGuideData) -> ForumPost | None:
    return next((post for post in data.posts if post.category == category), None)


def _faq_title_for_category(category: str, data: ProgressGuideData) -> str:
    post = _post_for_category(category, data)
    if post is None:
        return _embed_title(f"{category} FAQ")
    base = post.guide_title or post.label or post.category
    return _embed_title(post.faq_title or f"{base} FAQ")


def build_faq_embed(category: str, data: ProgressGuideData) -> discord.Embed | None:
    rows = data.faq_by_category.get(category, [])
    if not rows:
        return None
    post = _post_for_category(category, data)
    embed = discord.Embed(
        title=_faq_title_for_category(category, data),
        description=_embed_description(post.faq_description) if post else None,
        color=discord.Color.blurple(),
    )
    ordered = sorted(
        rows, key=lambda r: (_sort_num(r.get("sort_order")), _text(r.get("question")))
    )
    for row in ordered[:25]:
        question = _strip_visible_urls(row.get("question"))[:256]
        answer = _strip_visible_urls(row.get("answer"))[:_FIELD_LIMIT]
        if not question or not answer:
            continue
        embed.add_field(name=question, value=answer, inline=False)
    return embed if embed.fields else None


class ProgressGuideFAQButton(discord.ui.Button):
    def __init__(self, category: str, label: str = "FAQ") -> None:
        self.category = category
        super().__init__(
            label=_button_label(label or "FAQ"),
            style=discord.ButtonStyle.secondary,
            custom_id=f"{_FAQ_CUSTOM_ID_PREFIX}{category}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await get_or_load_progress_guide_data()
            embed = build_faq_embed(self.category, data)
            if embed is None:
                embed = discord.Embed(
                    title=f"{self.category} FAQ",
                    description="No FAQ entries are currently available.",
                    color=discord.Color.blurple(),
                )
        except Exception:
            embed = discord.Embed(
                title="Progress guide FAQ unavailable",
                description="I couldn’t load that FAQ right now. Please try again later.",
                color=discord.Color.red(),
            )
        await interaction.followup.send(embed=embed, ephemeral=True)


class ProgressGuideFAQPersistentView(discord.ui.View):
    def __init__(self, categories: Sequence[str] = _PERSISTENT_FAQ_CATEGORIES) -> None:
        super().__init__(timeout=None)
        for category in categories:
            self.add_item(ProgressGuideFAQButton(category))


def build_guide_embed(post: ForumPost, data: ProgressGuideData) -> discord.Embed | None:
    guide_rows = data.guides_by_category.get(post.category, [])
    if not guide_rows:
        return None
    embed = discord.Embed(
        title=_embed_title(post.guide_title or post.label or post.category),
        color=discord.Color.blurple(),
    )
    used = 0
    for row in guide_rows[:10]:
        title = _text(row.get("title")) or _text(row.get("section_key")) or "Guide"
        body = _text(row.get("body"))
        if not body:
            continue
        value = _strip_visible_urls(body)[:_FIELD_LIMIT]
        if not value:
            continue
        if used + len(title) + len(value) > _EMBED_LIMIT:
            break
        embed.add_field(name=title[:256], value=value, inline=False)
        used += len(title) + len(value)
    asset = data.assets_by_category_key.get((post.category, post.guide_asset_key))
    if asset:
        url = _safe_url(asset.get("asset_url"))
        asset_type = _text(asset.get("asset_type")).casefold()
        if url and (not asset_type or asset_type in {"image", "banner", "thumbnail"}):
            embed.set_image(url=url)
    return embed


def build_guide_view(
    post: ForumPost, data: ProgressGuideData
) -> discord.ui.View | None:
    view = discord.ui.View(timeout=None)
    added = False
    help_url = _safe_url(post.help_post_url)
    if data.faq_by_category.get(post.category):
        view.add_item(
            ProgressGuideFAQButton(post.category, post.faq_button_label or "FAQ")
        )
        added = True
    if post.questions_enabled and help_url:
        view.add_item(
            discord.ui.Button(
                label=_button_label(post.help_button_label or "Ask in Help"),
                style=discord.ButtonStyle.link,
                url=help_url,
            )
        )
        added = True
    return view if added else None


async def publish_or_refresh(bot: commands.Bot, *, refresh: bool) -> PublishSummary:
    data = await load_progress_guide_data()
    set_progress_guide_cache(data)
    sheet_id = get_milestones_sheet_id().strip()
    worksheet = await aget_worksheet(sheet_id, data.forum_posts_tab)
    header = await _load_header(sheet_id, data.forum_posts_tab)
    col = _column_label(_header_index(header, _MESSAGE_ID_COLUMN))
    summary = PublishSummary()
    for post in data.posts:
        label = post.label or post.category or f"row {post.row_number}"
        if not post.enabled:
            summary.skipped.append(f"{label}: disabled")
            continue
        target_id = post.guide_thread_id or post.guide_channel_id
        if target_id is None:
            summary.skipped.append(f"{label}: missing guide destination")
            continue
        embed = build_guide_embed(post, data)
        if embed is None:
            summary.skipped.append(f"{label}: missing guide content")
            continue
        channel = await _resolve_messageable(bot, target_id)
        if channel is None:
            summary.failures.append(f"{label}: invalid guide destination {target_id}")
            continue
        view = build_guide_view(post, data)
        try:
            if post.guide_panel_message_id:
                message = None
                try:
                    message = await channel.fetch_message(post.guide_panel_message_id)  # type: ignore[attr-defined]
                except discord.NotFound:
                    message = None
                except Exception:
                    raise
                if message is not None:
                    await message.edit(embed=embed, view=view)
                    summary.refreshed += 1
                    continue
                if not refresh:
                    summary.skipped.append(
                        f"{label}: stored panel missing; run refresh to recreate"
                    )
                    continue
            message = await channel.send(embed=embed, view=view)  # type: ignore[attr-defined]
            await acall_with_backoff(
                worksheet.update,
                f"{col}{post.row_number}",
                [[str(message.id)]],
                value_input_option="RAW",
            )
            summary.created += 1
        except Exception as exc:
            summary.failures.append(f"{label}: {type(exc).__name__}: {exc}")
    return summary


async def _load_header(sheet_id: str, tab: str) -> list[str]:
    values = await afetch_values(sheet_id, tab)
    return [str(c or "").strip().lower() for c in (values[0] if values else [])]


def _header_index(header: Sequence[str], name: str) -> int:
    try:
        return header.index(name.lower())
    except ValueError as exc:
        raise RuntimeError(f"missing required header: {name}") from exc


def _column_label(index: int) -> str:
    index += 1
    out = ""
    while index:
        index, rem = divmod(index - 1, 26)
        out = chr(65 + rem) + out
    return out


async def _resolve_messageable(
    bot: commands.Bot, channel_id: int
) -> discord.abc.Messageable | None:
    channel = bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except Exception:
            return None
    if isinstance(channel, discord.abc.Messageable):
        return channel
    return None
