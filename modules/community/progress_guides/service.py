from __future__ import annotations

import asyncio
from datetime import datetime, timezone
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
from shared.sheets.core import is_rate_limited_error

_FORUM_POSTS_KEY = "PROGRESS_FORUM_POSTS_TAB"
_GUIDES_KEY = "PROGRESS_GUIDES_TAB"
_FAQ_KEY = "PROGRESS_FAQ_TAB"
_ASSETS_KEY = "PROGRESS_ASSETS_TAB"
_CATEGORIES_KEY = "PROGRESS_CATEGORIES_TAB"
_USER_STATE_KEY = "PROGRESS_USER_STATE_TAB"
_MESSAGE_ID_COLUMN = "guide_panel_message_id"
_EMBED_LIMIT = 3900
_EMBED_TITLE_LIMIT = 256
_EMBED_DESCRIPTION_LIMIT = 4096
_FIELD_LIMIT = 900
_BUTTON_LABEL_LIMIT = 80
_URL_RE = re.compile(r"https?://\S+")
_FAQ_CUSTOM_ID_PREFIX = "progressguides:faq:"
_MISSIONS_CUSTOM_ID_PREFIX = "progressguides:missions:"
_MY_PROGRESS_CUSTOM_ID_PREFIX = "progressguides:myprogress:"
_SET_PROGRESS_CUSTOM_ID_PREFIX = "progressguides:setprogress:"
_PERSISTENT_FAQ_CATEGORIES = ("ARB", "RAM", "MAR", "FW_N", "FW_H")
_MISSION_CATEGORIES = ("ARB", "RAM", "MAR")
_MISSIONS_PER_PAGE = 15
_PICKER_OPTIONS_PER_PAGE = 25
_SELECT_LABEL_LIMIT = 100
_DATA_CACHE: "ProgressGuideData | None" = None
_DATA_CACHE_LOCK = asyncio.Lock()
_MISSION_CACHE: dict[str, list["MissionRow"]] = {}
_MISSION_CACHE_LOCKS: dict[str, asyncio.Lock] = {}


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
    mission_list_button_label: str
    mission_list_title: str
    my_progress_button_label: str
    my_progress_title: str
    my_progress_body_template: str
    my_progress_empty_description: str
    my_progress_set_button_label: str
    my_progress_modal_title: str
    my_progress_modal_step_label: str
    my_progress_saved_description: str
    my_progress_invalid_step_description: str
    my_progress_missing_step_description: str
    my_progress_unavailable_description: str
    my_progress_picker_title: str
    my_progress_picker_description: str
    my_progress_chapter_select_placeholder: str
    my_progress_mission_select_placeholder: str
    my_progress_no_missions_description: str
    my_progress_complete_button_label: str
    my_progress_complete_saved_description: str
    my_progress_next_mission_saved_description: str
    my_progress_completed_template: str
    progress_tracking_enabled: bool
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
            mission_list_button_label=_text(row.get("mission_list_button_label")),
            mission_list_title=_text(row.get("mission_list_title")),
            my_progress_button_label=_text(row.get("my_progress_button_label")),
            my_progress_title=_text(row.get("my_progress_title")),
            my_progress_body_template=_text(row.get("my_progress_body_template")),
            my_progress_empty_description=_text(
                row.get("my_progress_empty_description")
            ),
            my_progress_set_button_label=_text(row.get("my_progress_set_button_label")),
            my_progress_modal_title=_text(row.get("my_progress_modal_title")),
            my_progress_modal_step_label=_text(row.get("my_progress_modal_step_label")),
            my_progress_saved_description=_text(
                row.get("my_progress_saved_description")
            ),
            my_progress_invalid_step_description=_text(
                row.get("my_progress_invalid_step_description")
            ),
            my_progress_missing_step_description=_text(
                row.get("my_progress_missing_step_description")
            ),
            my_progress_unavailable_description=_text(
                row.get("my_progress_unavailable_description")
            ),
            my_progress_picker_title=_text(row.get("my_progress_picker_title")),
            my_progress_picker_description=_text(
                row.get("my_progress_picker_description")
            ),
            my_progress_chapter_select_placeholder=_text(
                row.get("my_progress_chapter_select_placeholder")
            ),
            my_progress_mission_select_placeholder=_text(
                row.get("my_progress_mission_select_placeholder")
            ),
            my_progress_no_missions_description=_text(
                row.get("my_progress_no_missions_description")
            ),
            my_progress_complete_button_label=_text(
                row.get("my_progress_complete_button_label")
            ),
            my_progress_complete_saved_description=_text(
                row.get("my_progress_complete_saved_description")
            ),
            my_progress_next_mission_saved_description=_text(
                row.get("my_progress_next_mission_saved_description")
            ),
            my_progress_completed_template=_text(
                row.get("my_progress_completed_template")
            ),
            progress_tracking_enabled=_truthy(row.get("progress_tracking_enabled")),
            guide_channel_id=_int_or_none(row.get("guide_channel_id")),
            guide_thread_id=_int_or_none(row.get("guide_thread_id")),
            guide_panel_message_id=_int_or_none(row.get("guide_panel_message_id")),
            help_post_url=_text(row.get("help_post_url")),
            guide_asset_key=_text(row.get("guide_asset_key")),
            questions_enabled=_truthy(row.get("questions_enabled")),
            sort_order=_sort_num(row.get("sort_order")),
            enabled=_truthy(row.get("enabled")),
        )


