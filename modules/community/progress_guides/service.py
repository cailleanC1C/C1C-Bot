from __future__ import annotations

import asyncio
import logging
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
_GUIDE_POST_URL_COLUMN = "guide_post_url"
_HELP_MESSAGE_ID_COLUMN = "help_panel_message_id"
_HELP_POST_URL_COLUMN = "help_post_url"
_HELP_PANEL_COLUMNS = (
    "help_panel_title",
    "help_panel_description",
    "help_panel_footer",
    "help_back_button_label",
)
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
_PLAN_AHEAD_CUSTOM_ID_PREFIX = "progressguides:planahead:"
_FW_STARS_CUSTOM_ID_PREFIX = "progressguides:fwstars:"
_FW_PROGRESS_CUSTOM_ID_PREFIX = "progressguides:fwprogress:"
_FW_GUIDE_CUSTOM_ID_PREFIX = "progressguides:fwguide:"
_FW_CONDITIONS_CUSTOM_ID_PREFIX = "progressguides:fwconditions:"
_HOW_TO_USE_CUSTOM_ID_PREFIX = "progressguides:howto:"
_PERSISTENT_FAQ_CATEGORIES = ("ARB", "RAM", "MAR", "FW_N", "FW_H")
_MISSION_CATEGORIES = ("ARB", "RAM", "MAR")
_FACTION_WARS_CATEGORIES = ("FW_N", "FW_H")
_FW_HARD_CATEGORY = "FW_H"
_FW_USER_COUNTERS_KEY = "PROGRESS_USER_COUNTERS_TAB"
_FW_FACTIONS_KEY = "PROGRESS_FW_FACTIONS_TAB"
_FW_CHAMPION_GUIDES_KEY = "PROGRESS_FW_CHAMPION_GUIDES_TAB"
_FW_HARD_STAGE_CONDITIONS_KEY = "PROGRESS_FW_HARD_STAGE_CONDITIONS_TAB"
_FW_HARD_STAGE_SOLVERS_KEY = "PROGRESS_FW_HARD_STAGE_SOLVERS_TAB"
_MISSIONS_PER_PAGE = 15
_PICKER_OPTIONS_PER_PAGE = 25
_FAQ_OPTIONS_PER_PAGE = 25
_SELECT_LABEL_LIMIT = 100
_SELECT_VALUE_LIMIT = 100
_DATA_CACHE: "ProgressGuideData | None" = None
_DATA_CACHE_LOCK = asyncio.Lock()
_MISSION_CACHE: dict[str, list["MissionRow"]] = {}
_MISSION_CACHE_LOCKS: dict[str, asyncio.Lock] = {}
_FW_DATA_CACHE: "FactionWarsData | None" = None
_FW_DATA_CACHE_LOCK = asyncio.Lock()
log = logging.getLogger("c1c.community.progress_guides.service")


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
    plan_ahead_button_label: str
    plan_ahead_title: str
    plan_ahead_intro_template: str
    plan_ahead_no_progress_description: str
    plan_ahead_no_items_description: str
    plan_ahead_upcoming_field_title: str
    plan_ahead_save_field_title: str
    plan_ahead_avoid_field_title: str
    plan_ahead_time_gate_field_title: str
    plan_ahead_warning_field_title: str
    plan_ahead_footer: str
    plan_ahead_lookahead_count: int | None
    counter_stars_button_label: str
    counter_progress_button_label: str
    faction_guide_button_label: str
    conditions_button_label: str
    counter_stars_title: str
    counter_stars_empty_description: str
    counter_stars_saved_description: str
    counter_stars_invalid_value_description: str
    counter_stars_faction_select_placeholder: str
    counter_stars_modal_title: str
    counter_stars_modal_value_label: str
    counter_progress_title: str
    counter_progress_intro_template: str
    counter_progress_empty_description: str
    counter_progress_footer: str
    counter_progress_finished_field_title: str
    counter_progress_close_field_title: str
    counter_progress_focus_field_title: str
    faction_guide_title: str
    faction_guide_select_placeholder: str
    faction_guide_champions_field_title: str
    faction_guide_roles_field_title: str
    faction_guide_accessible_field_title: str
    faction_guide_note_field_title: str
    faction_guide_empty_description: str
    conditions_title: str
    conditions_select_placeholder: str
    conditions_summary_field_title: str
    conditions_stages_field_title: str
    conditions_empty_description: str
    progress_tracking_enabled: bool
    guide_channel_id: int | None
    guide_thread_id: int | None
    guide_panel_message_id: int | None
    guide_post_url: str
    help_channel_id: int | None
    help_thread_id: int | None
    help_post_url: str
    help_panel_message_id: int | None
    help_panel_title: str
    help_panel_description: str
    help_panel_footer: str
    help_back_button_label: str
    how_to_use_button_label: str
    how_to_use_title: str
    how_to_use_description: str
    guide_footer: str
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
            plan_ahead_button_label=_text(row.get("plan_ahead_button_label")),
            plan_ahead_title=_text(row.get("plan_ahead_title")),
            plan_ahead_intro_template=_text(row.get("plan_ahead_intro_template")),
            plan_ahead_no_progress_description=_text(
                row.get("plan_ahead_no_progress_description")
            ),
            plan_ahead_no_items_description=_text(
                row.get("plan_ahead_no_items_description")
            ),
            plan_ahead_upcoming_field_title=_text(
                row.get("plan_ahead_upcoming_field_title")
            ),
            plan_ahead_save_field_title=_text(row.get("plan_ahead_save_field_title")),
            plan_ahead_avoid_field_title=_text(row.get("plan_ahead_avoid_field_title")),
            plan_ahead_time_gate_field_title=_text(
                row.get("plan_ahead_time_gate_field_title")
            ),
            plan_ahead_warning_field_title=_text(
                row.get("plan_ahead_warning_field_title")
            ),
            plan_ahead_footer=_text(row.get("plan_ahead_footer")),
            plan_ahead_lookahead_count=_int_or_none(
                row.get("plan_ahead_lookahead_count")
            ),
            counter_stars_button_label=_text(row.get("counter_stars_button_label")),
            counter_progress_button_label=_text(
                row.get("counter_progress_button_label")
            ),
            faction_guide_button_label=_text(row.get("faction_guide_button_label")),
            conditions_button_label=_text(row.get("conditions_button_label")),
            counter_stars_title=_text(row.get("counter_stars_title")),
            counter_stars_empty_description=_text(
                row.get("counter_stars_empty_description")
            ),
            counter_stars_saved_description=_text(
                row.get("counter_stars_saved_description")
            ),
            counter_stars_invalid_value_description=_text(
                row.get("counter_stars_invalid_value_description")
            ),
            counter_stars_faction_select_placeholder=_text(
                row.get("counter_stars_faction_select_placeholder")
            ),
            counter_stars_modal_title=_text(row.get("counter_stars_modal_title")),
            counter_stars_modal_value_label=_text(
                row.get("counter_stars_modal_value_label")
            ),
            counter_progress_title=_text(row.get("counter_progress_title")),
            counter_progress_intro_template=_text(
                row.get("counter_progress_intro_template")
            ),
            counter_progress_empty_description=_text(
                row.get("counter_progress_empty_description")
            ),
            counter_progress_footer=_text(row.get("counter_progress_footer")),
            counter_progress_finished_field_title=_text(
                row.get("counter_progress_finished_field_title")
            ),
            counter_progress_close_field_title=_text(
                row.get("counter_progress_close_field_title")
            ),
            counter_progress_focus_field_title=_text(
                row.get("counter_progress_focus_field_title")
            ),
            faction_guide_title=_text(row.get("faction_guide_title")),
            faction_guide_select_placeholder=_text(
                row.get("faction_guide_select_placeholder")
            ),
            faction_guide_champions_field_title=_text(
                row.get("faction_guide_champions_field_title")
            ),
            faction_guide_roles_field_title=_text(
                row.get("faction_guide_roles_field_title")
            ),
            faction_guide_accessible_field_title=_text(
                row.get("faction_guide_accessible_field_title")
            ),
            faction_guide_note_field_title=_text(
                row.get("faction_guide_note_field_title")
            ),
            faction_guide_empty_description=_text(
                row.get("faction_guide_empty_description")
            ),
            conditions_title=_text(row.get("conditions_title")),
            conditions_select_placeholder=_text(
                row.get("conditions_select_placeholder")
            ),
            conditions_summary_field_title=_text(
                row.get("conditions_summary_field_title")
            ),
            conditions_stages_field_title=_text(
                row.get("conditions_stages_field_title")
            ),
            conditions_empty_description=_text(row.get("conditions_empty_description")),
            progress_tracking_enabled=_truthy(row.get("progress_tracking_enabled")),
            guide_channel_id=_int_or_none(row.get("guide_channel_id")),
            guide_thread_id=_int_or_none(row.get("guide_thread_id")),
            guide_panel_message_id=_int_or_none(row.get("guide_panel_message_id")),
            guide_post_url=_text(row.get("guide_post_url")),
            help_channel_id=_int_or_none(row.get("help_channel_id")),
            help_thread_id=_int_or_none(row.get("help_thread_id")),
            help_post_url=_text(row.get("help_post_url")),
            help_panel_message_id=_int_or_none(row.get("help_panel_message_id")),
            help_panel_title=_text(row.get("help_panel_title")),
            help_panel_description=_text(row.get("help_panel_description")),
            help_panel_footer=_text(row.get("help_panel_footer")),
            help_back_button_label=_text(row.get("help_back_button_label")),
            how_to_use_button_label=_text(row.get("how_to_use_button_label")),
            how_to_use_title=_text(row.get("how_to_use_title")),
            how_to_use_description=_text(row.get("how_to_use_description")),
            guide_footer=_text(row.get("guide_footer")),
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
    tips: str
    avoid_doing: str
    resource_tags: str
    time_gate: bool
    difficulty_note: str
    guide_priority: str
    retroactive_note: str
    prep_window: str
    completion_rule: str

    def __init__(
        self,
        sequence_number: int,
        text: str,
        key: str = "",
        title: str = "",
        step_index: int | None = None,
        tips: str = "",
        avoid_doing: str = "",
        resource_tags: str = "",
        time_gate: bool = False,
        difficulty_note: str = "",
        guide_priority: str = "",
        retroactive_note: str = "",
        prep_window: str = "",
        completion_rule: str = "",
    ) -> None:
        self.sequence_number = sequence_number
        self.step_index = step_index if step_index is not None else sequence_number
        self.key = key
        self.title = title
        self.text = text
        self.tips = tips
        self.avoid_doing = avoid_doing
        self.resource_tags = resource_tags
        self.time_gate = time_gate
        self.difficulty_note = difficulty_note
        self.guide_priority = guide_priority
        self.retroactive_note = retroactive_note
        self.prep_window = prep_window
        self.completion_rule = completion_rule

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
    clear_faction_wars_cache()


