"""Restrict FAQ sharing to General Chat and clan chats from Mirralith Config."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import discord

from modules.ops import server_rules as base
import modules.ops.server_rules_interactive as interactive

GENERAL_CHAT_KEY = "SERVER_RULES_FAQ_GENERAL_CHAT_CHANNEL_ID"
CLAN_CHAT_CATEGORY_KEY = "SERVER_RULES_FAQ_CLAN_CHAT_CATEGORY_ID"
MAX_OPTIONS = 25
_INSTALLED = False


@dataclass(frozen=True)
class ShareDestinationConfig:
    general_chat_id: int
    clan_chat_category_id: int


async def _load_config() -> tuple[ShareDestinationConfig | None, list[str]]:
    general_raw = await base.recruitment_sheet.get_config_value_async(
        GENERAL_CHAT_KEY, None
    )
    category_raw = await base.recruitment_sheet.get_config_value_async(
        CLAN_CHAT_CATEGORY_KEY, None
    )
    errors: list[str] = []

    general = str(general_raw or "").strip()
    category = str(category_raw or "").strip()
    if not base.valid_snowflake(general):
        errors.append(f"Config key {GENERAL_CHAT_KEY} must be a Discord channel ID")
    if not base.valid_snowflake(category):
        errors.append(f"Config key {CLAN_CHAT_CATEGORY_KEY} must be a Discord category ID")
    if errors:
        return None, errors
    return ShareDestinationConfig(int(general), int(category)), []


def _is_text_destination(channel: Any) -> bool:
    return getattr(channel, "type", None) in {
        getattr(discord.ChannelType, "text", None),
        getattr(discord.ChannelType, "news", None),
    }


def _parent_id(channel: Any) -> int | None:
    value = getattr(channel, "category_id", None)
    if value is not None:
        return value
    return getattr(getattr(channel, "category", None), "id", None)


def _allowed_by_config(channel: Any, config: ShareDestinationConfig) -> bool:
    channel_id = getattr(channel, "id", None)
    return channel_id == config.general_chat_id or (
        _is_text_destination(channel)
        and _parent_id(channel) == config.clan_chat_category_id
    )


def _member_ok(channel: Any, interaction: discord.Interaction) -> bool:
    permissions_for = getattr(channel, "permissions_for", None)
    member = getattr(interaction, "user", None)
    if member is None or not callable(permissions_for):
        return False
    try:
        permissions = permissions_for(member)
    except Exception:
        return False
    return interactive._can_send(permissions, False)


def _candidate_channels(
    guild: Any, interaction: discord.Interaction, config: ShareDestinationConfig
) -> list[Any]:
    if guild is None:
        return []

    candidates: list[Any] = []
    general = guild.get_channel(config.general_chat_id)
    if general is not None and _is_text_destination(general):
        candidates.append(general)

    category = guild.get_channel(config.clan_chat_category_id)
    for channel in list(getattr(category, "channels", None) or []):
        if _is_text_destination(channel) and _parent_id(channel) == config.clan_chat_category_id:
            candidates.append(channel)

    unique: dict[int, Any] = {}
    for channel in candidates:
        channel_id = getattr(channel, "id", None)
        if channel_id is None:
            continue
        if not _member_ok(channel, interaction):
            continue
        if not interactive._bot_ok(channel, interaction):
            continue
        unique[channel_id] = channel

    general_channel = unique.pop(config.general_chat_id, None)
    clan_channels = sorted(
        unique.values(),
        key=lambda channel: (
            getattr(channel, "position", 0),
            str(getattr(channel, "name", "")).lower(),
        ),
    )
    return ([general_channel] if general_channel is not None else []) + clan_channels


def _label(channel: Any) -> str:
    name = str(getattr(channel, "name", "channel")).strip() or "channel"
    label = f"#{name}"
    return label if len(label) <= 100 else label[:99] + "…"


class FAQShareDestinationSelect(discord.ui.Select):
    def __init__(
        self,
        bot: discord.Client,
        topic_key: str,
        question_key: str | None,
        ui: interactive.UI,
        config: ShareDestinationConfig,
        channels: list[Any],
    ) -> None:
        self.bot = bot
        self.topic_key = topic_key
        self.question_key = question_key
        self.ui = ui
        self.config = config
        options = [
            discord.SelectOption(label=_label(channel), value=str(channel.id))
            for channel in channels
        ]
        super().__init__(
            custom_id=interactive.FAQ_SHARE_CHANNEL_CUSTOM_ID,
            placeholder=ui.share_channel_placeholder,
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        guild = getattr(interaction, "guild", None)
        try:
            channel_id = int(self.values[0]) if self.values else 0
        except (TypeError, ValueError):
            channel_id = 0
        target = guild.get_channel(channel_id) if guild is not None and channel_id else None

        config, config_errors = await _load_config()
        if config_errors or config is None or target is None:
            await interaction.response.send_message(
                embed=interactive._error(self.ui), ephemeral=True
            )
            return
        if not _allowed_by_config(target, config):
            await interaction.response.send_message(
                embed=interactive._feedback(self.ui.share_permission_text), ephemeral=True
            )
            return
        if not _member_ok(target, interaction) or not interactive._bot_ok(target, interaction):
            await interaction.response.send_message(
                embed=interactive._feedback(self.ui.share_permission_text), ephemeral=True
            )
            return

        topics, ui, errors = await interactive.load_state()
        active_ui = ui or self.ui
        topic = interactive._find(topics, self.topic_key) if not errors else None
        if topic is None:
            await interaction.response.send_message(
                embed=interactive._error(active_ui), ephemeral=True
            )
            return
        if self.question_key:
            question = next(
                (row for row in topic.questions if row.key == self.question_key), None
            )
            rows = (question,) if question else ()
        else:
            rows = topic.questions
        batches = interactive._shared_batches(rows, active_ui)
        if not batches:
            await interaction.response.send_message(
                embed=interactive._error(active_ui), ephemeral=True
            )
            return
        if not await interactive._send_batches(target, batches):
            await interaction.response.send_message(
                embed=interactive._feedback(active_ui.share_failure_text), ephemeral=True
            )
            return

        await interaction.response.edit_message(
            view=FAQShareView(self.bot, self.topic_key, self.question_key, active_ui)
        )
        mention = getattr(target, "mention", "#channel")
        await interaction.followup.send(
            embed=interactive._feedback(
                active_ui.share_success_text.format(channel=mention), success=True
            ),
            ephemeral=True,
        )


class FAQShareDestinationView(discord.ui.View):
    def __init__(
        self,
        bot: discord.Client,
        topic_key: str,
        question_key: str | None,
        ui: interactive.UI,
        config: ShareDestinationConfig,
        channels: list[Any],
    ) -> None:
        super().__init__(timeout=900)
        self.add_item(
            FAQShareDestinationSelect(
                bot, topic_key, question_key, ui, config, channels
            )
        )


class FAQShareView(discord.ui.View):
    def __init__(
        self,
        bot: discord.Client,
        topic_key: str,
        question_key: str | None,
        ui: interactive.UI,
    ) -> None:
        super().__init__(timeout=900)
        self.bot = bot
        self.topic_key = topic_key
        self.question_key = question_key
        self.ui = ui
        button = discord.ui.Button(
            label=ui.share_answer_label if question_key else ui.share_group_label,
            style=discord.ButtonStyle.primary,
        )
        button.callback = self._share
        self.add_item(button)

    async def _share(self, interaction: discord.Interaction) -> None:
        config, errors = await _load_config()
        if errors or config is None:
            await interaction.response.edit_message(
                embed=interactive._error(self.ui), view=None
            )
            return
        channels = _candidate_channels(
            getattr(interaction, "guild", None), interaction, config
        )
        if not channels or len(channels) > MAX_OPTIONS:
            await interaction.response.edit_message(
                embed=interactive._error(self.ui), view=None
            )
            return
        await interaction.response.edit_message(
            view=FAQShareDestinationView(
                self.bot,
                self.topic_key,
                self.question_key,
                self.ui,
                config,
                channels,
            )
        )


def install() -> None:
    """Replace the broad Discord ChannelSelect with the configured destination flow."""

    global _INSTALLED
    if _INSTALLED:
        return
    interactive.FAQShareView = FAQShareView
    _INSTALLED = True
