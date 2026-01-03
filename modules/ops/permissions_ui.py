"""Interactive permissions UI for managing role overwrites."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Iterable, Optional

import discord
from discord.ext import commands

from c1c_coreops.helpers import help_metadata, tier
from c1c_coreops.rbac import admin_only
from modules.common import runtime as runtime_helpers
from shared import config as shared_config
from shared.redaction import sanitize_embed

__all__ = ["PermissionsUICog", "setup"]

log = logging.getLogger(__name__)

PERMISSION_PAGE_SIZE = 12
SELECT_PAGE_SIZE = 25

_PERMISSION_KEYS = tuple(
    sorted(name for name, _ in discord.Permissions.all_channel())
)


@dataclass(slots=True)
class PermissionsState:
    actor_id: int
    guild_id: int
    role_id: Optional[int] = None
    category_ids: set[int] = field(default_factory=set)
    channel_ids: set[int] = field(default_factory=set)
    permission_states: dict[str, Optional[bool]] = field(
        default_factory=lambda: {key: None for key in _PERMISSION_KEYS}
    )
    previewed: bool = False

    def selected_changes(self) -> dict[str, bool]:
        return {
            key: value
            for key, value in self.permission_states.items()
            if value is not None
        }


@dataclass(slots=True)
class TargetResolution:
    selected_categories: list[discord.CategoryChannel]
    selected_channels: list[discord.abc.GuildChannel]
    expanded_channels: list[discord.abc.GuildChannel]
    blacklisted: list[discord.abc.GuildChannel]
    no_access: list[discord.abc.GuildChannel]
    cannot_edit: list[discord.abc.GuildChannel]
    unsupported: list[discord.abc.GuildChannel]
    eligible: list[discord.abc.GuildChannel]


def _format_permission_name(name: str) -> str:
    return name.replace("_", " ").title()


def _format_permission_state(state: Optional[bool]) -> tuple[str, discord.ButtonStyle]:
    if state is True:
        return ("🟩", discord.ButtonStyle.success)
    if state is False:
        return ("🟥", discord.ButtonStyle.danger)
    return ("⬜", discord.ButtonStyle.secondary)


def _chunk_lines(lines: Iterable[str], limit: int = 900) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_len = 0
    for line in lines:
        text = line.rstrip() or "—"
        additional = len(text) + (1 if current else 0)
        if current and current_len + additional > limit:
            chunks.append("\n".join(current))
            current = [text]
            current_len = len(text)
        else:
            current.append(text)
            current_len += additional
    if current:
        chunks.append("\n".join(current))
    return chunks or ["—"]


def _format_channel_label(channel: discord.abc.GuildChannel) -> str:
    if isinstance(channel, discord.CategoryChannel):
        return f"🗂️ {channel.name}"
    if getattr(channel, "type", None) == discord.ChannelType.voice:
        return f"🔊 {channel.name}"
    if getattr(channel, "type", None) == discord.ChannelType.stage_voice:
        return f"🎙️ {channel.name}"
    if getattr(channel, "type", None) == discord.ChannelType.forum:
        return f"🗒️ {channel.name}"
    if getattr(channel, "type", None) == discord.ChannelType.news:
        return f"📰 {channel.name}"
    if getattr(channel, "type", None) == discord.ChannelType.text:
        return f"# {channel.name}"
    return f"• {channel.name}"


_SUPPORTED_CHANNEL_TYPES = {
    discord.ChannelType.text,
    discord.ChannelType.voice,
    discord.ChannelType.stage_voice,
    discord.ChannelType.forum,
    discord.ChannelType.news,
    discord.ChannelType.category,
}


def _is_supported_channel(channel: discord.abc.GuildChannel) -> bool:
    channel_type = getattr(channel, "type", None)
    return channel_type in _SUPPORTED_CHANNEL_TYPES


def _group_channels_by_category(
    channels: Iterable[discord.abc.GuildChannel],
) -> list[tuple[str, list[discord.abc.GuildChannel]]]:
    grouped: dict[Optional[int], dict[str, object]] = {}
    for channel in channels:
        if isinstance(channel, discord.CategoryChannel):
            key = channel.id
            name = channel.name
            position = channel.position
        else:
            category = getattr(channel, "category", None)
            if category is None:
                key = None
                name = "No Category"
                position = 1_000_000
            else:
                key = category.id
                name = category.name
                position = category.position
        entry = grouped.setdefault(key, {"name": name, "position": position, "channels": []})
        entry["channels"].append(channel)

    def _channel_sort_key(item: discord.abc.GuildChannel) -> tuple[int, str]:
        return (getattr(item, "position", 0), getattr(item, "name", "").lower())

    sorted_groups = sorted(
        grouped.values(),
        key=lambda item: (item["position"], str(item["name"]).lower()),
    )
    results: list[tuple[str, list[discord.abc.GuildChannel]]] = []
    for group in sorted_groups:
        channels_list = list(group["channels"])
        channels_list.sort(key=_channel_sort_key)
        results.append((str(group["name"]), channels_list))
    return results


def _grouped_channel_lines(
    channels: Iterable[discord.abc.GuildChannel],
    *,
    reason_map: Optional[dict[int, str]] = None,
    limit: Optional[int] = None,
) -> tuple[list[str], int]:
    grouped = _group_channels_by_category(channels)
    total_channels = sum(len(group_channels) for _, group_channels in grouped)
    lines: list[str] = []
    included = 0

    for category_name, group_channels in grouped:
        if limit is not None and included >= limit:
            break
        group_lines: list[str] = []
        for channel in group_channels:
            if limit is not None and included >= limit:
                break
            label = _format_channel_label(channel)
            reason = reason_map.get(channel.id) if reason_map else None
            entry = f"• {label}"
            if reason:
                entry = f"{entry} — {reason}"
            group_lines.append(entry)
            included += 1
        if group_lines:
            lines.append(category_name)
            lines.extend(group_lines)

    remaining = max(0, total_channels - included)
    if remaining > 0:
        lines.append(f"…and {remaining} more")
    return lines, remaining


def _build_list_embeds(
    *,
    title: str,
    description: Optional[str],
    sections: list[tuple[str, list[str]]],
    colour: discord.Colour,
) -> list[discord.Embed]:
    embeds: list[discord.Embed] = []
    current = discord.Embed(title=title, description=description, colour=colour)
    for section_name, lines in sections:
        for chunk in _chunk_lines(lines, limit=1000):
            if len(current.fields) >= 25:
                embeds.append(current)
                current = discord.Embed(title=f"{title} (cont.)", colour=colour)
            current.add_field(name=section_name, value=chunk, inline=False)
    if current.fields or current.description:
        embeds.append(current)
    return embeds


def _page_slice(items: list, page: int, page_size: int) -> list:
    start = page * page_size
    end = start + page_size
    return items[start:end]


def _resolve_targets(
    guild: discord.Guild,
    state: PermissionsState,
    *,
    blacklist_channel_ids: set[int],
    blacklist_category_ids: set[int],
    bot_member: Optional[discord.Member],
) -> TargetResolution:
    selected_categories: list[discord.CategoryChannel] = []
    selected_channels: list[discord.abc.GuildChannel] = []
    expanded: dict[int, discord.abc.GuildChannel] = {}

    for category_id in sorted(state.category_ids):
        channel = guild.get_channel(category_id)
        if isinstance(channel, discord.CategoryChannel):
            selected_categories.append(channel)
            expanded[channel.id] = channel
            for child in channel.channels:
                expanded[child.id] = child

    for channel_id in sorted(state.channel_ids):
        channel = guild.get_channel(channel_id)
        if isinstance(channel, discord.abc.GuildChannel):
            selected_channels.append(channel)
            expanded[channel.id] = channel

    expanded_channels = list(expanded.values())
    blacklisted: list[discord.abc.GuildChannel] = []
    no_access: list[discord.abc.GuildChannel] = []
    cannot_edit: list[discord.abc.GuildChannel] = []
    unsupported: list[discord.abc.GuildChannel] = []
    eligible: list[discord.abc.GuildChannel] = []

    for channel in expanded_channels:
        channel_id = getattr(channel, "id", None)
        category_id = getattr(channel, "category_id", None)
        if channel_id in blacklist_channel_ids:
            blacklisted.append(channel)
            continue
        if isinstance(channel, discord.CategoryChannel) and channel_id in blacklist_category_ids:
            blacklisted.append(channel)
            continue
        if category_id in blacklist_category_ids:
            blacklisted.append(channel)
            continue

        if not _is_supported_channel(channel):
            unsupported.append(channel)
            continue

        if bot_member is None:
            no_access.append(channel)
            continue
        perms = channel.permissions_for(bot_member)
        if not perms.view_channel:
            no_access.append(channel)
            continue
        if not perms.manage_channels:
            cannot_edit.append(channel)
            continue

        eligible.append(channel)

    return TargetResolution(
        selected_categories=selected_categories,
        selected_channels=selected_channels,
        expanded_channels=expanded_channels,
        blacklisted=blacklisted,
        no_access=no_access,
        cannot_edit=cannot_edit,
        unsupported=unsupported,
        eligible=eligible,
    )


def _permission_changes_text(changes: dict[str, bool]) -> tuple[list[str], list[str]]:
    allowed: list[str] = []
    denied: list[str] = []
    for key, value in sorted(changes.items()):
        label = _format_permission_name(key)
        if value:
            allowed.append(f"✅ {label}")
        else:
            denied.append(f"❌ {label}")
    return allowed, denied


class _PermissionsBaseView(discord.ui.View):
    def __init__(self, state: PermissionsState, bot: commands.Bot, *, timeout: int = 900):
        super().__init__(timeout=timeout)
        self.state = state
        self.bot = bot

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user and interaction.user.id == self.state.actor_id:
            return True
        await interaction.response.send_message(
            "This permissions session belongs to another admin.",
            ephemeral=True,
        )
        return False


class PermissionsBuilderView(_PermissionsBaseView):
    def __init__(self, state: PermissionsState, bot: commands.Bot):
        super().__init__(state, bot)
        self.apply_button.disabled = not state.previewed

    async def _render_builder(
        self, guild: discord.Guild, bot_member: Optional[discord.Member]
    ) -> discord.Embed:
        role = guild.get_role(self.state.role_id) if self.state.role_id else None
        blacklist_channels = shared_config.get_perms_blacklist_channel_ids()
        blacklist_categories = shared_config.get_perms_blacklist_category_ids()
        resolution = _resolve_targets(
            guild,
            self.state,
            blacklist_channel_ids=blacklist_channels,
            blacklist_category_ids=blacklist_categories,
            bot_member=bot_member,
        )
        changes = self.state.selected_changes()

        summary_lines = [
            f"**Role:** {role.mention if role else '—'}",
            f"**Selected categories:** {len(self.state.category_ids)}",
            f"**Selected channels:** {len(self.state.channel_ids)}",
            f"**Expanded channel count:** {len(resolution.expanded_channels)}",
            f"**Excluded by blacklist:** {len(resolution.blacklisted)}",
            f"**Excluded (bot lacks access):** {len(resolution.no_access)}",
            f"**Excluded (cannot edit overwrites):** {len(resolution.cannot_edit)}",
            f"**Excluded (unsupported type):** {len(resolution.unsupported)}",
            f"**Permission changes:** {len(changes)}",
        ]

        embed = discord.Embed(
            title="Permissions Builder",
            description="\n".join(summary_lines),
            colour=discord.Colour.blurple(),
        )
        allowed, denied = _permission_changes_text(changes)
        if allowed:
            for chunk in _chunk_lines(allowed):
                embed.add_field(name="Allow", value=chunk, inline=False)
        if denied:
            for chunk in _chunk_lines(denied):
                embed.add_field(name="Deny", value=chunk, inline=False)
        if not allowed and not denied:
            embed.add_field(name="Permission changes", value="—", inline=False)
        return embed

    @discord.ui.button(label="Pick Role", style=discord.ButtonStyle.primary)
    async def pick_role(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        view = RolePickerView(self.state, self.bot)
        embed = view.render_embed(guild)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Pick Targets", style=discord.ButtonStyle.primary)
    async def pick_targets(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        view = TargetPickerView(self.state, self.bot)
        embed = view.render_embed(guild)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Pick Permissions", style=discord.ButtonStyle.primary)
    async def pick_permissions(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        view = PermissionsPickerView(self.state, self.bot)
        embed = view.render_embed()
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Preview", style=discord.ButtonStyle.secondary)
    async def preview(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        error = _validate_builder_state(guild, self.state)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        self.state.previewed = True
        view = PreviewView(self.state, self.bot)
        embed = view.render_embed(guild)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Apply", style=discord.ButtonStyle.success)
    async def apply_button(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        error = _validate_builder_state(guild, self.state)
        if error:
            await interaction.response.send_message(error, ephemeral=True)
            return
        view = PreviewView(self.state, self.bot)
        embed = view.render_embed(guild)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="Permissions session cancelled.",
            embed=None,
            view=self,
        )


class RolePickerSelect(discord.ui.Select):
    def __init__(self, view: "RolePickerView"):
        self.view_ref = view
        roles = view.roles_page
        options = []
        for role in roles:
            label = role.name
            if len(label) > 100:
                label = f"{label[:97]}…"
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(role.id),
                    default=role.id == view.state.role_id,
                )
            )
        super().__init__(
            placeholder="Select a role",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        value = self.values[0]
        try:
            self.view_ref.state.role_id = int(value)
        except ValueError:
            await interaction.response.send_message("Invalid role selection.", ephemeral=True)
            return
        view = PermissionsBuilderView(self.view_ref.state, self.view_ref.bot)
        bot_member = interaction.guild.me
        embed = await view._render_builder(interaction.guild, bot_member)
        await interaction.response.edit_message(embed=embed, view=view)


class RolePickerView(_PermissionsBaseView):
    def __init__(self, state: PermissionsState, bot: commands.Bot):
        super().__init__(state, bot)
        self.page = 0
        self.roles: list[discord.Role] = []

    def render_embed(self, guild: discord.Guild) -> discord.Embed:
        self.roles = sorted(guild.roles, key=lambda role: role.position, reverse=True)
        embed = discord.Embed(
            title="Pick Role",
            description="Select the role to receive the permissions overwrite.",
            colour=discord.Colour.blurple(),
        )
        self._sync_components()
        return embed

    @property
    def roles_page(self) -> list[discord.Role]:
        return _page_slice(self.roles, self.page, SELECT_PAGE_SIZE)

    def _sync_components(self) -> None:
        self.clear_items()
        total_pages = max(1, (len(self.roles) + SELECT_PAGE_SIZE - 1) // SELECT_PAGE_SIZE)
        self.add_item(RolePickerSelect(self))
        prev_button = discord.ui.Button(
            label="◀ Prev",
            style=discord.ButtonStyle.secondary,
            disabled=self.page <= 0,
        )
        next_button = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= total_pages - 1,
        )
        back_button = discord.ui.Button(
            label="Back",
            style=discord.ButtonStyle.secondary,
        )

        async def _prev(interaction: discord.Interaction) -> None:
            self.page = max(0, self.page - 1)
            embed = self.render_embed(interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)

        async def _next(interaction: discord.Interaction) -> None:
            self.page = min(total_pages - 1, self.page + 1)
            embed = self.render_embed(interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)

        async def _back(interaction: discord.Interaction) -> None:
            if not interaction.guild:
                await interaction.response.send_message("Guild only.", ephemeral=True)
                return
            view = PermissionsBuilderView(self.state, self.bot)
            bot_member = interaction.guild.me
            embed = await view._render_builder(interaction.guild, bot_member)
            await interaction.response.edit_message(embed=embed, view=view)

        prev_button.callback = _prev
        next_button.callback = _next
        back_button.callback = _back
        self.add_item(prev_button)
        self.add_item(next_button)
        self.add_item(back_button)


class TargetPickerSelect(discord.ui.Select):
    def __init__(self, view: "TargetPickerView"):
        self.view_ref = view
        options = []
        for channel in view.page_items:
            label = _format_channel_label(channel)
            if len(label) > 100:
                label = f"{label[:97]}…"
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(channel.id),
                    default=channel.id in view.selected_ids,
                )
            )
        super().__init__(
            placeholder=view.placeholder,
            min_values=0,
            max_values=min(len(options), SELECT_PAGE_SIZE),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        selected_ids = {int(value) for value in self.values}
        page_ids = {channel.id for channel in self.view_ref.page_items}
        if self.view_ref.mode == "categories":
            self.view_ref.state.category_ids = (
                self.view_ref.state.category_ids - page_ids
            ) | selected_ids
        else:
            self.view_ref.state.channel_ids = (
                self.view_ref.state.channel_ids - page_ids
            ) | selected_ids
        embed = self.view_ref.render_embed(interaction.guild)
        await interaction.response.edit_message(embed=embed, view=self.view_ref)


class TargetPickerView(_PermissionsBaseView):
    def __init__(self, state: PermissionsState, bot: commands.Bot):
        super().__init__(state, bot)
        self.mode = "categories"
        self.page = 0
        self.categories: list[discord.CategoryChannel] = []
        self.channels: list[discord.abc.GuildChannel] = []

    def render_embed(self, guild: discord.Guild) -> discord.Embed:
        self.categories = sorted(
            [channel for channel in guild.categories],
            key=lambda c: c.position,
        )
        self.channels = sorted(
            [
                channel
                for channel in guild.channels
                if not isinstance(channel, discord.CategoryChannel)
            ],
            key=lambda c: (getattr(c, "category_id", 0) or 0, getattr(c, "position", 0)),
        )
        embed = discord.Embed(
            title="Pick Targets",
            description="Select categories and channels to update. Switch between lists as needed.",
            colour=discord.Colour.blurple(),
        )
        blacklist_channels = shared_config.get_perms_blacklist_channel_ids()
        blacklist_categories = shared_config.get_perms_blacklist_category_ids()
        resolution = _resolve_targets(
            guild,
            self.state,
            blacklist_channel_ids=blacklist_channels,
            blacklist_category_ids=blacklist_categories,
            bot_member=guild.me,
        )
        summary_lines = [
            f"**Selected categories:** {len(self.state.category_ids)}",
            f"**Selected channels:** {len(self.state.channel_ids)}",
            f"**Expanded channel count:** {len(resolution.expanded_channels)}",
            f"**Excluded by blacklist:** {len(resolution.blacklisted)}",
            f"**Excluded (bot lacks access):** {len(resolution.no_access)}",
            f"**Excluded (cannot edit overwrites):** {len(resolution.cannot_edit)}",
            f"**Excluded (unsupported type):** {len(resolution.unsupported)}",
        ]
        embed.add_field(name="Summary", value="\n".join(summary_lines), inline=False)
        self._sync_components()
        return embed

    @property
    def placeholder(self) -> str:
        return "Select categories" if self.mode == "categories" else "Select channels"

    @property
    def selected_ids(self) -> set[int]:
        return self.state.category_ids if self.mode == "categories" else self.state.channel_ids

    @property
    def page_items(self) -> list[discord.abc.GuildChannel]:
        source = self.categories if self.mode == "categories" else self.channels
        return _page_slice(source, self.page, SELECT_PAGE_SIZE)

    def _sync_components(self) -> None:
        self.clear_items()
        source = self.categories if self.mode == "categories" else self.channels
        total_pages = max(1, (len(source) + SELECT_PAGE_SIZE - 1) // SELECT_PAGE_SIZE)
        if source:
            self.add_item(TargetPickerSelect(self))
        else:
            placeholder = (
                "No categories available" if self.mode == "categories" else "No channels available"
            )
            empty_button = discord.ui.Button(
                label=placeholder,
                style=discord.ButtonStyle.secondary,
                disabled=True,
            )
            self.add_item(empty_button)
        switch_label = "Switch to Channels" if self.mode == "categories" else "Switch to Categories"
        switch_button = discord.ui.Button(label=switch_label, style=discord.ButtonStyle.secondary)
        prev_button = discord.ui.Button(
            label="◀ Prev",
            style=discord.ButtonStyle.secondary,
            disabled=self.page <= 0,
        )
        next_button = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= total_pages - 1,
        )
        done_button = discord.ui.Button(label="Done", style=discord.ButtonStyle.success)
        back_button = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)

        async def _switch(interaction: discord.Interaction) -> None:
            self.mode = "channels" if self.mode == "categories" else "categories"
            self.page = 0
            embed = self.render_embed(interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)

        async def _prev(interaction: discord.Interaction) -> None:
            self.page = max(0, self.page - 1)
            embed = self.render_embed(interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)

        async def _next(interaction: discord.Interaction) -> None:
            self.page = min(total_pages - 1, self.page + 1)
            embed = self.render_embed(interaction.guild)
            await interaction.response.edit_message(embed=embed, view=self)

        async def _done(interaction: discord.Interaction) -> None:
            view = PermissionsBuilderView(self.state, self.bot)
            bot_member = interaction.guild.me
            embed = await view._render_builder(interaction.guild, bot_member)
            await interaction.response.edit_message(embed=embed, view=view)

        async def _back(interaction: discord.Interaction) -> None:
            view = PermissionsBuilderView(self.state, self.bot)
            bot_member = interaction.guild.me
            embed = await view._render_builder(interaction.guild, bot_member)
            await interaction.response.edit_message(embed=embed, view=view)

        switch_button.callback = _switch
        prev_button.callback = _prev
        next_button.callback = _next
        done_button.callback = _done
        back_button.callback = _back
        self.add_item(switch_button)
        self.add_item(prev_button)
        self.add_item(next_button)
        self.add_item(done_button)
        self.add_item(back_button)


class PermissionButton(discord.ui.Button):
    def __init__(self, view: "PermissionsPickerView", key: str):
        self.view_ref = view
        self.key = key
        state = view.state.permission_states.get(key)
        prefix, style = _format_permission_state(state)
        label = f"{prefix} {_format_permission_name(key)}"
        if len(label) > 80:
            label = f"{label[:77]}…"
        super().__init__(label=label, style=style)

    async def callback(self, interaction: discord.Interaction) -> None:
        current = self.view_ref.state.permission_states.get(self.key)
        next_state: Optional[bool]
        if current is None:
            next_state = True
        elif current is True:
            next_state = False
        else:
            next_state = None
        self.view_ref.state.permission_states[self.key] = next_state
        self.view_ref._sync_components()
        embed = self.view_ref.render_embed()
        await interaction.response.edit_message(embed=embed, view=self.view_ref)


class PermissionsPickerView(_PermissionsBaseView):
    def __init__(self, state: PermissionsState, bot: commands.Bot):
        super().__init__(state, bot)
        self.page = 0
        self._sync_components()

    @property
    def page_keys(self) -> list[str]:
        return _page_slice(list(_PERMISSION_KEYS), self.page, PERMISSION_PAGE_SIZE)

    def render_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Pick Permissions",
            description=(
                "Cycle each permission: Unchanged → Allow → Deny → Unchanged.\n"
                "Use Done to return to the builder when finished."
            ),
            colour=discord.Colour.blurple(),
        )
        return embed

    def _sync_components(self) -> None:
        self.clear_items()
        total_pages = max(1, (len(_PERMISSION_KEYS) + PERMISSION_PAGE_SIZE - 1) // PERMISSION_PAGE_SIZE)
        for key in self.page_keys:
            self.add_item(PermissionButton(self, key))

        prev_button = discord.ui.Button(
            label="◀ Prev",
            style=discord.ButtonStyle.secondary,
            disabled=self.page <= 0,
        )
        next_button = discord.ui.Button(
            label="Next ▶",
            style=discord.ButtonStyle.secondary,
            disabled=self.page >= total_pages - 1,
        )
        done_button = discord.ui.Button(label="Done", style=discord.ButtonStyle.success)
        clear_button = discord.ui.Button(label="Clear", style=discord.ButtonStyle.secondary)
        back_button = discord.ui.Button(label="Back", style=discord.ButtonStyle.secondary)

        async def _prev(interaction: discord.Interaction) -> None:
            self.page = max(0, self.page - 1)
            self._sync_components()
            embed = self.render_embed()
            await interaction.response.edit_message(embed=embed, view=self)

        async def _next(interaction: discord.Interaction) -> None:
            self.page = min(total_pages - 1, self.page + 1)
            self._sync_components()
            embed = self.render_embed()
            await interaction.response.edit_message(embed=embed, view=self)

        async def _done(interaction: discord.Interaction) -> None:
            view = PermissionsBuilderView(self.state, self.bot)
            bot_member = interaction.guild.me if interaction.guild else None
            if interaction.guild is None:
                await interaction.response.send_message("Guild only.", ephemeral=True)
                return
            embed = await view._render_builder(interaction.guild, bot_member)
            await interaction.response.edit_message(embed=embed, view=view)

        async def _clear(interaction: discord.Interaction) -> None:
            for key in _PERMISSION_KEYS:
                self.state.permission_states[key] = None
            self._sync_components()
            embed = self.render_embed()
            await interaction.response.edit_message(embed=embed, view=self)

        async def _back(interaction: discord.Interaction) -> None:
            view = PermissionsBuilderView(self.state, self.bot)
            bot_member = interaction.guild.me if interaction.guild else None
            if interaction.guild is None:
                await interaction.response.send_message("Guild only.", ephemeral=True)
                return
            embed = await view._render_builder(interaction.guild, bot_member)
            await interaction.response.edit_message(embed=embed, view=view)

        prev_button.callback = _prev
        next_button.callback = _next
        done_button.callback = _done
        clear_button.callback = _clear
        back_button.callback = _back
        self.add_item(prev_button)
        self.add_item(next_button)
        self.add_item(done_button)
        self.add_item(clear_button)
        self.add_item(back_button)


class PreviewView(_PermissionsBaseView):
    def __init__(self, state: PermissionsState, bot: commands.Bot):
        super().__init__(state, bot)
        self.full_list_embeds: list[discord.Embed] = []
        self.has_more_targets = False

    def render_embed(self, guild: discord.Guild) -> discord.Embed:
        role = guild.get_role(self.state.role_id) if self.state.role_id else None
        blacklist_channels = shared_config.get_perms_blacklist_channel_ids()
        blacklist_categories = shared_config.get_perms_blacklist_category_ids()
        resolution = _resolve_targets(
            guild,
            self.state,
            blacklist_channel_ids=blacklist_channels,
            blacklist_category_ids=blacklist_categories,
            bot_member=guild.me,
        )
        changes = self.state.selected_changes()
        allowed, denied = _permission_changes_text(changes)

        embed = discord.Embed(
            title="Permissions Preview",
            description=(
                f"**Role:** {role.mention if role else '—'}\n"
                f"**Expanded channel count:** {len(resolution.expanded_channels)}\n"
                f"**Excluded by blacklist:** {len(resolution.blacklisted)}\n"
                f"**Excluded (bot lacks access):** {len(resolution.no_access)}\n"
                f"**Excluded (cannot edit overwrites):** {len(resolution.cannot_edit)}\n"
                f"**Excluded (unsupported type):** {len(resolution.unsupported)}\n"
                f"**Eligible channels:** {len(resolution.eligible)}"
            ),
            colour=discord.Colour.orange(),
        )
        target_lines, remaining_targets = _grouped_channel_lines(
            resolution.eligible,
            limit=25,
        )
        targets_title = "Target Channels"
        if remaining_targets:
            targets_title = "Target Channels (first 25)"
        for chunk in _chunk_lines(target_lines, limit=1000):
            if len(embed.fields) >= 25:
                break
            embed.add_field(name=targets_title, value=chunk, inline=False)
            targets_title = "Target Channels (cont.)"

        skipped_reasons = {
            channel.id: "blacklisted" for channel in resolution.blacklisted
        }
        skipped_reasons.update(
            {channel.id: "bot lacks access" for channel in resolution.no_access}
        )
        skipped_reasons.update(
            {channel.id: "cannot edit overwrites" for channel in resolution.cannot_edit}
        )
        skipped_reasons.update(
            {channel.id: "unsupported type" for channel in resolution.unsupported}
        )
        skipped_channels = (
            resolution.blacklisted
            + resolution.no_access
            + resolution.cannot_edit
            + resolution.unsupported
        )
        skipped_lines, _ = _grouped_channel_lines(
            skipped_channels,
            reason_map=skipped_reasons,
        )
        for chunk in _chunk_lines(skipped_lines, limit=1000):
            if len(embed.fields) >= 25:
                break
            embed.add_field(name="Skipped Channels", value=chunk, inline=False)
        if allowed:
            for chunk in _chunk_lines(allowed):
                if len(embed.fields) >= 25:
                    break
                embed.add_field(name="Allow", value=chunk, inline=False)
        if denied:
            for chunk in _chunk_lines(denied):
                if len(embed.fields) >= 25:
                    break
                embed.add_field(name="Deny", value=chunk, inline=False)
        if not allowed and not denied:
            if len(embed.fields) < 25:
                embed.add_field(name="Permission changes", value="—", inline=False)
        embed.set_footer(text="Default mode is dry-run until confirmed.")

        full_target_lines, _ = _grouped_channel_lines(resolution.eligible)
        full_skipped_lines, _ = _grouped_channel_lines(
            skipped_channels,
            reason_map=skipped_reasons,
        )
        self.has_more_targets = remaining_targets > 0
        if self.has_more_targets:
            self.full_list_embeds = _build_list_embeds(
                title="Permissions Preview - Full Target List",
                description=None,
                sections=[
                    ("Target Channels", full_target_lines),
                    ("Skipped Channels", full_skipped_lines),
                ],
                colour=discord.Colour.orange(),
            )
        else:
            self.full_list_embeds = []
        self.show_full_list.disabled = not self.has_more_targets
        return embed

    @discord.ui.button(label="Show full target list", style=discord.ButtonStyle.secondary)
    async def show_full_list(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if not self.full_list_embeds:
            await interaction.response.send_message("No additional channels to show.", ephemeral=True)
            return
        embeds = self.full_list_embeds
        first_batch = embeds[:10]
        await interaction.response.send_message(embeds=first_batch, ephemeral=True)
        remaining = embeds[10:]
        for start in range(0, len(remaining), 10):
            await interaction.followup.send(
                embeds=remaining[start : start + 10],
                ephemeral=True,
            )

    @discord.ui.button(label="CONFIRM APPLY", style=discord.ButtonStyle.danger)
    async def confirm_apply(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        await interaction.response.defer()
        result = await _apply_permissions(interaction.guild, self.state, self.bot)
        embed = discord.Embed(
            title="Permissions Applied",
            description=result.summary,
            colour=discord.Colour.green() if result.errors == 0 else discord.Colour.orange(),
        )
        await interaction.edit_original_response(embed=embed, view=None)

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary)
    async def back(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        if not interaction.guild:
            await interaction.response.send_message("Guild only.", ephemeral=True)
            return
        view = PermissionsBuilderView(self.state, self.bot)
        bot_member = interaction.guild.me
        embed = await view._render_builder(interaction.guild, bot_member)
        await interaction.response.edit_message(embed=embed, view=view)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.danger)
    async def cancel(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content="Permissions session cancelled.",
            embed=None,
            view=self,
        )


@dataclass(slots=True)
class ApplyResult:
    updated: int
    skipped: int
    errors: int
    duration_sec: float
    summary: str


def _validate_builder_state(guild: discord.Guild, state: PermissionsState) -> Optional[str]:
    if state.role_id is None or guild.get_role(state.role_id) is None:
        return "Pick a role before previewing."
    if not state.category_ids and not state.channel_ids:
        return "Pick at least one category or channel before previewing."
    if not state.selected_changes():
        return "Pick at least one permission change before previewing."
    return None


def _should_update_overwrite(
    overwrite: discord.PermissionOverwrite, changes: dict[str, bool]
) -> bool:
    for key, value in changes.items():
        current = getattr(overwrite, key, None)
        if current != value:
            return True
    return False


async def _apply_permissions(
    guild: discord.Guild, state: PermissionsState, bot: commands.Bot
) -> ApplyResult:
    role = guild.get_role(state.role_id) if state.role_id else None
    if role is None:
        return ApplyResult(0, 0, 0, 0.0, "Role not found; no changes applied.")

    blacklist_channels = shared_config.get_perms_blacklist_channel_ids()
    blacklist_categories = shared_config.get_perms_blacklist_category_ids()
    resolution = _resolve_targets(
        guild,
        state,
        blacklist_channel_ids=blacklist_channels,
        blacklist_category_ids=blacklist_categories,
        bot_member=guild.me,
    )
    changes = state.selected_changes()
    start = time.monotonic()
    updated = 0
    skipped = 0
    errors = 0

    for index, channel in enumerate(resolution.eligible, start=1):
        try:
            overwrite = channel.overwrites_for(role)
            if not _should_update_overwrite(overwrite, changes):
                skipped += 1
                continue
            for key, value in changes.items():
                setattr(overwrite, key, value)
            await channel.set_permissions(
                role,
                overwrite=overwrite,
                reason="Permissions UI apply",
            )
        except Exception:  # pragma: no cover - network failure
            errors += 1
            log.warning(
                "Failed to apply permissions",
                exc_info=True,
                extra={"channel": channel, "role": role.id},
            )
            continue
        updated += 1
        if index % 5 == 0:
            await asyncio.sleep(0.6)

    duration = time.monotonic() - start
    summary_lines = [
        f"Role: {role.name} ({role.id})",
        f"Updated: {updated}",
        f"Skipped: {skipped}",
        f"Errors: {errors}",
        f"Duration: {duration:.1f}s",
    ]
    summary = "\n".join(summary_lines)

    if guild:
        await runtime_helpers.send_log_message(
            "🔐 Permissions UI applied — "
            f"role={role.name} ({role.id}) • updated={updated} • "
            f"skipped={skipped} • errors={errors} • duration={duration:.1f}s"
        )

    return ApplyResult(updated, skipped, errors, duration, summary)


class PermissionsUICog(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @tier("admin")
    @help_metadata(function_group="operational", section="permissions", access_tier="admin")
    @commands.command(
        name="perm",
        help="Launches the interactive permissions UI.",
        brief="Open the permissions UI.",
    )
    @admin_only()
    async def perm(self, ctx: commands.Context) -> None:
        if not shared_config.features.ops_permissions_enabled:
            await ctx.reply("Permissions module is disabled.", mention_author=False)
            return
        guild = ctx.guild
        if guild is None:
            await ctx.reply("Guild only.", mention_author=False)
            return
        member = ctx.author
        if isinstance(member, discord.Member):
            perms = member.guild_permissions
            if not (perms.manage_channels or perms.administrator):
                await ctx.reply(
                    "You need Manage Channels or Administrator to use this command.",
                    mention_author=False,
                )
                return
        bot_member = guild.me
        if bot_member is None or not bot_member.guild_permissions.manage_channels:
            await ctx.reply(
                "I need Manage Channels to manage overwrites.",
                mention_author=False,
            )
            return
        state = PermissionsState(actor_id=ctx.author.id, guild_id=guild.id)
        view = PermissionsBuilderView(state, self.bot)
        embed = await view._render_builder(guild, bot_member)
        await ctx.reply(embed=sanitize_embed(embed), view=view, mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PermissionsUICog(bot))