@dataclass(init=False, slots=True)
class MissionRow:
    sequence_number: int
    step_index: int
    key: str
    title: str
    text: str

    def __init__(
        self,
        sequence_number: int,
        text: str,
        key: str = "",
        title: str = "",
        step_index: int | None = None,
    ) -> None:
        self.sequence_number = sequence_number
        self.step_index = step_index if step_index is not None else sequence_number
        self.key = key
        self.title = title
        self.text = text

    @property
    def number(self) -> int:
        return self.sequence_number


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
    clear_mission_cache()


def clear_mission_cache() -> None:
    _MISSION_CACHE.clear()
    _MISSION_CACHE_LOCKS.clear()


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


def _mission_title_for_category(category: str, data: ProgressGuideData) -> str:
    post = _post_for_category(category, data)
    if post is None:
        return _embed_title(category)
    return _embed_title(
        post.mission_list_title or post.guide_title or post.label or post.category
    )


def _supports_missions(post: ForumPost) -> bool:
    return post.category in _MISSION_CATEGORIES and bool(
        post.mission_list_button_label or post.mission_list_title
    )


def _supports_my_progress(post: ForumPost) -> bool:
    return (
        post.category in _MISSION_CATEGORIES
        and post.progress_tracking_enabled
        and bool(post.my_progress_button_label)
    )


def _mission_unavailable_embed(description: str) -> discord.Embed:
    return discord.Embed(
        title="Mission list unavailable",
        description=description,
        color=discord.Color.red(),
    )


def _is_quota_failure(exc: BaseException) -> bool:
    if isinstance(exc, milestones_config.MilestonesConfigLoadFailed):
        return True
    return is_rate_limited_error(exc)


def _parse_mission_rows(rows: Sequence[Mapping[str, object]]) -> list[MissionRow]:
    parsed: list[MissionRow] = []
    for order, row in enumerate(rows, start=1):
        text = _strip_visible_urls(row.get("description") or row.get("mission_text"))
        if not text:
            continue
        step_index = _int_or_none(row.get("step_index")) or order
        parsed.append(
            MissionRow(
                sequence_number=len(parsed) + 1,
                step_index=step_index,
                key=_text(row.get("mission_key")),
                title=_text(row.get("title")),
                text=text,
            )
        )
    return parsed


async def _mission_tab_for_category(category: str) -> str | None:
    sheet_id = get_milestones_sheet_id().strip()
    if not sheet_id:
        raise RuntimeError("MILESTONES_SHEET_ID not set")
    categories_tab = await milestones_config.arequire_value(_CATEGORIES_KEY)
    rows = await afetch_records(sheet_id, categories_tab)
    for row in rows:
        if _text(row.get("category")) == category:
            config_key = _text(row.get("mission_tab_config_key"))
            if not config_key:
                return None
            return await milestones_config.arequire_value(config_key)
    return None


async def get_or_load_missions(category: str) -> list[MissionRow]:
    if category in _MISSION_CACHE:
        return _MISSION_CACHE[category]
    lock = _MISSION_CACHE_LOCKS.setdefault(category, asyncio.Lock())
    async with lock:
        if category in _MISSION_CACHE:
            return _MISSION_CACHE[category]
        tab = await _mission_tab_for_category(category)
        if not tab:
            return []
        rows = await afetch_records(get_milestones_sheet_id().strip(), tab)
        missions = _parse_mission_rows(rows)
        _MISSION_CACHE[category] = missions
        return missions