def clear_mission_cache() -> None:
    _MISSION_CACHE.clear()
    _MISSION_CACHE_LOCKS.clear()


def clear_faction_wars_cache() -> None:
    global _FW_DATA_CACHE
    _FW_DATA_CACHE = None


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


def _supports_plan_ahead(post: ForumPost) -> bool:
    return (
        post.category in _MISSION_CATEGORIES
        and post.progress_tracking_enabled
        and bool(post.plan_ahead_button_label)
    )


def _supports_my_progress(post: ForumPost) -> bool:
    return (
        post.category in _MISSION_CATEGORIES
        and post.progress_tracking_enabled
        and bool(post.my_progress_button_label)
    )


def _supports_how_to_use(post: ForumPost) -> bool:
    return bool(post.how_to_use_button_label and post.how_to_use_description)


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
                tips=_strip_visible_urls(row.get("tips")),
                avoid_doing=_strip_visible_urls(row.get("avoid_doing")),
                resource_tags=_text(row.get("resource_tags")),
                time_gate=_truthy(row.get("time_gate")),
                difficulty_note=_strip_visible_urls(row.get("difficulty_note")),
                guide_priority=_text(row.get("guide_priority")),
                retroactive_note=_strip_visible_urls(row.get("retroactive_note")),
                prep_window=_text(row.get("prep_window")),
                completion_rule=_text(row.get("completion_rule")),
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


def _faq_rows_for_category(
    category: str, data: ProgressGuideData
) -> list[Mapping[str, object]]:
    rows = [
        row
        for row in data.faq_by_category.get(category, [])
        if _truthy(row.get("enabled"))
        and _text(row.get("question"))
        and _text(row.get("answer"))
    ]
    return sorted(
        rows,
        key=lambda r: (
            _sort_num(r.get("sort_order")),
            _text(r.get("question") or r.get("faq_key")),
        ),
    )


def _faq_tag_label(tag: object) -> str:
    cleaned = _text(tag).replace("_", " ")
    return " ".join(cleaned.split()).title()


def _faq_tag_key(tag: object) -> str:
    label = _faq_tag_label(tag)
    return _limit_text(label.casefold(), _SELECT_VALUE_LIMIT)


def _parse_faq_tags(value: object) -> list[tuple[str, str]]:
    raw = _text(value)
    if not raw:
        return []
    tags: list[tuple[str, str]] = []
    seen: set[str] = set()
    for part in re.split(r"[,;]", raw):
        label = _faq_tag_label(part)
        if not label:
            continue
        key = _faq_tag_key(label)
        if not key or key in seen:
            continue
        seen.add(key)
        tags.append((key, label))
    return tags


def _faq_tag_options(rows: Sequence[Mapping[str, object]]) -> list[tuple[str, str]]:
    by_key: dict[str, str] = {}
    for row in rows:
        for key, label in _parse_faq_tags(row.get("tags")):
            by_key.setdefault(key, label)
    return sorted(by_key.items(), key=lambda item: item[1].casefold())


def _filter_faq_rows_by_tag(
    rows: Sequence[Mapping[str, object]], selected_tag: str | None
) -> list[Mapping[str, object]]:
    if not selected_tag:
        return list(rows)
    return [
        row
        for row in rows
        if any(key == selected_tag for key, _label in _parse_faq_tags(row.get("tags")))
    ]


def _faq_option_value(row: Mapping[str, object], sorted_index: int) -> str:
    key = _text(row.get("faq_key"))
    if key:
        return _limit_text(key, _SELECT_VALUE_LIMIT)
    return f"idx:{sorted_index}"


def build_faq_picker_embed(
    category: str,
    data: ProgressGuideData,
    rows: Sequence[Mapping[str, object]],
    *,
    page: int,
    selected_tag_label: str | None = None,
) -> discord.Embed:
    post = _post_for_category(category, data)
    description = _embed_description(post.faq_description) if post else None
    if selected_tag_label:
        filter_note = f"Filter: {selected_tag_label}"
        description = f"{description}\n\n{filter_note}" if description else filter_note
    total_pages = max(
        1, (len(rows) + _FAQ_OPTIONS_PER_PAGE - 1) // _FAQ_OPTIONS_PER_PAGE
    )
    if total_pages > 1:
        page_note = f"Page {page + 1} / {total_pages}"
        description = f"{description}\n\n{page_note}" if description else page_note
    return discord.Embed(
        title=_faq_title_for_category(category, data),
        description=_embed_description(description),
        color=discord.Color.blurple(),
    )


