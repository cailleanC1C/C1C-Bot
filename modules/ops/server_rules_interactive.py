"""Interactive navigation and FAQ browsing for the Server Rules publisher."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from string import Formatter
from typing import Any, Callable

import discord

from modules.ops import server_rules as base

FAQ_SECTION = "faq"
RULE_NAV_SECTION = "navigation"
FAQ_NAV_SECTION = "faq_navigation"
FAQ_GROUP_CUSTOM_ID = "serverrules:faq:group:v1"
FAQ_QUESTION_CUSTOM_ID = "serverrules:faq:question:v1"
FAQ_SHARE_CHANNEL_CUSTOM_ID = "serverrules:faq:share_channel:v1"
FAQ_SHOW_ALL = "__all__"
MAX_TOPIC_QUESTIONS = 24
CACHE_TTL_KEY = "SERVER_RULES_FAQ_CACHE_TTL_SECONDS"

_UI_KEYS = {
    "group_select_placeholder": "SERVER_RULES_FAQ_GROUP_SELECT_PLACEHOLDER",
    "question_list_heading": "SERVER_RULES_FAQ_QUESTION_LIST_HEADING",
    "question_list_instruction": "SERVER_RULES_FAQ_QUESTION_LIST_INSTRUCTION",
    "question_select_placeholder": "SERVER_RULES_FAQ_QUESTION_SELECT_PLACEHOLDER",
    "show_all_label": "SERVER_RULES_FAQ_SHOW_ALL_LABEL",
    "show_all_description": "SERVER_RULES_FAQ_SHOW_ALL_DESCRIPTION",
    "share_answer_label": "SERVER_RULES_FAQ_SHARE_ANSWER_LABEL",
    "share_group_label": "SERVER_RULES_FAQ_SHARE_GROUP_LABEL",
    "shared_footer": "SERVER_RULES_FAQ_SHARED_FOOTER",
    "unavailable_text": "SERVER_RULES_FAQ_UNAVAILABLE_TEXT",
    "share_channel_placeholder": "SERVER_RULES_FAQ_SHARE_CHANNEL_PLACEHOLDER",
    "share_success_text": "SERVER_RULES_FAQ_SHARE_SUCCESS_TEXT",
    "share_permission_text": "SERVER_RULES_FAQ_SHARE_PERMISSION_TEXT",
    "share_failure_text": "SERVER_RULES_FAQ_SHARE_FAILURE_TEXT",
}


@dataclass(frozen=True)
class UI:
    group_select_placeholder: str
    question_list_heading: str
    question_list_instruction: str
    question_select_placeholder: str
    show_all_label: str
    show_all_description: str
    share_answer_label: str
    share_group_label: str
    shared_footer: str
    unavailable_text: str
    share_channel_placeholder: str
    share_success_text: str
    share_permission_text: str
    share_failure_text: str


@dataclass(frozen=True)
class Topic:
    key: str
    title: str
    questions: tuple[base.Row, ...]


_CACHE_LOCK = asyncio.Lock()
_CACHE: tuple[float, int, tuple[Topic, ...], UI] | None = None
_CACHE_GEN = 0


def invalidate_cache() -> None:
    global _CACHE, _CACHE_GEN
    _CACHE = None
    _CACHE_GEN += 1


def _section(row: base.Row) -> str:
    return row.data.get("section", "").strip().lower()


def _group_section(group: base.MessageGroup) -> str:
    return _section(group.first_row)


def _copy(embed: discord.Embed) -> discord.Embed:
    return discord.Embed.from_dict(embed.to_dict())


def _trim(text: str, limit: int = 100) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


async def _load_ui(*, force: bool = False) -> tuple[UI | None, list[tuple[str, str]]]:
    values: dict[str, str] = {}
    errors: list[tuple[str, str]] = []
    for index, (field, key) in enumerate(_UI_KEYS.items()):
        raw = await base.recruitment_sheet.get_config_value_async(
            key, None, force=force and index == 0
        )
        value = str(raw or "").strip()
        if not value:
            errors.append(("config", f"Config key {key} is missing"))
        values[field] = value
    if errors:
        return None, errors
    limits = {
        "group_select_placeholder": 150,
        "question_select_placeholder": 150,
        "show_all_label": 100,
        "show_all_description": 100,
        "share_answer_label": 80,
        "share_group_label": 80,
        "share_channel_placeholder": 150,
        "shared_footer": base.MAX_FOOTER,
        "unavailable_text": base.MAX_DESCRIPTION,
        "share_success_text": base.MAX_DESCRIPTION,
        "share_permission_text": base.MAX_DESCRIPTION,
        "share_failure_text": base.MAX_DESCRIPTION,
    }
    for field, limit in limits.items():
        if len(values[field]) > limit:
            errors.append(("config", f"Config key {_UI_KEYS[field]} exceeds Discord's {limit} character limit"))
    try:
        placeholders = [
            (name, spec, conv)
            for _literal, name, spec, conv in Formatter().parse(values["share_success_text"])
            if name is not None
        ]
    except ValueError:
        placeholders = [("invalid", "", None)]
    if any(name != "channel" or spec or conv for name, spec, conv in placeholders):
        errors.append(("config", "SERVER_RULES_FAQ_SHARE_SUCCESS_TEXT may only use {channel}"))
    return (UI(**values), []) if not errors else (None, errors)


async def _load_ttl() -> tuple[int | None, list[tuple[str, str]]]:
    raw = await base.recruitment_sheet.get_config_value_async(CACHE_TTL_KEY, None)
    try:
        ttl = int(str(raw or "").strip())
    except ValueError:
        return None, [("config", f"Config key {CACHE_TTL_KEY} must be an integer")]
    if not 0 <= ttl <= 86400:
        return None, [("config", f"Config key {CACHE_TTL_KEY} must be between 0 and 86400")]
    return ttl, []


def _catalog(rows: list[base.Row]) -> tuple[list[Topic], list[tuple[str, str]]]:
    faq = sorted(
        [row for row in rows if row.enabled and _section(row) == FAQ_SECTION],
        key=lambda row: (float(row.order or 0), row.row_number),
    )
    errors: list[tuple[str, str]] = []
    grouped: dict[str, list[base.Row]] = {}
    titles: dict[str, str] = {}
    keys: set[str] = set()
    for row in faq:
        label = row.key or f"row {row.row_number}"
        if not row.topic_key or not row.topic_title:
            errors.append((label, "enabled FAQ rows require topic_key and topic_title"))
            continue
        if len(row.topic_key) > 100 or len(row.topic_title) > 100:
            errors.append((label, "FAQ topic key/title exceeds Discord select limit"))
        if not row.key or len(row.key) > 100 or row.key in keys:
            errors.append((label, "FAQ message_key must be unique and at most 100 characters"))
        else:
            keys.add(row.key)
        if not row.data.get("title", "").strip():
            errors.append((label, "enabled FAQ rows require a question title"))
        existing = titles.setdefault(row.topic_key, row.topic_title)
        if existing != row.topic_title:
            errors.append((row.topic_key, "topic_title must be consistent within a topic"))
        grouped.setdefault(row.topic_key, []).append(row)
    topics = [Topic(key, titles[key], tuple(grouped[key])) for key in grouped]
    topics.sort(key=lambda topic: float(topic.questions[0].order or 0))
    if len(topics) > 25:
        errors.append(("faq", "FAQ has more than 25 groups"))
    for topic in topics:
        if len(topic.questions) > MAX_TOPIC_QUESTIONS:
            errors.append((topic.key, "FAQ group has more than 24 questions"))
    return topics, errors


def _navigation(groups: list[base.MessageGroup]):
    rule_nav = [g for g in groups if _group_section(g) == RULE_NAV_SECTION]
    faq_nav = [g for g in groups if _group_section(g) == FAQ_NAV_SECTION]
    errors: list[tuple[str, str]] = []
    if len(rule_nav) != 1:
        errors.append(("navigation", "exactly one enabled Rules navigation message is required"))
    if len(faq_nav) != 1:
        errors.append(("faq_navigation", "exactly one enabled FAQ navigation message is required"))
    if errors:
        return None, [], None, errors
    start = float(rule_nav[0].first_row.order or 0)
    end = float(faq_nav[0].first_row.order or 0)
    rules = [
        group for group in groups
        if _group_section(group) == "rules" and start < float(group.first_row.order or 0) < end
    ]
    rules.sort(key=lambda group: float(group.first_row.order or 0))
    if len(rules) != 6:
        errors.append(("navigation", "Rules navigation requires exactly six rule message groups"))
    return rule_nav[0], rules, faq_nav[0], errors


def _prepare_faq_rows(rows: list[base.Row]) -> list[tuple[str, str]]:
    errors: list[tuple[str, str]] = []
    for row in rows:
        if not row.enabled or _section(row) != FAQ_SECTION:
            continue
        label = row.key or f"row {row.row_number}"
        try:
            order = float(row.data.get("order", ""))
        except ValueError:
            errors.append((label, "enabled FAQ rows require numeric order"))
            continue
        if not math.isfinite(order):
            errors.append((label, "enabled FAQ rows require finite order"))
            continue
        row.order = order
        row.embed, embed_errors = base.build_embed(row)
        errors.extend((label, error) for error in embed_errors)
    ordered = sorted(
        [row for row in rows if row.enabled and _section(row) == FAQ_SECTION],
        key=lambda row: (float(row.order or 0), row.row_number),
    )
    errors.extend(base._validate_topic_runs(ordered))
    return errors


async def _load_state_uncached() -> tuple[list[Topic], UI | None, list[tuple[str, str]], int | None]:
    _tab, headers, rows, load_errors = await base.load_rows()
    errors = [("config", error) for error in load_errors]
    ui, ui_errors = await _load_ui()
    ttl, ttl_errors = await _load_ttl()
    errors.extend(ui_errors + ttl_errors)
    if not {"topic_key", "topic_title"}.issubset(headers):
        errors.append(("config", "ServerRulesFAQ requires topic_key and topic_title headers"))
    errors.extend(_prepare_faq_rows(rows))
    topics, topic_errors = _catalog(rows)
    errors.extend(topic_errors)
    return topics, ui, errors, ttl


async def load_state(*, force: bool = False) -> tuple[list[Topic], UI | None, list[tuple[str, str]]]:
    global _CACHE
    now = time.monotonic()
    if not force and _CACHE and _CACHE[1] > 0 and now - _CACHE[0] < _CACHE[1]:
        return list(_CACHE[2]), _CACHE[3], []
    async with _CACHE_LOCK:
        now = time.monotonic()
        if not force and _CACHE and _CACHE[1] > 0 and now - _CACHE[0] < _CACHE[1]:
            return list(_CACHE[2]), _CACHE[3], []
        generation = _CACHE_GEN
        topics, ui, errors, ttl = await _load_state_uncached()
        if generation == _CACHE_GEN and not errors and ui is not None and ttl and ttl > 0:
            _CACHE = (time.monotonic(), ttl, tuple(topics), ui)
        elif generation == _CACHE_GEN:
            _CACHE = None
        return topics, ui, errors


def _question_list(topic: Topic, ui: UI) -> discord.Embed:
    questions = "\n".join(
        f"**{index}.** {row.data.get('title', '').strip()}"
        for index, row in enumerate(topic.questions, 1)
    )
    return discord.Embed(
        title=topic.title,
        description=f"### {ui.question_list_heading}\n{questions}\n\n{ui.question_list_instruction}",
        colour=base.SERVER_RULES_FAQ_SLATE,
    )


def _answer_embeds(rows) -> list[discord.Embed]:
    return [_copy(row.embed) for row in rows if row.embed is not None]


def _batches(embeds: list[discord.Embed]) -> list[list[discord.Embed]]:
    result: list[list[discord.Embed]] = []
    batch: list[discord.Embed] = []
    total = 0
    for embed in embeds:
        size = base._embed_text_len(embed)
        if batch and (len(batch) >= base.MAX_EMBEDS_PER_MESSAGE or total + size > base.MAX_TOTAL):
            result.append(batch)
            batch, total = [], 0
        batch.append(embed)
        total += size
    if batch:
        result.append(batch)
    return result


def _shared_batches(rows, ui: UI) -> list[list[discord.Embed]]:
    embeds = _answer_embeds(rows)
    if embeds:
        old = getattr(getattr(embeds[-1], "footer", None), "text", None)
        embeds[-1].set_footer(text=f"{old}\n{ui.shared_footer}" if old else ui.shared_footer)
    return _batches(embeds)


def _error(ui: UI | None) -> discord.Embed:
    text = ui.unavailable_text if ui else "The FAQ is temporarily unavailable. Please try again later."
    return discord.Embed(description=text, colour=base.SERVER_RULES_FAQ_SLATE)


def _feedback(text: str, success: bool = False) -> discord.Embed:
    return discord.Embed(
        description=text,
        colour=base.SERVER_RULES_FAQ_GREEN if success else base.SERVER_RULES_FAQ_SLATE,
    )


def _find(topics: list[Topic], key: str) -> Topic | None:
    return next((topic for topic in topics if topic.key == key), None)


def _filter_builder(original: Callable):
    def build(rows):
        groups, errors = original(rows)
        return [g for g in groups if _group_section(g) != FAQ_SECTION], errors
    return build


async def _preflight(bot: discord.Client):
    target, _tab, headers, rows, summary = await base.preflight(bot)
    if summary is not None:
        return target, summary
    groups, group_errors = base.build_groups(rows)
    topics, topic_errors = _catalog(rows)
    _rn, _rules, _fn, nav_errors = _navigation(groups)
    ui, ui_errors = await _load_ui()
    _ttl, ttl_errors = await _load_ttl()
    errors = group_errors + topic_errors + nav_errors + ui_errors + ttl_errors
    if not {"topic_key", "topic_title"}.issubset(headers):
        errors.append(("config", "ServerRulesFAQ requires topic_key and topic_title headers"))
    if not topics:
        errors.append(("faq", "at least one enabled FAQ group is required"))
    if ui is not None:
        for topic in topics:
            errors.extend((topic.key, f"FAQ question list {err}") for err in base._validate_embed_payload([_question_list(topic, ui)]))
            for rows_to_share in [*[(row,) for row in topic.questions], topic.questions]:
                for batch in _shared_batches(rows_to_share, ui):
                    errors.extend((topic.key, f"FAQ shared payload {err}") for err in base._validate_embed_payload(batch))
    if not errors:
        return target, None
    result = base.Summary()
    for key, reason in errors:
        result.fail(key, reason)
    return target, result


def _jump(target: Any, message_id: str) -> str:
    guild_id = getattr(getattr(target, "guild", None), "id", None)
    channel_id = getattr(target, "id", None)
    if guild_id is None or channel_id is None or not base.valid_snowflake(message_id):
        raise ValueError("rule message cannot be linked")
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


def _nav_payload(target: Any, nav: base.MessageGroup, rules: list[base.MessageGroup]):
    if len(nav.payload_embeds) != 1:
        raise ValueError("Rules navigation must contain exactly one embed")
    embed = _copy(nav.payload_embeds[0])
    lines = []
    for group in rules:
        if not group.stored_message_id:
            raise ValueError(f"{group.key} has no stored message_id")
        title = (group.payload_embeds[0].title or group.key).strip()
        lines.append(f"• [{title}]({_jump(target, group.stored_message_id)})")
    intro = (embed.description or "").rstrip()
    embed.description = (intro + "\n\n" if intro else "") + "\n".join(lines)
    errors = base._validate_embed_payload([embed])
    if errors:
        raise ValueError("; ".join(errors))
    return [embed]


async def _fetch_group(target, group, bot_id):
    state, message, reason = await base._fetch(target, group.stored_message_id)
    if state is not base.FetchState.FOUND or message is None:
        return None, reason or "stored message was not found"
    keys = {group.key} | {row.topic_key for row in group.rows if row.topic_key}
    if not base._is_valid_stored_message(
        message,
        stored_message_id=group.stored_message_id,
        target=target,
        bot_id=bot_id,
        permitted_legacy_keys=keys,
    ):
        return None, "stored message is not managed by server rules"
    return message, ""


async def _cleanup_old_faq(target, tab, headers, rows, bot_id, summary) -> bool:
    if not await base._recover_pending(target, tab, headers, rows, bot_id, summary):
        return False
    updates = []
    for row in rows:
        if _section(row) != FAQ_SECTION or not row.message_id or base._is_recovery_artifact(row.message_id):
            continue
        if not base.valid_snowflake(row.message_id):
            summary.fail(row.key, "FAQ message_id is not a valid Discord snowflake")
            return False
        updates.append((row, base._recovery_value("", [row.message_id])))
    if not updates:
        return True
    try:
        await base._write_ids_batch(tab, headers, updates)
    except Exception:
        summary.fail("sheet", "failed to journal legacy FAQ message cleanup")
        return False
    for row, value in updates:
        row.data["message_id"] = value
    return await base._recover_pending(target, tab, headers, rows, bot_id, summary)


async def _postprocess(bot, target, summary, *, cleanup_faq: bool):
    checked, tab, headers, rows, errors = await base.preflight(bot)
    if errors is not None:
        for key, reasons in errors.failures.items():
            for reason in reasons:
                summary.fail(key, reason)
        return
    if checked is None:
        summary.fail("config", "Rules destination is unavailable")
        return
    topics, topic_errors = _catalog(rows)
    ui, ui_errors = await _load_ui()
    groups, group_errors = base.build_groups(rows)
    rule_nav, rules, faq_nav, nav_errors = _navigation(groups)
    all_errors = topic_errors + ui_errors + group_errors + nav_errors
    if all_errors or ui is None or rule_nav is None or faq_nav is None:
        for key, reason in all_errors:
            summary.fail(key, reason)
        return
    bot_id = getattr(getattr(bot, "user", None), "id", None)
    if cleanup_faq and not await _cleanup_old_faq(target, tab, headers, rows, bot_id, summary):
        return
    nav_message, reason = await _fetch_group(target, rule_nav, bot_id)
    if nav_message is None:
        summary.fail(rule_nav.key, reason)
    else:
        try:
            await nav_message.edit(content=None, embeds=_nav_payload(target, rule_nav, rules))
        except Exception:
            summary.fail(rule_nav.key, "failed to build or edit Rules navigation links")
    faq_message, reason = await _fetch_group(target, faq_nav, bot_id)
    if faq_message is None:
        summary.fail(faq_nav.key, reason)
    else:
        try:
            await faq_message.edit(
                content=None,
                embeds=[_copy(embed) for embed in faq_nav.payload_embeds],
                view=FAQGroupView(bot, topics, ui),
            )
        except Exception:
            summary.fail(faq_nav.key, "failed to attach the persistent FAQ selector")


async def publish(bot: discord.Client):
    async with base.MUTATION_LOCK:
        target, validation = await _preflight(bot)
        if validation is not None:
            return validation, target
        original = base.build_groups
        base.build_groups = _filter_builder(original)
        try:
            summary, target = await base._publish(bot)
        finally:
            base.build_groups = original
        if target is not None and summary.created:
            await _postprocess(bot, target, summary, cleanup_faq=True)
        invalidate_cache()
        return summary, target


async def refresh(bot: discord.Client):
    async with base.MUTATION_LOCK:
        target, validation = await _preflight(bot)
        if validation is not None:
            return validation, target
        original = base.build_groups
        base.build_groups = _filter_builder(original)
        try:
            summary, target = await base._refresh(bot)
        finally:
            base.build_groups = original
        if target is not None:
            await _postprocess(bot, target, summary, cleanup_faq=False)
        invalidate_cache()
        return summary, target


_THREAD_TYPES = {
    value for value in (
        getattr(discord.ChannelType, "public_thread", None),
        getattr(discord.ChannelType, "private_thread", None),
        getattr(discord.ChannelType, "news_thread", None),
    ) if value is not None
}
_SHARE_TYPES = [
    value for value in (
        getattr(discord.ChannelType, "text", None),
        getattr(discord.ChannelType, "news", None),
        getattr(discord.ChannelType, "public_thread", None),
        getattr(discord.ChannelType, "private_thread", None),
        getattr(discord.ChannelType, "news_thread", None),
    ) if value is not None
]


def _can_send(permissions: Any, thread: bool) -> bool:
    if permissions is None or not getattr(permissions, "view_channel", False):
        return False
    return bool(getattr(permissions, "send_messages_in_threads" if thread else "send_messages", False))


def _selected_ok(selected: Any, interaction: discord.Interaction) -> bool:
    channel_type = getattr(selected, "type", None)
    if getattr(selected, "guild_id", None) != getattr(interaction, "guild_id", None):
        return False
    if channel_type not in _SHARE_TYPES:
        return False
    thread = channel_type in _THREAD_TYPES
    if thread and (getattr(selected, "archived", False) or getattr(selected, "locked", False)):
        return False
    return _can_send(getattr(selected, "permissions", None), thread)


async def _resolve(selected: Any):
    resolver = getattr(selected, "resolve", None)
    if callable(resolver):
        resolved = resolver()
        if resolved is not None:
            return resolved
    fetch = getattr(selected, "fetch", None)
    if callable(fetch):
        try:
            return await fetch()
        except Exception:
            return None
    return None


def _bot_ok(channel: Any, interaction: discord.Interaction) -> bool:
    guild = getattr(interaction, "guild", None)
    member = getattr(guild, "me", None)
    permissions_for = getattr(channel, "permissions_for", None)
    if member is None or not callable(permissions_for):
        return False
    try:
        permissions = permissions_for(member)
    except Exception:
        return False
    channel_type = getattr(channel, "type", None)
    thread = channel_type in _THREAD_TYPES
    if thread and (getattr(channel, "archived", False) or getattr(channel, "locked", False)):
        return False
    return _can_send(permissions, thread) and bool(getattr(permissions, "embed_links", False))


async def _send_batches(target: Any, batches: list[list[discord.Embed]]) -> bool:
    sent = []
    try:
        for batch in batches:
            sent.append(await target.send(embeds=batch))
        return True
    except Exception:
        for message in reversed(sent):
            try:
                await message.delete()
            except Exception:
                pass
        return False


class FAQGroupSelect(discord.ui.Select):
    def __init__(self, bot: discord.Client, topics: list[Topic], ui: UI | None = None):
        self.bot = bot
        options = [discord.SelectOption(label=topic.title, value=topic.key) for topic in topics]
        if not options:
            options = [discord.SelectOption(label="FAQ", value="__routing__")]
        super().__init__(
            placeholder=ui.group_select_placeholder if ui else None,
            options=options,
            min_values=1,
            max_values=1,
            custom_id=FAQ_GROUP_CUSTOM_ID,
        )

    async def callback(self, interaction: discord.Interaction):
        topics, ui, errors = await load_state()
        topic = _find(topics, self.values[0]) if not errors else None
        if topic is None or ui is None:
            await interaction.response.send_message(embed=_error(ui), ephemeral=True)
            return
        await interaction.response.send_message(
            embed=_question_list(topic, ui),
            view=FAQQuestionView(self.bot, topic, ui),
            ephemeral=True,
        )


class FAQGroupView(discord.ui.View):
    def __init__(self, bot: discord.Client, topics: list[Topic], ui: UI | None = None):
        super().__init__(timeout=None)
        self.add_item(FAQGroupSelect(bot, topics, ui))


class FAQQuestionSelect(discord.ui.Select):
    def __init__(self, bot: discord.Client, topic: Topic, ui: UI):
        self.bot = bot
        self.topic_key = topic.key
        options = [
            discord.SelectOption(label=ui.show_all_label, value=FAQ_SHOW_ALL, description=ui.show_all_description),
            *[
                discord.SelectOption(label=_trim(row.data.get("title", "")), value=row.key)
                for row in topic.questions
            ],
        ]
        super().__init__(
            placeholder=ui.question_select_placeholder,
            options=options,
            min_values=1,
            max_values=1,
            custom_id=FAQ_QUESTION_CUSTOM_ID,
        )

    async def callback(self, interaction: discord.Interaction):
        topics, ui, errors = await load_state()
        topic = _find(topics, self.topic_key) if not errors else None
        if topic is None or ui is None:
            await interaction.response.edit_message(embed=_error(ui), view=None)
            return
        choice = self.values[0]
        if choice == FAQ_SHOW_ALL:
            rows, question_key = topic.questions, None
        else:
            question = next((row for row in topic.questions if row.key == choice), None)
            if question is None:
                await interaction.response.edit_message(embed=_error(ui), view=None)
                return
            rows, question_key = (question,), question.key
        batches = _batches(_answer_embeds(rows))
        if not batches:
            await interaction.response.edit_message(embed=_error(ui), view=None)
            return
        await interaction.response.edit_message(
            embeds=batches[0],
            view=FAQShareView(self.bot, topic.key, question_key, ui),
        )
        for batch in batches[1:]:
            await interaction.followup.send(embeds=batch, ephemeral=True)


class FAQQuestionView(discord.ui.View):
    def __init__(self, bot: discord.Client, topic: Topic, ui: UI):
        super().__init__(timeout=900)
        self.add_item(FAQQuestionSelect(bot, topic, ui))


class FAQShareView(discord.ui.View):
    def __init__(self, bot: discord.Client, topic_key: str, question_key: str | None, ui: UI):
        super().__init__(timeout=900)
        self.bot, self.topic_key, self.question_key, self.ui = bot, topic_key, question_key, ui
        button = discord.ui.Button(
            label=ui.share_answer_label if question_key else ui.share_group_label,
            style=discord.ButtonStyle.primary,
        )
        button.callback = self._share
        self.add_item(button)

    async def _share(self, interaction: discord.Interaction):
        await interaction.response.edit_message(
            view=FAQShareChannelView(self.bot, self.topic_key, self.question_key, self.ui)
        )


class FAQShareChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, bot: discord.Client, topic_key: str, question_key: str | None, ui: UI):
        self.bot, self.topic_key, self.question_key, self.ui = bot, topic_key, question_key, ui
        super().__init__(
            custom_id=FAQ_SHARE_CHANNEL_CUSTOM_ID,
            channel_types=_SHARE_TYPES,
            placeholder=ui.share_channel_placeholder,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        selected = self.values[0] if self.values else None
        if selected is None or not _selected_ok(selected, interaction):
            await interaction.response.send_message(embed=_feedback(self.ui.share_permission_text), ephemeral=True)
            return
        target = await _resolve(selected)
        if target is None or not _bot_ok(target, interaction):
            await interaction.response.send_message(embed=_feedback(self.ui.share_permission_text), ephemeral=True)
            return
        topics, ui, errors = await load_state()
        active_ui = ui or self.ui
        topic = _find(topics, self.topic_key) if not errors else None
        if topic is None:
            await interaction.response.send_message(embed=_error(active_ui), ephemeral=True)
            return
        if self.question_key:
            question = next((row for row in topic.questions if row.key == self.question_key), None)
            rows = (question,) if question else ()
        else:
            rows = topic.questions
        batches = _shared_batches(rows, active_ui)
        if not batches:
            await interaction.response.send_message(embed=_error(active_ui), ephemeral=True)
            return
        if not await _send_batches(target, batches):
            await interaction.response.send_message(embed=_feedback(active_ui.share_failure_text), ephemeral=True)
            return
        await interaction.response.edit_message(
            view=FAQShareView(self.bot, self.topic_key, self.question_key, active_ui)
        )
        mention = getattr(selected, "mention", None) or getattr(target, "mention", "#channel")
        await interaction.followup.send(
            embed=_feedback(active_ui.share_success_text.format(channel=mention), success=True),
            ephemeral=True,
        )


class FAQShareChannelView(discord.ui.View):
    def __init__(self, bot: discord.Client, topic_key: str, question_key: str | None, ui: UI):
        super().__init__(timeout=900)
        self.add_item(FAQShareChannelSelect(bot, topic_key, question_key, ui))


def register_persistent_view(bot: discord.Client) -> None:
    marker = "_server_rules_faq_view_registered"
    if getattr(bot, marker, False):
        return
    bot.add_view(FAQGroupView(bot, []))
    setattr(bot, marker, True)