def build_mission_embed(
    category: str, data: ProgressGuideData, missions: Sequence[MissionRow], *, page: int
) -> discord.Embed:
    total = len(missions)
    total_pages = max(1, (total + _MISSIONS_PER_PAGE - 1) // _MISSIONS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * _MISSIONS_PER_PAGE
    shown = missions[start : start + _MISSIONS_PER_PAGE]
    lines = [
        f"Missions {start + 1}-{start + len(shown)} of {total}",
        f"Page {page + 1} / {total_pages}",
        "",
    ]
    for mission in shown:
        lines.append(f"{mission.sequence_number}. {_limit_text(mission.text, 220)}")
    return discord.Embed(
        title=_mission_title_for_category(category, data),
        description=_embed_description("\n".join(lines)),
        color=discord.Color.blurple(),
    )


class MissionListPaginationView(discord.ui.View):
    def __init__(
        self,
        category: str,
        data: ProgressGuideData,
        missions: Sequence[MissionRow],
        page: int = 0,
    ) -> None:
        super().__init__(timeout=900)
        self.category = category
        self.data = data
        self.missions = list(missions)
        self.page = page
        self.total_pages = max(
            1, (len(self.missions) + _MISSIONS_PER_PAGE - 1) // _MISSIONS_PER_PAGE
        )
        for emoji, target, disabled in (
            ("⏮️", 0, self.page <= 0),
            ("◀️", self.page - 1, self.page <= 0),
            ("▶️", self.page + 1, self.page >= self.total_pages - 1),
            ("⏭️", self.total_pages - 1, self.page >= self.total_pages - 1),
        ):
            self.add_item(MissionPageButton(emoji, target, disabled))


class MissionPageButton(discord.ui.Button):
    def __init__(self, emoji: str, target_page: int, disabled: bool) -> None:
        self.target_page = target_page
        super().__init__(
            emoji=emoji, style=discord.ButtonStyle.secondary, disabled=disabled
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, MissionListPaginationView):
            return
        next_view = MissionListPaginationView(
            view.category, view.data, view.missions, self.target_page
        )
        await interaction.response.edit_message(
            embed=build_mission_embed(
                view.category, view.data, view.missions, page=self.target_page
            ),
            view=next_view,
        )


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


class ProgressGuideMissionButton(discord.ui.Button):
    def __init__(self, category: str, label: str = "Mission List") -> None:
        self.category = category
        super().__init__(
            label=_button_label(label or "Mission List"),
            style=discord.ButtonStyle.secondary,
            custom_id=f"{_MISSIONS_CUSTOM_ID_PREFIX}{category}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        if self.category not in _MISSION_CATEGORIES:
            embed = _mission_unavailable_embed(
                "No mission list is configured for this guide yet."
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        try:
            data = await get_or_load_progress_guide_data()
            missions = await get_or_load_missions(self.category)
        except Exception as exc:
            if _is_quota_failure(exc):
                embed = _mission_unavailable_embed(
                    "Google Sheets read quota was temporarily exceeded. Please wait a minute and try again."
                )
            else:
                embed = _mission_unavailable_embed(
                    "No mission list is configured for this guide yet."
                )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        if not missions:
            embed = _mission_unavailable_embed(
                "No missions are currently available for this guide."
            )
            await interaction.followup.send(embed=embed, ephemeral=True)
            return
        view = MissionListPaginationView(self.category, data, missions, 0)
        await interaction.followup.send(
            embed=build_mission_embed(self.category, data, missions, page=0),
            view=view,
            ephemeral=True,
        )


@dataclass(slots=True)
class ProgressCategory:
    category: str
    label: str
    total_steps: int | None
    mission_tab_config_key: str


async def _progress_category(category: str) -> ProgressCategory | None:
    sheet_id = get_milestones_sheet_id().strip()
    categories_tab = await milestones_config.arequire_value(_CATEGORIES_KEY)
    rows = await afetch_records(sheet_id, categories_tab)
    for row in rows:
        if _text(row.get("category")) == category:
            return ProgressCategory(
                category=category,
                label=_text(row.get("label")) or category,
                total_steps=_int_or_none(row.get("total_steps")),
                mission_tab_config_key=_text(row.get("mission_tab_config_key")),
            )
    return None


async def _user_state_rows() -> tuple[str, list[dict[str, Any]]]:
    sheet_id = get_milestones_sheet_id().strip()
    tab = await milestones_config.arequire_value(_USER_STATE_KEY)
    return tab, await afetch_records(sheet_id, tab)


def _find_user_state(
    rows: Sequence[Mapping[str, object]], user_id: int, category: str
) -> tuple[int, Mapping[str, object]] | None:
    user = str(user_id)
    for index, row in enumerate(rows, start=2):
        if _text(row.get("user_id")) == user and _text(row.get("category")) == category:
            return index, row
    return None


def _format_percent(current: int | None, total: int | None) -> str:
    if current is None or not total:
        return ""
    value = current / total * 100
    return f"{int(value)}" if value.is_integer() else f"{value:.1f}"


def _mission_for_state(
    state: Mapping[str, object], missions: Sequence[MissionRow]
) -> MissionRow | None:
    key = _text(state.get("current_mission_key"))
    if key:
        found = next((m for m in missions if m.key == key), None)
        if found:
            return found
    step = _int_or_none(state.get("current_step_index"))
    return (
        next((m for m in missions if m.step_index == step), None)
        if step is not None
        else None
    )


def build_my_progress_embed(
    post: ForumPost,
    category_info: ProgressCategory | None,
    state: Mapping[str, object] | None,
    missions: Sequence[MissionRow],
) -> discord.Embed:
    title = _embed_title(
        post.my_progress_title or f"{post.label or post.category} Progress"
    )
    if state is None:
        return discord.Embed(
            title=title,
            description=_embed_description(post.my_progress_empty_description),
            color=discord.Color.blurple(),
        )
    mission = _mission_for_state(state, missions)
    sequence = (
        mission.sequence_number
        if mission
        else _int_or_none(state.get("current_step_index"))
    )
    local_step = (
        mission.step_index if mission else _int_or_none(state.get("current_step_index"))
    )
    total = category_info.total_steps if category_info else None
    status = _text(state.get("status"))
    is_done = status.casefold() in {"done", "completed", "complete"}
    completed_steps = (
        sequence
        if is_done and sequence is not None
        else (max(sequence - 1, 0) if sequence is not None else None)
    )
    remaining_steps = (
        max(total - completed_steps, 0)
        if total is not None and completed_steps is not None
        else None
    )
    chapter_title = mission.title if mission else ""
    chapter_total_steps = (
        sum(
            1
            for m in missions
            if (m.title or "Missions") == (mission.title or "Missions")
        )
        if mission
        else None
    )
    values = {
        "category_label": (
            category_info.label if category_info else (post.label or post.category)
        ),
        "current_step_index": str(local_step) if local_step is not None else "",
        "total_steps": str(total) if total is not None else "",
        "mission_description": mission.text if mission else "",
        "percent_complete": _format_percent(completed_steps, total),
        "remaining_steps": str(remaining_steps) if remaining_steps is not None else "",
        "status": status,
        "current_sequence_number": str(sequence) if sequence is not None else "",
        "completed_steps": str(completed_steps) if completed_steps is not None else "",
        "chapter_title": chapter_title,
        "chapter_step_index": str(local_step) if local_step is not None else "",
        "chapter_total_steps": (
            str(chapter_total_steps) if chapter_total_steps is not None else ""
        ),
    }
    body = (
        (
            post.my_progress_completed_template
            if is_done and post.my_progress_completed_template
            else post.my_progress_body_template
        )
        or "Current mission: {current_step_index} / {total_steps}\n\nMission: {mission_description}\n\nProgress: {percent_complete}% complete\n{remaining_steps} missions remaining"
    )
    for key, value in values.items():
        body = body.replace("{" + key + "}", value)
    return discord.Embed(
        title=title, description=_embed_description(body), color=discord.Color.blurple()
    )


class SetProgressButton(discord.ui.Button):
    def __init__(self, category: str, label: str) -> None:
        self.category = category
        super().__init__(
            label=_button_label(label),
            style=discord.ButtonStyle.primary,
            custom_id=f"{_SET_PROGRESS_CUSTOM_ID_PREFIX}{category}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        data = await get_or_load_progress_guide_data()
        post = _post_for_category(self.category, data)
        if post is None:
            await interaction.followup.send(
                embed=_progress_unavailable_embed(None), ephemeral=True
            )
            return
        try:
            missions = await get_or_load_missions(self.category)
        except Exception as exc:
            if _is_quota_failure(exc):
                await interaction.followup.send(
                    embed=_progress_unavailable_embed(post), ephemeral=True
                )
                return
            raise
        if not missions:
            await interaction.followup.send(
                embed=_progress_no_missions_embed(post), ephemeral=True
            )
            return
        await interaction.followup.send(
            embed=build_progress_picker_embed(post),
            view=ProgressChapterPickerView(self.category, post, missions),
            ephemeral=True,
        )


class MarkMissionDoneButton(discord.ui.Button):
    def __init__(self, category: str, label: str) -> None:
        self.category = category
        super().__init__(
            label=_button_label(label or "Mark Mission Done"),
            style=discord.ButtonStyle.success,
            custom_id=f"progressguides:complete:{category}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        post: ForumPost | None = None
        try:
            data = await get_or_load_progress_guide_data()
            post = _post_for_category(self.category, data)
            if post is None:
                await interaction.followup.send(
                    embed=_progress_unavailable_embed(post), ephemeral=True
                )
                return
            category_info = await _progress_category(self.category)
            _tab, rows = await _user_state_rows()
            found = _find_user_state(rows, interaction.user.id, self.category)
            if found is None:
                await interaction.followup.send(
                    embed=build_my_progress_embed(post, category_info, None, []),
                    ephemeral=True,
                )
                return
            missions = await get_or_load_missions(self.category)
            old_state = found[1]
            new_state = await complete_current_mission(
                interaction.user.id, self.category, old_state, missions
            )
            embed = build_my_progress_embed(post, category_info, new_state, missions)
            if _text(new_state.get("status")).casefold() in {
                "done",
                "completed",
                "complete",
            }:
                footer = post.my_progress_complete_saved_description
            else:
                footer = post.my_progress_next_mission_saved_description
            if footer:
                embed.set_footer(text=footer)
            await interaction.followup.send(
                embed=embed, view=MyProgressView(post), ephemeral=True
            )
        except Exception as exc:
            if _is_quota_failure(exc):
                await interaction.followup.send(
                    embed=_progress_unavailable_embed(post), ephemeral=True
                )
                return
            raise


class MyProgressView(discord.ui.View):
    def __init__(self, post: ForumPost) -> None:
        super().__init__(timeout=900)
        if post.my_progress_set_button_label:
            self.add_item(
                SetProgressButton(post.category, post.my_progress_set_button_label)
            )
        if post.my_progress_complete_button_label:
            self.add_item(
                MarkMissionDoneButton(
                    post.category, post.my_progress_complete_button_label
                )
            )


def build_progress_picker_embed(
    post: ForumPost, *, selected_chapter: str | None = None
) -> discord.Embed:
    description = post.my_progress_picker_description
    if selected_chapter:
        description = (
            f"{description}\n\n**{_limit_text(selected_chapter, 200)}**"
            if description
            else f"**{_limit_text(selected_chapter, 200)}**"
        )
    return discord.Embed(
        title=_embed_title(post.my_progress_picker_title or post.my_progress_title),
        description=_embed_description(description),
        color=discord.Color.blurple(),
    )


def _progress_no_missions_embed(post: ForumPost) -> discord.Embed:
    return _progress_notice_embed(
        post,
        post.my_progress_no_missions_description
        or post.my_progress_missing_step_description,
    )


def _chapter_groups(
    missions: Sequence[MissionRow],
) -> list[tuple[str, list[MissionRow]]]:
    grouped: dict[str, list[MissionRow]] = {}
    for mission in missions:
        chapter = mission.title or "Missions"
        grouped.setdefault(chapter, []).append(mission)
    groups = [(title, rows) for title, rows in grouped.items()]
    return sorted(groups, key=lambda item: item[1][0].sequence_number if item[1] else 0)


def _select_label(value: object) -> str:
    text = _text(value)
    return text if len(text) <= _SELECT_LABEL_LIMIT else text[:97].rstrip() + "..."


def _mission_option_label(mission: MissionRow) -> str:
    return _select_label(f"{mission.step_index}. {mission.text}")


def _mission_option_value(mission: MissionRow) -> str:
    return mission.key or str(mission.sequence_number)


class ProgressChapterSelect(discord.ui.Select):
    def __init__(
        self,
        groups: Sequence[tuple[str, list[MissionRow]]],
        page: int,
        placeholder: str,
    ) -> None:
        self.groups = list(groups)
        start = page * _PICKER_OPTIONS_PER_PAGE
        options = [
            discord.SelectOption(label=_select_label(title), value=str(start + index))
            for index, (title, _rows) in enumerate(
                self.groups[start : start + _PICKER_OPTIONS_PER_PAGE]
            )
        ]
        super().__init__(
            placeholder=_select_label(placeholder or "Select a chapter"),
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ProgressChapterPickerView):
            return
        index = int(self.values[0])
        await interaction.response.edit_message(
            embed=build_progress_picker_embed(
                view.post, selected_chapter=view.groups[index][0]
            ),
            view=ProgressMissionPickerView(
                view.category, view.post, view.missions, index, 0
            ),
        )


class ProgressMissionSelect(discord.ui.Select):
    def __init__(
        self, missions: Sequence[MissionRow], placeholder: str, page: int
    ) -> None:
        self.missions = list(missions)
        start = page * _PICKER_OPTIONS_PER_PAGE
        options = [
            discord.SelectOption(
                label=_mission_option_label(mission),
                value=_mission_option_value(mission),
            )
            for mission in self.missions[start : start + _PICKER_OPTIONS_PER_PAGE]
        ]
        super().__init__(
            placeholder=_select_label(placeholder or "Select a mission"),
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ProgressMissionPickerView):
            return
        selected_value = self.values[0]
        mission = next(
            (
                m
                for m in view.chapter_missions
                if _mission_option_value(m) == selected_value
            ),
            None,
        )
        if mission is None:
            await interaction.response.send_message(
                embed=_progress_no_missions_embed(view.post), ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await get_or_load_progress_guide_data()
            post = _post_for_category(view.category, data) or view.post
            category_info = await _progress_category(view.category)
            await upsert_progress_user_state(
                interaction.user.id, view.category, mission
            )
            state = {
                "current_step_index": str(mission.step_index),
                "current_mission_key": mission.key,
                "status": "in_progress",
            }
            embed = build_my_progress_embed(post, category_info, state, view.missions)
            if post.my_progress_saved_description:
                embed.set_footer(text=post.my_progress_saved_description)
            await interaction.followup.send(embed=embed, ephemeral=True)
        except Exception as exc:
            if _is_quota_failure(exc):
                await interaction.followup.send(
                    embed=_progress_unavailable_embed(view.post), ephemeral=True
                )
                return
            raise


class ProgressChapterPickerView(discord.ui.View):
    def __init__(
        self,
        category: str,
        post: ForumPost,
        missions: Sequence[MissionRow],
        page: int = 0,
    ) -> None:
        super().__init__(timeout=900)
        self.category = category
        self.post = post
        self.missions = list(missions)
        self.groups = _chapter_groups(self.missions)
        self.page = page
        select = ProgressChapterSelect(
            self.groups,
            page,
            post.my_progress_chapter_select_placeholder,
        )
        self.add_item(select)
        total_pages = max(
            1,
            (len(self.groups) + _PICKER_OPTIONS_PER_PAGE - 1)
            // _PICKER_OPTIONS_PER_PAGE,
        )
        if total_pages > 1:
            self.add_item(
                ProgressPickerPageButton("◀️", page - 1, page <= 0, "chapter")
            )
            self.add_item(
                ProgressPickerPageButton(
                    "▶️", page + 1, page >= total_pages - 1, "chapter"
                )
            )


class ProgressMissionPickerView(discord.ui.View):
    def __init__(
        self,
        category: str,
        post: ForumPost,
        missions: Sequence[MissionRow],
        chapter_index: int,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=900)
        self.category = category
        self.post = post
        self.missions = list(missions)
        self.groups = _chapter_groups(self.missions)
        self.chapter_index = chapter_index
        self.chapter_title, self.chapter_missions = self.groups[chapter_index]
        self.page = page
        self.add_item(
            ProgressMissionSelect(
                self.chapter_missions, post.my_progress_mission_select_placeholder, page
            )
        )
        total_pages = max(
            1,
            (len(self.chapter_missions) + _PICKER_OPTIONS_PER_PAGE - 1)
            // _PICKER_OPTIONS_PER_PAGE,
        )
        if total_pages > 1:
            self.add_item(
                ProgressPickerPageButton("◀️", page - 1, page <= 0, "mission")
            )
            self.add_item(
                ProgressPickerPageButton(
                    "▶️", page + 1, page >= total_pages - 1, "mission"
                )
            )


class ProgressPickerPageButton(discord.ui.Button):
    def __init__(self, emoji: str, target_page: int, disabled: bool, mode: str) -> None:
        self.target_page = target_page
        self.mode = mode
        super().__init__(
            emoji=emoji, style=discord.ButtonStyle.secondary, disabled=disabled
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if isinstance(view, ProgressChapterPickerView):
            next_view = ProgressChapterPickerView(
                view.category, view.post, view.missions, self.target_page
            )
            await interaction.response.edit_message(
                embed=build_progress_picker_embed(view.post), view=next_view
            )
        elif isinstance(view, ProgressMissionPickerView):
            next_view = ProgressMissionPickerView(
                view.category,
                view.post,
                view.missions,
                view.chapter_index,
                self.target_page,
            )
            await interaction.response.edit_message(
                embed=build_progress_picker_embed(
                    view.post, selected_chapter=view.chapter_title
                ),
                view=next_view,
            )


async def upsert_progress_user_state(
    user_id: int, category: str, mission: MissionRow
) -> None:
    sheet_id = get_milestones_sheet_id().strip()
    tab, rows = await _user_state_rows()
    found = _find_user_state(rows, user_id, category)
    worksheet = await aget_worksheet(sheet_id, tab)
    header = await _load_header(sheet_id, tab)
    now = datetime.now(timezone.utc).isoformat()
    values = {
        "user_id": str(user_id),
        "category": category,
        "current_step_index": str(mission.step_index),
        "current_mission_key": mission.key,
        "status": "in_progress",
        "notify_plan_ahead": "",
        "private_thread_id": "",
        "last_panel_message_id": "",
        "notes": "",
        "updated_at_utc": now,
    }
    if found is None:
        row = [values.get(name, "") for name in header]
        await acall_with_backoff(worksheet.append_row, row, value_input_option="RAW")
        return
    row_number, existing = found
    preserve = {
        "notify_plan_ahead",
        "private_thread_id",
        "last_panel_message_id",
        "notes",
    }
    full_row = [
        _text(existing.get(name)) if name in preserve else values.get(name, "")
        for name in header
    ]
    start_col = _column_label(0)
    end_col = _column_label(len(header) - 1)
    await acall_with_backoff(
        worksheet.update,
        f"{start_col}{row_number}:{end_col}{row_number}",
        [full_row],
        value_input_option="RAW",
    )


async def complete_current_mission(
    user_id: int,
    category: str,
    state: Mapping[str, object],
    missions: Sequence[MissionRow],
) -> Mapping[str, object]:
    mission = _mission_for_state(state, missions)
    if mission is None:
        return state
    next_mission = next(
        (m for m in missions if m.sequence_number == mission.sequence_number + 1), None
    )
    status = "in_progress" if next_mission else "done"
    target = next_mission or mission
    sheet_id = get_milestones_sheet_id().strip()
    tab, rows = await _user_state_rows()
    found = _find_user_state(rows, user_id, category)
    if found is None:
        await upsert_progress_user_state(user_id, category, target)
        return {
            **dict(state),
            "current_step_index": str(target.step_index),
            "current_mission_key": target.key,
            "status": status,
        }
    row_number, existing = found
    worksheet = await aget_worksheet(sheet_id, tab)
    header = await _load_header(sheet_id, tab)
    now = datetime.now(timezone.utc).isoformat()
    values = {
        "user_id": str(user_id),
        "category": category,
        "current_step_index": str(target.step_index),
        "current_mission_key": target.key,
        "status": status,
        "updated_at_utc": now,
    }
    preserve = {
        "notify_plan_ahead",
        "private_thread_id",
        "last_panel_message_id",
        "notes",
    }
    full_row = [
        (
            _text(existing.get(name))
            if name in preserve
            else values.get(name, _text(existing.get(name)))
        )
        for name in header
    ]
    start_col = _column_label(0)
    end_col = _column_label(len(header) - 1)
    await acall_with_backoff(
        worksheet.update,
        f"{start_col}{row_number}:{end_col}{row_number}",
        [full_row],
        value_input_option="RAW",
    )
    return {**dict(existing), **values}


def _progress_notice_embed(post: ForumPost | None, description: str) -> discord.Embed:
    title = post.my_progress_title if post else "My Progress"
    return discord.Embed(
        title=_embed_title(title),
        description=_embed_description(description or "Unavailable."),
        color=discord.Color.red(),
    )


def _progress_unavailable_embed(post: ForumPost | None = None) -> discord.Embed:
    description = post.my_progress_unavailable_description if post else "Unavailable."
    return _progress_notice_embed(post, description)


class ProgressGuideMyProgressButton(discord.ui.Button):
    def __init__(self, category: str, label: str = "My Progress") -> None:
        self.category = category
        super().__init__(
            label=_button_label(label or "My Progress"),
            style=discord.ButtonStyle.secondary,
            custom_id=f"{_MY_PROGRESS_CUSTOM_ID_PREFIX}{category}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        post: ForumPost | None = None
        if self.category not in _MISSION_CATEGORIES:
            await interaction.followup.send(
                embed=_progress_unavailable_embed(post), ephemeral=True
            )
            return
        try:
            data = await get_or_load_progress_guide_data()
            post = _post_for_category(self.category, data)
            if post is None:
                await interaction.followup.send(
                    embed=_progress_unavailable_embed(post), ephemeral=True
                )
                return
            category_info = await _progress_category(self.category)
            _tab, rows = await _user_state_rows()
            found = _find_user_state(rows, interaction.user.id, self.category)
            missions = await get_or_load_missions(self.category) if found else []
            state = found[1] if found else None
            await interaction.followup.send(
                embed=build_my_progress_embed(post, category_info, state, missions),
                view=MyProgressView(post),
                ephemeral=True,
            )
        except Exception as exc:
            if _is_quota_failure(exc):
                await interaction.followup.send(
                    embed=_progress_unavailable_embed(post), ephemeral=True
                )
                return
            raise


class ProgressGuideMyProgressPersistentView(discord.ui.View):
    def __init__(self, categories: Sequence[str] = _MISSION_CATEGORIES) -> None:
        super().__init__(timeout=None)
        for category in categories:
            self.add_item(ProgressGuideMyProgressButton(category))


class ProgressGuideMissionPersistentView(discord.ui.View):
    def __init__(self, categories: Sequence[str] = _MISSION_CATEGORIES) -> None:
        super().__init__(timeout=None)
        for category in categories:
            self.add_item(ProgressGuideMissionButton(category))


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
    if _supports_missions(post):
        view.add_item(
            ProgressGuideMissionButton(
                post.category, post.mission_list_button_label or "Mission List"
            )
        )
        added = True
    if _supports_my_progress(post):
        view.add_item(
            ProgressGuideMyProgressButton(post.category, post.my_progress_button_label)
        )
        added = True
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
    """Publish or refresh progress guide panels within a fixed Sheets budget.

    Read budget per command run:
    - Config is read through the existing milestones config path.
    - ProgressForumPosts, ProgressGuides, ProgressFAQ, and ProgressAssets are read
      once by :func:`load_progress_guide_data`.
    - Worksheet/header reads are lazy and only allowed when
      ``guide_panel_message_id`` must actually be written back.

    Write budget per command run:
    - ``guide_panel_message_id`` is written only after a new panel message is
      created or a stored/deleted message is recreated.
    - Refreshing an existing stored message only edits Discord and must not read
      worksheet/header data or write back to Sheets.
    """

    data = await load_progress_guide_data()
    clear_mission_cache()
    set_progress_guide_cache(data)
    sheet_id = get_milestones_sheet_id().strip()
    worksheet = None
    message_id_col: str | None = None
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
            try:
                if worksheet is None or message_id_col is None:
                    worksheet = await aget_worksheet(sheet_id, data.forum_posts_tab)
                    header = await _load_header(sheet_id, data.forum_posts_tab)
                    message_id_col = _column_label(
                        _header_index(header, _MESSAGE_ID_COLUMN)
                    )
                await acall_with_backoff(
                    worksheet.update,
                    f"{message_id_col}{post.row_number}",
                    [[str(message.id)]],
                    attempts=1,
                    value_input_option="RAW",
                )
            except Exception:
                await _delete_untracked_message(message)
                raise
            summary.created += 1
        except Exception as exc:
            if is_rate_limited_error(exc):
                raise
            summary.failures.append(f"{label}: {type(exc).__name__}: {exc}")
    return summary


async def _delete_untracked_message(message: discord.Message) -> None:
    try:
        await message.delete()
    except Exception:
        pass


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