def build_selected_faq_embed(
    category: str, data: ProgressGuideData, row: Mapping[str, object]
) -> discord.Embed:
    question = _strip_visible_urls(row.get("question"))
    answer = _strip_visible_urls(row.get("answer"))
    prefix = f"**{question}**\n\n" if question else ""
    note = "\n\n*(Answer shortened because it is too long for one Discord embed.)*"
    description = f"{prefix}{answer}"
    if len(description) > _EMBED_DESCRIPTION_LIMIT:
        available = _EMBED_DESCRIPTION_LIMIT - len(prefix) - len(note)
        description = f"{prefix}{answer[: max(0, available)].rstrip()}{note}"
    return discord.Embed(
        title=_faq_title_for_category(category, data),
        description=_embed_description(description),
        color=discord.Color.blurple(),
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


class ProgressGuideFAQTagSelect(discord.ui.Select):
    def __init__(
        self, tag_options: Sequence[tuple[str, str]], selected_tag: str | None
    ) -> None:
        options = [
            discord.SelectOption(
                label="All questions", value="__all__", default=selected_tag is None
            )
        ]
        options.extend(
            discord.SelectOption(
                label=_limit_text(label, _SELECT_LABEL_LIMIT),
                value=key,
                default=key == selected_tag,
            )
            for key, label in list(tag_options)[:24]
        )
        super().__init__(
            placeholder="Filter FAQ questions by tag…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ProgressGuideFAQPickerView):
            return
        selected = self.values[0]
        selected_tag = None if selected == "__all__" else selected
        next_view = ProgressGuideFAQPickerView(
            view.category, view.data, view.all_rows, 0, selected_tag=selected_tag
        )
        await interaction.response.edit_message(
            embed=build_faq_picker_embed(
                view.category,
                view.data,
                next_view.filtered_rows,
                page=next_view.page,
                selected_tag_label=next_view.selected_tag_label,
            ),
            view=next_view,
        )


class ProgressGuideFAQSelect(discord.ui.Select):
    def __init__(self, rows: Sequence[Mapping[str, object]], page: int) -> None:
        self.page = page
        start = page * _FAQ_OPTIONS_PER_PAGE
        page_rows = list(enumerate(rows, start=0))[
            start : start + _FAQ_OPTIONS_PER_PAGE
        ]
        options = [
            discord.SelectOption(
                label=_limit_text(_text(row.get("question")), _SELECT_LABEL_LIMIT),
                value=_faq_option_value(row, index),
            )
            for index, row in page_rows
        ]
        super().__init__(
            placeholder="Choose a FAQ question…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ProgressGuideFAQPickerView):
            return
        selected = self.values[0]
        row = view.row_by_value.get(selected)
        if row is None:
            await interaction.response.edit_message(
                embed=build_faq_picker_embed(
                    view.category,
                    view.data,
                    view.filtered_rows,
                    page=view.page,
                    selected_tag_label=view.selected_tag_label,
                ),
                view=ProgressGuideFAQPickerView(
                    view.category,
                    view.data,
                    view.all_rows,
                    view.page,
                    selected_tag=view.selected_tag,
                ),
            )
            return
        await interaction.response.edit_message(
            embed=build_selected_faq_embed(view.category, view.data, row),
            view=ProgressGuideFAQPickerView(
                view.category,
                view.data,
                view.all_rows,
                view.page,
                selected_tag=view.selected_tag,
            ),
        )


class ProgressGuideFAQPageButton(discord.ui.Button):
    def __init__(self, label: str, target_page: int, disabled: bool) -> None:
        self.target_page = target_page
        super().__init__(
            label=label, style=discord.ButtonStyle.secondary, disabled=disabled
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, ProgressGuideFAQPickerView):
            return
        next_view = ProgressGuideFAQPickerView(
            view.category,
            view.data,
            view.all_rows,
            self.target_page,
            selected_tag=view.selected_tag,
        )
        await interaction.response.edit_message(
            embed=build_faq_picker_embed(
                view.category,
                view.data,
                next_view.filtered_rows,
                page=next_view.page,
                selected_tag_label=next_view.selected_tag_label,
            ),
            view=next_view,
        )


class ProgressGuideFAQPickerView(discord.ui.View):
    def __init__(
        self,
        category: str,
        data: ProgressGuideData,
        rows: Sequence[Mapping[str, object]],
        page: int = 0,
        *,
        selected_tag: str | None = None,
    ) -> None:
        super().__init__(timeout=900)
        self.category = category
        self.data = data
        self.all_rows = list(rows)
        self.tag_options = _faq_tag_options(self.all_rows)
        self.selected_tag = (
            selected_tag
            if any(key == selected_tag for key, _ in self.tag_options)
            else None
        )
        self.selected_tag_label = next(
            (label for key, label in self.tag_options if key == self.selected_tag), None
        )
        self.filtered_rows = _filter_faq_rows_by_tag(self.all_rows, self.selected_tag)
        self.rows = self.filtered_rows
        self.total_pages = max(
            1,
            (len(self.filtered_rows) + _FAQ_OPTIONS_PER_PAGE - 1)
            // _FAQ_OPTIONS_PER_PAGE,
        )
        self.page = max(0, min(page, self.total_pages - 1))
        self.row_by_value = {
            _faq_option_value(row, index): row
            for index, row in enumerate(self.filtered_rows)
        }
        if self.tag_options:
            self.add_item(
                ProgressGuideFAQTagSelect(self.tag_options, self.selected_tag)
            )
        if self.filtered_rows:
            self.add_item(ProgressGuideFAQSelect(self.filtered_rows, self.page))
        if self.total_pages > 1:
            self.add_item(
                ProgressGuideFAQPageButton("Previous", self.page - 1, self.page <= 0)
            )
            self.add_item(
                ProgressGuideFAQPageButton(
                    "Next", self.page + 1, self.page >= self.total_pages - 1
                )
            )

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True


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
            rows = _faq_rows_for_category(self.category, data)
            if not rows:
                embed = discord.Embed(
                    title=f"{self.category} FAQ",
                    description="No FAQ entries are currently available.",
                    color=discord.Color.blurple(),
                )
                await interaction.followup.send(embed=embed, ephemeral=True)
                return
            view = ProgressGuideFAQPickerView(self.category, data, rows, 0)
            embed = build_faq_picker_embed(self.category, data, rows, page=0)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
            return
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


def _plan_lookahead_count(post: ForumPost) -> int:
    count = post.plan_ahead_lookahead_count or 12
    return max(1, min(count, 25))


def _dedupe(values: Sequence[str], limit: int = 8) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        clean = _strip_visible_urls(value)
        key = clean.casefold()
        if not clean or key in seen:
            continue
        seen.add(key)
        result.append(clean)
        if len(result) >= limit:
            break
    return result


_RAW_PLAN_TOKENS = {
    "normal",
    "medium",
    "high",
    "wall",
    "major_wall",
    "critical",
}
_PREP_WINDOW_RANK = {
    "critical": 6,
    "major_wall": 5,
    "major wall": 5,
    "wall": 4,
    "high": 3,
    "medium": 2,
    "normal": 1,
}


def _normalized_line(value: object) -> str:
    clean = _strip_visible_urls(value)
    clean = re.sub(r"\s+", " ", clean).strip()
    if not clean:
        return ""
    folded = clean.casefold()
    if folded in _RAW_PLAN_TOKENS:
        return ""
    if re.fullmatch(r"[a-z0-9]+(?:_[a-z0-9]+)+", clean):
        return ""
    return clean


def _append_unique_line(lines: list[str], value: object) -> None:
    clean = _normalized_line(value)
    if not clean:
        return
    key = clean.casefold()
    if key not in {line.casefold() for line in lines}:
        lines.append(clean)


def _mission_plan_prefix(current_title: str, mission: MissionRow) -> str:
    if (mission.title or "Missions") == (current_title or "Missions"):
        return f"{mission.step_index}"
    return f"{mission.title or 'Missions'}, {mission.step_index}"


def _mission_plan_label(current_title: str, mission: MissionRow) -> str:
    text = _limit_text(mission.text, 140)
    return f"{_mission_plan_prefix(current_title, mission)}: {text}"


def _limited_bullets(
    lines: Sequence[str], limit: int, overflow_label: str | None = None
) -> str:
    visible = [line for line in lines if line]
    selected = visible[:limit]
    bullets = [f"- {line}" for line in selected]
    overflow = len(visible) - len(selected)
    if overflow > 0 and overflow_label:
        bullets.append(f"- +{overflow} {overflow_label}{'' if overflow == 1 else 's'}.")
    result: list[str] = []
    used = 0
    for bullet in bullets:
        extra = len(bullet) + (1 if result else 0)
        if used + extra > _FIELD_LIMIT:
            break
        result.append(bullet)
        used += extra
    return "\n".join(result)


def _prep_window_rank(value: object) -> int:
    raw_text = _text(value)
    if not raw_text:
        return 0
    try:
        return int(float(raw_text))
    except ValueError:
        pass
    raw = raw_text.casefold().replace("-", "_")
    return _PREP_WINDOW_RANK.get(raw, 0)


_ACTIVE_ONLY_TIMING_PHRASES = (
    "after this mission is active",
    "while this mission is active",
    "if it does not auto-complete",
    "after the mission is active",
    "while the mission is active",
    "if the mission does not auto-complete",
)


def _is_active_only_mission(mission: MissionRow) -> bool:
    return mission.completion_rule.strip().casefold() == "active_only"


def _is_active_only_instruction_note(note: str) -> bool:
    folded = note.casefold()
    return any(phrase in folded for phrase in _ACTIVE_ONLY_TIMING_PHRASES)


def _plan_warning_candidates(upcoming_missions: Sequence[MissionRow]) -> list[str]:
    candidates: list[tuple[int, int, str]] = []
    for order, mission in enumerate(upcoming_missions):
        clean = _normalized_line(mission.difficulty_note)
        if clean:
            candidates.append((-_prep_window_rank(mission.prep_window), order, clean))
    lines: list[str] = []
    for _rank, _order, line in sorted(candidates):
        _append_unique_line(lines, line)
        if len(lines) >= 2:
            break
    return lines


def _display_resource_tags(value: object, limit: int = 8) -> list[str]:
    parts = re.split(r"[,;]", _text(value))
    result: list[str] = []
    for part in parts:
        _append_unique_line(result, part.strip().replace("_", " "))
        if len(result) >= limit:
            break
    return result


def build_plan_ahead_embed(
    post: ForumPost,
    category_info: ProgressCategory | None,
    state: Mapping[str, object] | None,
    missions: Sequence[MissionRow],
) -> discord.Embed:
    title = _embed_title(post.plan_ahead_title or post.my_progress_title or post.label)
    if state is None:
        return discord.Embed(
            title=title,
            description=_embed_description(post.plan_ahead_no_progress_description),
            color=discord.Color.blurple(),
        )
    current = _mission_for_state(state, missions)
    if current is None:
        return discord.Embed(
            title=title,
            description=_embed_description(post.plan_ahead_no_progress_description),
            color=discord.Color.blurple(),
        )
    total = category_info.total_steps if category_info else len(missions)
    completed = max(current.sequence_number - 1, 0)
    remaining = max(total - completed, 0) if total is not None else None
    lookahead_count = _plan_lookahead_count(post)
    upcoming = [m for m in missions if m.sequence_number > current.sequence_number][
        :lookahead_count
    ]
    chapter_total = sum(
        1 for m in missions if (m.title or "Missions") == (current.title or "Missions")
    )
    values = {
        "category_label": (
            category_info.label if category_info else (post.label or post.category)
        ),
        "chapter_title": current.title,
        "chapter_step_index": str(current.step_index),
        "chapter_total_steps": str(chapter_total),
        "current_sequence_number": str(current.sequence_number),
        "completed_steps": str(completed),
        "total_steps": str(total) if total is not None else "",
        "remaining_steps": str(remaining) if remaining is not None else "",
        "percent_complete": _format_percent(completed, total),
        "lookahead_count": str(lookahead_count),
        "mission_description": current.text,
    }
    description = post.plan_ahead_intro_template
    for key, value in values.items():
        description = description.replace("{" + key + "}", value)
    embed = discord.Embed(
        title=title,
        description=_embed_description(description),
        color=discord.Color.blurple(),
    )
    current_title = current.title or "Missions"
    if upcoming and post.plan_ahead_upcoming_field_title:
        upcoming_lines = [_mission_plan_label(current_title, m) for m in upcoming]
        value = _limited_bullets(upcoming_lines, lookahead_count)
        if value:
            embed.add_field(
                name=_embed_title(post.plan_ahead_upcoming_field_title),
                value=value,
                inline=False,
            )

    if post.plan_ahead_save_field_title:
        save_lines: list[str] = []
        for m in upcoming:
            if _normalized_line(m.tips):
                _append_unique_line(save_lines, m.tips)
            else:
                for resource_line in _display_resource_tags(m.resource_tags):
                    _append_unique_line(save_lines, resource_line)
        value = _limited_bullets(save_lines, 4)
        if value:
            embed.add_field(
                name=_embed_title(post.plan_ahead_save_field_title),
                value=value,
                inline=False,
            )

    if post.plan_ahead_avoid_field_title:
        avoid_lines: list[str] = []
        for m in upcoming:
            avoid = _normalized_line(m.avoid_doing)
            if avoid and _is_active_only_mission(m):
                _append_unique_line(
                    avoid_lines, f"{_mission_plan_prefix(current_title, m)}: {avoid}"
                )
        value = _limited_bullets(avoid_lines, 3)
        if value:
            embed.add_field(
                name=_embed_title(post.plan_ahead_avoid_field_title),
                value=value,
                inline=False,
            )

    if post.plan_ahead_time_gate_field_title:
        timing_lines: list[str] = []
        for m in upcoming:
            note = _normalized_line(m.retroactive_note)
            if not note:
                continue
            if _is_active_only_instruction_note(note):
                continue
            _append_unique_line(
                timing_lines, f"{_mission_plan_prefix(current_title, m)}: {note}"
            )
        value = _limited_bullets(timing_lines, 3, "more timing note")
        if value:
            embed.add_field(
                name=_embed_title(post.plan_ahead_time_gate_field_title),
                value=value,
                inline=False,
            )

    if post.plan_ahead_warning_field_title:
        value = _limited_bullets(_plan_warning_candidates(upcoming), 2)
        if value:
            embed.add_field(
                name=_embed_title(post.plan_ahead_warning_field_title),
                value=value,
                inline=False,
            )
    if not embed.fields and post.plan_ahead_no_items_description:
        embed.description = _embed_description(post.plan_ahead_no_items_description)
    if post.plan_ahead_footer:
        embed.set_footer(text=_limit_text(post.plan_ahead_footer, 2048))
    return embed


class PlanAheadButton(discord.ui.Button):
    def __init__(self, category: str, label: str = "Plan Ahead") -> None:
        self.category = category
        super().__init__(
            label=_button_label(label or "Plan Ahead"),
            style=discord.ButtonStyle.secondary,
            custom_id=f"{_PLAN_AHEAD_CUSTOM_ID_PREFIX}{category}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        post: ForumPost | None = None
        category_info: ProgressCategory | None = None
        state: dict[str, Any] | None = None
        missions: list[MissionRow] = []
        try:
            data = await get_or_load_progress_guide_data()
            post = _post_for_category(self.category, data)
            if post is None or not _supports_plan_ahead(post):
                await interaction.followup.send(
                    embed=_progress_unavailable_embed(post), ephemeral=True
                )
                return
            category_info = await _progress_category(self.category)
            _tab, rows = await _user_state_rows()
            found = _find_user_state(rows, interaction.user.id, self.category)
            state = found[1] if found else None
            missions = await get_or_load_missions(self.category) if found else []
            view = (
                PlanAheadNoProgressView(post)
                if state is None and post.my_progress_set_button_label
                else None
            )
            embed = build_plan_ahead_embed(post, category_info, state, missions)
            send_kwargs: dict[str, Any] = {"embed": embed, "ephemeral": True}
            if view is not None:
                send_kwargs["view"] = view
            await interaction.followup.send(**send_kwargs)
        except Exception as exc:
            if _is_quota_failure(exc):
                await interaction.followup.send(
                    embed=_progress_unavailable_embed(post), ephemeral=True
                )
                return
            log.exception(
                "plan ahead callback failed",
                extra={
                    "category": self.category,
                    "user_id": getattr(getattr(interaction, "user", None), "id", None),
                    "guild_id": getattr(
                        getattr(interaction, "guild", None), "id", None
                    ),
                    "channel_id": getattr(
                        getattr(interaction, "channel", None), "id", None
                    ),
                    "exc_type": type(exc).__name__,
                    "exc_message": str(exc),
                    "post_found": post is not None,
                    "state_found": state is not None,
                    "current_mission_key": (
                        _text(state.get("current_mission_key")) if state else ""
                    ),
                    "current_step_index": (
                        _text(state.get("current_step_index")) if state else ""
                    ),
                    "missions_loaded_count": (
                        len(missions) if missions is not None else 0
                    ),
                    "category_info_found": category_info is not None,
                },
            )
            await interaction.followup.send(
                embed=_progress_unavailable_embed(post), ephemeral=True
            )


class PlanAheadNoProgressView(discord.ui.View):
    def __init__(self, post: ForumPost) -> None:
        super().__init__(timeout=900)
        self.add_item(
            SetProgressButton(post.category, post.my_progress_set_button_label)
        )


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
        if _supports_plan_ahead(post):
            self.add_item(PlanAheadButton(post.category, post.plan_ahead_button_label))


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


class ProgressGuidePlanAheadPersistentView(discord.ui.View):
    def __init__(self, categories: Sequence[str] = _MISSION_CATEGORIES) -> None:
        super().__init__(timeout=None)
        for category in categories:
            self.add_item(PlanAheadButton(category))


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


class ProgressGuideHowToUsePersistentView(discord.ui.View):
    def __init__(self, categories: Sequence[str] = _PERSISTENT_FAQ_CATEGORIES) -> None:
        super().__init__(timeout=None)
        for category in categories:
            self.add_item(ProgressGuideHowToUseButton(category))


@dataclass(slots=True)
class FactionWarsData:
    factions: list[Mapping[str, object]]
    champion_guides: list[Mapping[str, object]]
    hard_stage_conditions: list[Mapping[str, object]]
    hard_stage_solvers: list[Mapping[str, object]]


async def get_or_load_faction_wars_data() -> FactionWarsData:
    global _FW_DATA_CACHE
    if _FW_DATA_CACHE is not None:
        return _FW_DATA_CACHE
    async with _FW_DATA_CACHE_LOCK:
        if _FW_DATA_CACHE is not None:
            return _FW_DATA_CACHE
        sheet_id = get_milestones_sheet_id().strip()
        tabs = await asyncio.gather(
            milestones_config.arequire_value(_FW_FACTIONS_KEY),
            milestones_config.arequire_value(_FW_CHAMPION_GUIDES_KEY),
            milestones_config.arequire_value(_FW_HARD_STAGE_CONDITIONS_KEY),
            milestones_config.arequire_value(_FW_HARD_STAGE_SOLVERS_KEY),
        )
        rows = await _gather_rows(sheet_id, *tabs)
        _FW_DATA_CACHE = FactionWarsData(*rows)
        return _FW_DATA_CACHE


def _format_template(template: str, values: Mapping[str, object]) -> str:
    result = template
    for key, value in values.items():
        result = result.replace("{" + key + "}", _text(value))
    return result


def _fw_enabled_rows(
    rows: Sequence[Mapping[str, object]],
) -> list[Mapping[str, object]]:
    return [
        row
        for row in rows
        if not _text(row.get("enabled")) or _truthy(row.get("enabled"))
    ]


def _fw_mode(category: str) -> str:
    return "hard" if category == _FW_HARD_CATEGORY else "normal"


def _fw_row_matches_category(row: Mapping[str, object], category: str) -> bool:
    row_category = _text(row.get("category"))
    if row_category and row_category != category:
        return False
    row_mode = _text(row.get("mode")).casefold()
    return not row_mode or row_mode == _fw_mode(category)


def _fw_faction_key(row: Mapping[str, object]) -> str:
    return _text(row.get("faction_key") or row.get("counter_key"))


def _fw_faction_name(row: Mapping[str, object]) -> str:
    return _text(
        row.get("faction_name")
        or row.get("name")
        or row.get("counter_label")
        or _fw_faction_key(row)
    )


def _fw_faction_rows(data: FactionWarsData) -> list[Mapping[str, object]]:
    return sorted(
        _fw_enabled_rows(data.factions),
        key=lambda r: (_fw_faction_name(r).casefold(), _fw_faction_key(r).casefold()),
    )


def _fw_faction_options(data: FactionWarsData) -> list[tuple[str, str, int]]:
    options: list[tuple[str, str, int]] = []
    seen: set[str] = set()
    for row in _fw_faction_rows(data):
        key = _fw_faction_key(row)
        if not key or key in seen:
            continue
        seen.add(key)
        options.append((key, _fw_faction_name(row) or key, _fw_max_stars(row)))
    return options


def _fw_faction_lookup(data: FactionWarsData) -> dict[str, tuple[str, int]]:
    return {
        key: (label, max_stars) for key, label, max_stars in _fw_faction_options(data)
    }


def _fw_max_stars(row: Mapping[str, object] | None) -> int:
    if row is None:
        return 63
    return _int_or_none(row.get("max_stars")) or 63


async def _fw_user_counter_rows() -> tuple[str, list[dict[str, Any]]]:
    sheet_id = get_milestones_sheet_id().strip()
    tab = await milestones_config.arequire_value(_FW_USER_COUNTERS_KEY)
    return tab, await afetch_records(sheet_id, tab)


def _fw_user_rows(
    rows: Sequence[Mapping[str, object]], user_id: int, category: str
) -> list[Mapping[str, object]]:
    user = str(user_id)
    return [
        row
        for row in rows
        if _text(row.get("user_id")) == user and _text(row.get("category")) == category
    ]


def _fw_counter_value(row: Mapping[str, object]) -> int:
    return _int_or_none(row.get("current_value")) or 0


def _fw_goal_value(row: Mapping[str, object], fallback: int = 63) -> int:
    return _int_or_none(row.get("goal_value")) or fallback


def _fw_rows_by_key(
    rows: Sequence[Mapping[str, object]],
) -> dict[str, Mapping[str, object]]:
    return {_fw_faction_key(row): row for row in rows if _fw_faction_key(row)}


def build_faction_wars_stars_embed(
    post: ForumPost,
    fw: FactionWarsData,
    user_rows: Sequence[Mapping[str, object]],
) -> discord.Embed:
    title = post.counter_stars_title or f"{post.label or post.category} — My Stars"
    if not user_rows:
        return discord.Embed(
            title=_embed_title(title),
            description=_embed_description(
                post.counter_stars_empty_description
                or "No Faction Wars stars are saved for you yet."
            ),
            color=discord.Color.blurple(),
        )
    lookup = _fw_faction_lookup(fw)
    lines = []
    total = 0
    for row in sorted(user_rows, key=lambda r: _fw_faction_name(r).casefold())[:25]:
        key = _fw_faction_key(row)
        label, fallback_goal = lookup.get(key, (_fw_faction_name(row) or key, 63))
        stars = _fw_counter_value(row)
        goal = _fw_goal_value(row, fallback_goal)
        total += stars
        status = _text(row.get("status"))
        suffix = f" — {status}" if status else ""
        lines.append(f"- {label}: {stars}/{goal}⭐{suffix}")
    description = f"Total saved stars: **{total}**\n\n" + "\n".join(lines)
    return discord.Embed(
        title=_embed_title(title),
        description=_embed_description(description),
        color=discord.Color.blurple(),
    )


def build_faction_wars_progress_embed(
    post: ForumPost,
    fw: FactionWarsData,
    user_rows: Sequence[Mapping[str, object]],
) -> discord.Embed:
    lookup = _fw_faction_lookup(fw)
    saved = _fw_rows_by_key(user_rows)
    progress_rows: list[tuple[str, str, int, int]] = []
    for key, label, max_stars in _fw_faction_options(fw):
        row = saved.get(key)
        current = _fw_counter_value(row) if row else 0
        goal = _fw_goal_value(row, max_stars) if row else max_stars
        progress_rows.append((key, label, current, goal))
    for key, row in saved.items():
        if key not in lookup:
            label = _fw_faction_name(row) or key
            current = _fw_counter_value(row)
            progress_rows.append((key, label, current, _fw_goal_value(row)))
    total = sum(current for _key, _label, current, _goal in progress_rows)
    max_total = sum(goal for _key, _label, _current, goal in progress_rows)
    remaining = max(max_total - total, 0)
    finished = [r for r in progress_rows if r[3] and r[2] >= r[3]]
    close = [r for r in progress_rows if r[3] and 0 < r[3] - r[2] <= 3]
    focus = sorted(
        [r for r in progress_rows if r[2] < r[3]], key=lambda r: (r[2], r[1])
    )[:5]
    if not user_rows and post.counter_progress_empty_description:
        description = post.counter_progress_empty_description
    else:
        values = {
            "category": post.category,
            "category_label": post.label or post.category,
            "current_value": total,
            "goal_value": max_total,
            "percent_complete": _format_percent(total, max_total),
            "remaining_value": remaining,
        }
        description = _format_template(
            post.counter_progress_intro_template
            or "Total stars: {current_value}/{goal_value}⭐ ({percent_complete}% complete)",
            values,
        )
    embed = discord.Embed(
        title=_embed_title(
            post.counter_progress_title or f"{post.label or post.category} — Progress"
        ),
        description=_embed_description(description),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name=_embed_title(
            post.counter_progress_finished_field_title or "Finished factions"
        ),
        value=_limited_bullets(
            [f"{label}: {current}/{goal}⭐" for _k, label, current, goal in finished], 8
        )
        or "None yet.",
        inline=False,
    )
    embed.add_field(
        name=_embed_title(post.counter_progress_close_field_title or "Close to finish"),
        value=_limited_bullets(
            [f"{label}: {current}/{goal}⭐" for _k, label, current, goal in close], 8
        )
        or "None yet.",
        inline=False,
    )
    embed.add_field(
        name=_embed_title(post.counter_progress_focus_field_title or "Focus factions"),
        value=_limited_bullets(
            [f"{label}: {current}/{goal}⭐" for _k, label, current, goal in focus], 8
        )
        or "All tracked factions are complete.",
        inline=False,
    )
    if post.counter_progress_footer:
        embed.set_footer(text=_limit_text(post.counter_progress_footer, 2048))
    return embed


def _fw_champion_guide_row(
    fw: FactionWarsData, category: str, faction_key: str
) -> Mapping[str, object] | None:
    return next(
        (
            row
            for row in _fw_enabled_rows(fw.champion_guides)
            if _fw_row_matches_category(row, category)
            and _fw_faction_key(row) == faction_key
        ),
        None,
    )


def build_faction_wars_guide_embed(
    post: ForumPost, fw: FactionWarsData, faction_key: str
) -> discord.Embed:
    lookup = _fw_faction_lookup(fw)
    row = _fw_champion_guide_row(fw, post.category, faction_key)
    label = (
        _fw_faction_name(row) if row else lookup.get(faction_key, (faction_key, 63))[0]
    )
    embed = discord.Embed(
        title=_embed_title(post.faction_guide_title or f"{label} — Faction Guide"),
        color=discord.Color.blurple(),
    )
    if row is None:
        embed.description = _embed_description(
            post.faction_guide_empty_description
            or "No champion guide is configured for that faction yet."
        )
        return embed
    for title, key, fallback in (
        (
            post.faction_guide_champions_field_title,
            "recommended_champions",
            "Recommended champions",
        ),
        (post.faction_guide_roles_field_title, "core_roles", "Core roles"),
        (
            post.faction_guide_accessible_field_title,
            "accessible_options",
            "Accessible options",
        ),
        (post.faction_guide_note_field_title, "planning_note", "Planning note"),
    ):
        value = _strip_visible_urls(row.get(key))
        if value:
            embed.add_field(
                name=_embed_title(title or fallback),
                value=_limit_text(value, _FIELD_LIMIT),
                inline=False,
            )
    if not embed.fields:
        embed.description = _embed_description(
            post.faction_guide_empty_description
            or "No champion guide details are configured for that faction yet."
        )
    return embed


def _fw_condition_row(
    fw: FactionWarsData, category: str, faction_key: str
) -> Mapping[str, object] | None:
    return next(
        (
            row
            for row in _fw_enabled_rows(fw.hard_stage_conditions)
            if _fw_row_matches_category(row, category)
            and _fw_faction_key(row) == faction_key
        ),
        None,
    )


def _fw_solver_rows(
    fw: FactionWarsData, category: str, faction_key: str
) -> dict[int, list[Mapping[str, object]]]:
    solvers: dict[int, list[Mapping[str, object]]] = {}
    for row in _fw_enabled_rows(fw.hard_stage_solvers):
        if (
            not _fw_row_matches_category(row, category)
            or _fw_faction_key(row) != faction_key
        ):
            continue
        stage = _int_or_none(row.get("stage_number"))
        if stage is not None:
            solvers.setdefault(stage, []).append(row)
    return solvers


def build_faction_wars_conditions_embed(
    post: ForumPost, fw: FactionWarsData, faction_key: str
) -> discord.Embed:
    lookup = _fw_faction_lookup(fw)
    row = _fw_condition_row(fw, post.category, faction_key)
    label = (
        _fw_faction_name(row) if row else lookup.get(faction_key, (faction_key, 63))[0]
    )
    embed = discord.Embed(
        title=_embed_title(post.conditions_title or f"{label} — Hard Conditions"),
        color=discord.Color.blurple(),
    )
    if row is None:
        embed.description = _embed_description(
            post.conditions_empty_description
            or "No hard mode conditions are configured for that faction yet."
        )
        return embed
    summary = _strip_visible_urls(row.get("challenge_summary"))
    if summary:
        embed.add_field(
            name=_embed_title(
                post.conditions_summary_field_title or "Challenge summary"
            ),
            value=_limit_text(summary, _FIELD_LIMIT),
            inline=False,
        )
    solver_map = _fw_solver_rows(fw, post.category, faction_key)
    stage_lines = []
    solver_lines = []
    for stage in range(1, 22):
        condition = _strip_visible_urls(row.get(f"stage_{stage}"))
        if condition:
            stage_lines.append(f"{stage}: {condition}")
        for solver in solver_map.get(stage, []):
            parts = [
                _strip_visible_urls(solver.get("condition")),
                _strip_visible_urls(solver.get("solver_roles")),
                _strip_visible_urls(solver.get("suggested_champions")),
                _strip_visible_urls(solver.get("notes")),
            ]
            detail = " • ".join(part for part in parts if part)
            if detail:
                solver_lines.append(f"{stage} solver: {detail}")
    combined_stage_lines = [*stage_lines, *solver_lines]
    stages_value = _limited_bullets(combined_stage_lines, 25)
    if stages_value:
        embed.add_field(
            name=_embed_title(post.conditions_stages_field_title or "Stage conditions"),
            value=stages_value,
            inline=False,
        )
    if not embed.fields:
        embed.description = _embed_description(
            post.conditions_empty_description
            or "No hard mode condition details are configured for that faction yet."
        )
    return embed


class FactionWarsStarModal(discord.ui.Modal):
    def __init__(
        self, post: ForumPost, faction_key: str, faction_label: str, max_stars: int
    ) -> None:
        super().__init__(
            title=_limit_text(
                post.counter_stars_modal_title or f"Set {faction_label} stars", 45
            )
        )
        self.post = post
        self.faction_key = faction_key
        self.faction_label = faction_label
        self.max_stars = max_stars
        self.stars = discord.ui.TextInput(
            label=_limit_text(
                post.counter_stars_modal_value_label or "Saved stars", 45
            ),
            placeholder=f"0-{max_stars}",
            required=True,
            max_length=3,
        )
        self.add_item(self.stars)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = _text(self.stars.value)
        try:
            value = int(raw)
        except ValueError:
            await interaction.response.send_message(
                embed=_progress_notice_embed(
                    self.post,
                    self.post.counter_stars_invalid_value_description
                    or "Enter a whole number of stars.",
                ),
                ephemeral=True,
            )
            return
        if value < 0 or value > self.max_stars:
            await interaction.response.send_message(
                embed=_progress_notice_embed(
                    self.post,
                    self.post.counter_stars_invalid_value_description
                    or f"Enter a star value from 0 to {self.max_stars}.",
                ),
                ephemeral=True,
            )
            return
        await upsert_faction_wars_counter(
            interaction.user.id,
            self.post.category,
            self.faction_key,
            self.faction_label,
            value,
            self.max_stars,
        )
        saved_template = (
            self.post.counter_stars_saved_description
            or "Saved {current_value}/{goal_value} stars for {counter_label}."
        )
        saved_description = _format_template(
            saved_template,
            {
                "counter_key": self.faction_key,
                "counter_label": self.faction_label,
                "current_value": value,
                "goal_value": self.max_stars,
                "category": self.post.category,
                "category_label": self.post.label or self.post.category,
            },
        )
        await interaction.response.send_message(
            embed=_progress_notice_embed(self.post, saved_description),
            ephemeral=True,
        )


class FactionWarsFactionSelect(discord.ui.Select):
    def __init__(self, post: ForumPost, fw: FactionWarsData, kind: str) -> None:
        self.post = post
        self.kind = kind
        options = [
            discord.SelectOption(label=_select_label(label), value=key)
            for key, label, _max_stars in _fw_faction_options(fw)[:25]
        ]
        placeholder = {
            "stars": post.counter_stars_faction_select_placeholder,
            "guide": post.faction_guide_select_placeholder,
            "conditions": post.conditions_select_placeholder,
        }.get(kind) or "Choose a faction…"
        super().__init__(
            placeholder=_select_label(placeholder),
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        if not isinstance(view, FactionWarsFactionPickerView):
            return
        selected = self.values[0]
        label, max_stars = _fw_faction_lookup(view.fw).get(selected, (selected, 63))
        if view.kind == "stars":
            await interaction.response.send_modal(
                FactionWarsStarModal(view.post, selected, label, max_stars)
            )
            return
        if view.kind == "guide":
            embed = build_faction_wars_guide_embed(view.post, view.fw, selected)
        else:
            embed = build_faction_wars_conditions_embed(view.post, view.fw, selected)
        await interaction.response.edit_message(embed=embed, view=view)


class FactionWarsFactionPickerView(discord.ui.View):
    def __init__(self, post: ForumPost, fw: FactionWarsData, kind: str) -> None:
        super().__init__(timeout=900)
        self.post = post
        self.fw = fw
        self.kind = kind
        if _fw_faction_options(fw):
            self.add_item(FactionWarsFactionSelect(post, fw, kind))


async def upsert_faction_wars_counter(
    user_id: int,
    category: str,
    counter_key: str,
    counter_label: str,
    current_value: int,
    goal_value: int,
) -> None:
    sheet_id = get_milestones_sheet_id().strip()
    tab, rows = await _fw_user_counter_rows()
    worksheet = await aget_worksheet(sheet_id, tab)
    header = await _load_header(sheet_id, tab)
    now = datetime.now(timezone.utc).isoformat()
    values = {
        "user_id": str(user_id),
        "category": category,
        "counter_key": counter_key,
        "counter_label": counter_label,
        "current_value": str(current_value),
        "goal_value": str(goal_value),
        "status": (
            "complete" if goal_value and current_value >= goal_value else "in_progress"
        ),
        "notes": "",
        "updated_at_utc": now,
    }
    found: tuple[int, Mapping[str, object]] | None = None
    for row_number, row in enumerate(rows, start=2):
        if (
            _text(row.get("user_id")) == str(user_id)
            and _text(row.get("category")) == category
            and _text(row.get("counter_key")) == counter_key
        ):
            found = (row_number, row)
            break
    if found is None:
        await acall_with_backoff(
            worksheet.append_row,
            [values.get(name, "") for name in header],
            value_input_option="RAW",
        )
        return
    row_number, existing = found
    full_row = [
        (
            _text(existing.get(name))
            if name == "notes"
            else values.get(name, _text(existing.get(name)))
        )
        for name in header
    ]
    await acall_with_backoff(
        worksheet.update,
        f"{_column_label(0)}{row_number}:{_column_label(len(header) - 1)}{row_number}",
        [full_row],
        value_input_option="RAW",
    )


class FactionWarsPanelButton(discord.ui.Button):
    def __init__(self, category: str, label: str, kind: str) -> None:
        self.category = category
        self.kind = kind
        prefixes = {
            "stars": _FW_STARS_CUSTOM_ID_PREFIX,
            "progress": _FW_PROGRESS_CUSTOM_ID_PREFIX,
            "guide": _FW_GUIDE_CUSTOM_ID_PREFIX,
            "conditions": _FW_CONDITIONS_CUSTOM_ID_PREFIX,
        }
        super().__init__(
            label=_button_label(label),
            style=discord.ButtonStyle.secondary,
            custom_id=f"{prefixes[kind]}{category}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        post: ForumPost | None = None
        try:
            data = await get_or_load_progress_guide_data()
            post = _post_for_category(self.category, data)
            if post is None or self.category not in _FACTION_WARS_CATEGORIES:
                await interaction.followup.send(
                    embed=_progress_unavailable_embed(post), ephemeral=True
                )
                return
            if self.kind == "conditions" and self.category != _FW_HARD_CATEGORY:
                await interaction.followup.send(
                    embed=discord.Embed(
                        title="Hard mode only",
                        description="Conditions are only available for hard mode Faction Wars.",
                        color=discord.Color.red(),
                    ),
                    ephemeral=True,
                )
                return
            fw = await get_or_load_faction_wars_data()
            if self.kind == "stars":
                _tab, rows = await _fw_user_counter_rows()
                user_rows = _fw_user_rows(rows, interaction.user.id, self.category)
                await interaction.followup.send(
                    embed=build_faction_wars_stars_embed(post, fw, user_rows),
                    view=FactionWarsFactionPickerView(post, fw, "stars"),
                    ephemeral=True,
                )
                return
            if self.kind == "progress":
                _tab, rows = await _fw_user_counter_rows()
                user_rows = _fw_user_rows(rows, interaction.user.id, self.category)
                await interaction.followup.send(
                    embed=build_faction_wars_progress_embed(post, fw, user_rows),
                    ephemeral=True,
                )
                return
            view_kind = "guide" if self.kind == "guide" else "conditions"
            await interaction.followup.send(
                embed=discord.Embed(
                    title=_embed_title(
                        post.faction_guide_title
                        if view_kind == "guide"
                        else post.conditions_title
                    )
                    or "Choose a faction",
                    description="Choose a faction to view details.",
                    color=discord.Color.blurple(),
                ),
                view=FactionWarsFactionPickerView(post, fw, view_kind),
                ephemeral=True,
            )
        except Exception as exc:
            if _is_quota_failure(exc):
                await interaction.followup.send(
                    embed=_progress_unavailable_embed(post), ephemeral=True
                )
                return
            raise


class FactionWarsPersistentView(discord.ui.View):
    def __init__(self, categories: Sequence[str] = _FACTION_WARS_CATEGORIES) -> None:
        super().__init__(timeout=None)
        for category in categories:
            self.add_item(FactionWarsPanelButton(category, "My Stars", "stars"))
            self.add_item(FactionWarsPanelButton(category, "Progress", "progress"))
            self.add_item(FactionWarsPanelButton(category, "Faction Guide", "guide"))
            if category == _FW_HARD_CATEGORY:
                self.add_item(
                    FactionWarsPanelButton(category, "Conditions", "conditions")
                )


def _add_faction_wars_panel_buttons(view: discord.ui.View, post: ForumPost) -> bool:
    if post.category not in _FACTION_WARS_CATEGORIES:
        return False
    view.add_item(
        FactionWarsPanelButton(
            post.category, post.counter_stars_button_label or "My Stars", "stars"
        )
    )
    view.add_item(
        FactionWarsPanelButton(
            post.category, post.counter_progress_button_label or "Progress", "progress"
        )
    )
    view.add_item(
        FactionWarsPanelButton(
            post.category, post.faction_guide_button_label or "Faction Guide", "guide"
        )
    )
    if post.category == _FW_HARD_CATEGORY:
        view.add_item(
            FactionWarsPanelButton(
                post.category,
                post.conditions_button_label or "Conditions",
                "conditions",
            )
        )
    return True


def build_help_embed(post: ForumPost) -> discord.Embed | None:
    if not (post.help_panel_title or post.help_panel_description):
        return None
    embed = discord.Embed(
        title=_embed_title(post.help_panel_title),
        description=_embed_description(post.help_panel_description),
        color=discord.Color.blurple(),
    )
    if post.help_panel_footer:
        embed.set_footer(text=_limit_text(post.help_panel_footer, 2048))
    return embed


def build_help_view(post: ForumPost, data: ProgressGuideData) -> discord.ui.View | None:
    view = discord.ui.View(timeout=None)
    added = False
    if data.faq_by_category.get(post.category):
        view.add_item(
            ProgressGuideFAQButton(post.category, post.faq_button_label or "FAQ")
        )
        added = True
    if _add_faction_wars_panel_buttons(view, post):
        added = True
    if _supports_missions(post):
        view.add_item(
            ProgressGuideMissionButton(
                post.category, post.mission_list_button_label or "Mission List"
            )
        )
        added = True
    guide_url = _safe_url(post.guide_post_url)
    if guide_url:
        view.add_item(
            discord.ui.Button(
                label=_button_label(post.help_back_button_label or "Back"),
                style=discord.ButtonStyle.link,
                url=guide_url,
            )
        )
        added = True
    return view if added else None


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
    if post.guide_footer:
        embed.set_footer(text=_limit_text(post.guide_footer, 2048))
    return embed


def build_how_to_use_embed(post: ForumPost) -> discord.Embed:
    return discord.Embed(
        title=_embed_title(post.how_to_use_title) or None,
        description=_embed_description(post.how_to_use_description),
        color=discord.Color.blurple(),
    )


class ProgressGuideHowToUseButton(discord.ui.Button):
    def __init__(self, category: str, label: str = "How to use") -> None:
        self.category = category
        super().__init__(
            label=_button_label(label or "How to use"),
            style=discord.ButtonStyle.secondary,
            custom_id=f"{_HOW_TO_USE_CUSTOM_ID_PREFIX}{category}",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            data = await get_or_load_progress_guide_data()
            post = _post_for_category(self.category, data)
            if post is None or not _supports_how_to_use(post):
                embed = discord.Embed(
                    title="Progress guide help unavailable",
                    description="This guide helper is not configured right now.",
                    color=discord.Color.red(),
                )
            else:
                embed = build_how_to_use_embed(post)
        except Exception:
            embed = discord.Embed(
                title="Progress guide help unavailable",
                description=(
                    "I couldn’t load that guide helper right now. "
                    "Please try again later."
                ),
                color=discord.Color.red(),
            )
        await interaction.followup.send(embed=embed, ephemeral=True)


def build_guide_view(
    post: ForumPost, data: ProgressGuideData
) -> discord.ui.View | None:
    view = discord.ui.View(timeout=None)
    added = False
    help_url = _safe_url(post.help_post_url)
    if _add_faction_wars_panel_buttons(view, post):
        added = True
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
    if _supports_plan_ahead(post):
        view.add_item(PlanAheadButton(post.category, post.plan_ahead_button_label))
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
    if _supports_how_to_use(post):
        view.add_item(
            ProgressGuideHowToUseButton(post.category, post.how_to_use_button_label)
        )
        added = True
    return view if added else None


async def publish_or_refresh(bot: commands.Bot, *, refresh: bool) -> PublishSummary:
    """Publish or refresh progress guide and managed help panels."""

    data = await load_progress_guide_data()
    clear_mission_cache()
    set_progress_guide_cache(data)
    sheet_id = get_milestones_sheet_id().strip()
    sheet_state = _ForumPostsSheetState(sheet_id, data.forum_posts_tab)
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
        initial_help_post_url = post.help_post_url
        try:
            message = None
            guide_created = False
            if post.guide_panel_message_id:
                message = await _fetch_message_or_none(
                    channel, post.guide_panel_message_id
                )
                if message is None and not refresh:
                    summary.skipped.append(
                        f"{label}: stored panel missing; run refresh to recreate"
                    )
                    continue
            if message is None:
                message = await channel.send(embed=embed, view=build_guide_view(post, data))  # type: ignore[attr-defined]
                guide_created = True
                try:
                    await sheet_state.write_if_changed(
                        post.row_number,
                        _MESSAGE_ID_COLUMN,
                        str(message.id),
                        post.guide_panel_message_id,
                    )
                    post.guide_panel_message_id = message.id
                except Exception:
                    await _delete_untracked_message(message)
                    raise
            jump_url = getattr(message, "jump_url", "")
            if jump_url:
                await sheet_state.write_if_changed(
                    post.row_number,
                    _GUIDE_POST_URL_COLUMN,
                    jump_url,
                    post.guide_post_url,
                )
                post.guide_post_url = jump_url
            await _publish_or_refresh_help_post(
                bot, post, data, sheet_state, summary, label
            )
            final_view = build_guide_view(post, data)
            if guide_created:
                if post.help_post_url != initial_help_post_url:
                    await message.edit(embed=embed, view=final_view)
                summary.created += 1
            else:
                await message.edit(embed=embed, view=final_view)
                summary.refreshed += 1
        except Exception as exc:
            if is_rate_limited_error(exc):
                raise
            summary.failures.append(f"{label}: {type(exc).__name__}: {exc}")
    return summary


async def _publish_or_refresh_help_post(
    bot: commands.Bot,
    post: ForumPost,
    data: ProgressGuideData,
    sheet_state: "_ForumPostsSheetState",
    summary: PublishSummary,
    label: str,
) -> None:
    target_id = post.help_thread_id or post.help_channel_id
    if target_id is None:
        summary.skipped.append(f"{label}: missing help destination")
        return
    embed = build_help_embed(post)
    if embed is None:
        summary.skipped.append(f"{label}: missing help panel copy")
        return
    channel = await _resolve_messageable(bot, target_id)
    if channel is None:
        summary.failures.append(f"{label}: invalid help destination {target_id}")
        return
    view = build_help_view(post, data)
    try:
        if post.help_panel_message_id:
            message = await _fetch_message_or_none(channel, post.help_panel_message_id)
            if message is not None:
                await message.edit(embed=embed, view=view, content=None)
                jump_url = getattr(message, "jump_url", "")
                if jump_url:
                    await sheet_state.write_if_changed(
                        post.row_number,
                        _HELP_POST_URL_COLUMN,
                        jump_url,
                        post.help_post_url,
                    )
                    post.help_post_url = jump_url
                summary.refreshed += 1
                return
        message = await channel.send(embed=embed, view=view)  # type: ignore[attr-defined]
        jump_url = getattr(message, "jump_url", "")
        try:
            await sheet_state.write_if_changed(
                post.row_number,
                _HELP_MESSAGE_ID_COLUMN,
                str(message.id),
                post.help_panel_message_id,
            )
            if jump_url:
                await sheet_state.write_if_changed(
                    post.row_number, _HELP_POST_URL_COLUMN, jump_url, post.help_post_url
                )
                post.help_post_url = jump_url
        except Exception:
            await _delete_untracked_message(message)
            raise
        summary.created += 1
    except Exception as exc:
        if is_rate_limited_error(exc):
            raise
        summary.failures.append(f"{label} help: {type(exc).__name__}: {exc}")


async def _fetch_message_or_none(
    channel: discord.abc.Messageable, message_id: int
) -> discord.Message | None:
    try:
        return await channel.fetch_message(message_id)  # type: ignore[attr-defined]
    except discord.NotFound:
        return None


class _ForumPostsSheetState:
    def __init__(self, sheet_id: str, tab: str) -> None:
        self.sheet_id = sheet_id
        self.tab = tab
        self.worksheet = None
        self.header: list[str] | None = None

    async def _ensure_loaded(self) -> None:
        if self.worksheet is None:
            self.worksheet = await aget_worksheet(self.sheet_id, self.tab)
        if self.header is None:
            self.header = await _load_header(self.sheet_id, self.tab)

    async def ensure_help_columns(self) -> None:
        await self._ensure_loaded()
        assert self.header is not None
        missing = [col for col in _HELP_PANEL_COLUMNS if col not in self.header]
        if not missing:
            return
        self.header.extend(missing)
        assert self.worksheet is not None
        end_col = _column_label(len(self.header) - 1)
        await acall_with_backoff(
            self.worksheet.update,
            f"A1:{end_col}1",
            [self.header],
            attempts=1,
            value_input_option="RAW",
        )

    async def write_if_changed(
        self, row_number: int, column: str, value: str, current: object
    ) -> None:
        if _text(current) == value:
            return
        await self.ensure_help_columns()
        assert self.header is not None and self.worksheet is not None
        col = _column_label(_header_index(self.header, column))
        await acall_with_backoff(
            self.worksheet.update,
            f"{col}{row_number}",
            [[value]],
            attempts=1,
            value_input_option="RAW",
        )


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
