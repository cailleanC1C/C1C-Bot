"""Embed and component helpers for the shard tracker."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Mapping, Sequence

import discord

from shared import theme

from .mercy import MercySnapshot, format_percent


TAB_LABELS: Mapping[str, str] = {
    "overview": "Overview",
    "ancient": "Ancient",
    "void": "Void",
    "sacred": "Sacred",
    "primal": "Primal",
    "last_pulls": "Last Pulls",
}

FOOTER_TEXT = "For info about how this works type !help shards"
OVERFLOW_RANGE = 100


@dataclass(frozen=True)
class ShardDisplay:
    key: str
    label: str
    owned: int
    mercy: MercySnapshot
    last_timestamp: str
    last_depth: int


@dataclass(frozen=True)
class MythicDisplay:
    mercy: MercySnapshot
    last_timestamp: str
    last_depth: int


class ShardTrackerView(discord.ui.View):
    """Interactive view for the tabbed shard tracker."""

    def __init__(
        self,
        *,
        owner_id: int,
        controller: "ShardTrackerController",
        active_tab: str,
        shard_labels: Mapping[str, str],
        shard_emojis: Mapping[str, discord.PartialEmoji | None],
        mythic_controls: bool = True,
        timeout: float | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.active_tab = active_tab
        self._controller = controller
        # Tab buttons
        for tab in ("overview", "ancient", "void", "sacred", "primal", "last_pulls"):
            label: str | None = None
            emoji = None

            if tab in ("overview", "last_pulls"):
                label = TAB_LABELS[tab]
            else:
                emoji = shard_emojis.get(tab)
                if not emoji or not getattr(emoji, "id", None):
                    emoji = None
                    label = TAB_LABELS[tab]
            style = discord.ButtonStyle.primary if tab == active_tab else discord.ButtonStyle.secondary
            self.add_item(
                _ShardButton(
                    custom_id=f"tab:{tab}",
                    label=label,
                    emoji=emoji,
                    style=style,
                    owner_id=owner_id,
                    controller=controller,
                )
            )

        # Action rows depend on tab
        if active_tab in shard_labels:
            self._add_primary_buttons()
            self._add_legendary_button()
            self._add_last_pulls_button()

    def _add_primary_buttons(self) -> None:
        self.add_item(
            _ShardButton(
                custom_id=f"action:stash:{self.active_tab}",
                label="+ Stash",
                emoji=None,
                style=discord.ButtonStyle.primary,
                owner_id=self.owner_id,
                controller=self._controller,
            )
        )
        self.add_item(
            _ShardButton(
                custom_id=f"action:pulls:{self.active_tab}",
                label="- Pulls",
                emoji=None,
                style=discord.ButtonStyle.secondary,
                owner_id=self.owner_id,
                controller=self._controller,
            )
        )
        self.add_item(
            _ShardButton(
                custom_id=f"action:share:{self.active_tab}",
                label="Share to Clan",
                emoji=None,
                style=discord.ButtonStyle.secondary,
                owner_id=self.owner_id,
                controller=self._controller,
            )
        )

    def _add_legendary_button(self) -> None:
        label = "Got Legendary/Mythical" if self.active_tab == "primal" else "Got Legendary"
        self.add_item(
            _ShardButton(
                custom_id=f"action:legendary:{self.active_tab}",
                label=label,
                emoji=None,
                style=discord.ButtonStyle.success,
                owner_id=self.owner_id,
                controller=self._controller,
            )
        )

    def _add_last_pulls_button(self) -> None:
        self.add_item(
            _ShardButton(
                custom_id=f"action:last_pulls:{self.active_tab}",
                label="Last Pulls / Mercy",
                emoji=None,
                style=discord.ButtonStyle.secondary,
                owner_id=self.owner_id,
                controller=self._controller,
            )
        )


class _ShardButton(discord.ui.Button[ShardTrackerView]):
    def __init__(
        self,
        *,
        custom_id: str,
        label: str,
        emoji: discord.PartialEmoji | None,
        style: discord.ButtonStyle,
        owner_id: int,
        controller: "ShardTrackerController",
        ) -> None:
        super().__init__(custom_id=custom_id, label=label, style=style, emoji=emoji)
        self._owner_id = owner_id
        self._controller = controller

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        user_id = getattr(interaction.user, "id", None)
        owner_id = self._owner_id
        if owner_id and user_id != owner_id:
            await interaction.response.send_message(
                "Only the owner of this tracker can use these buttons.", ephemeral=True
            )
            return
        await self._controller.handle_button_interaction(
            interaction=interaction,
            custom_id=self.custom_id,
            active_tab=self.custom_id.split(":")[-1]
            if self.custom_id.startswith("action:") or self.custom_id.startswith("tab:")
            else self.view.active_tab if isinstance(self.view, ShardTrackerView) else "overview",
        )


_TAB_COLORS: Mapping[str, discord.Colour] = {
    "overview": theme.colors.c1c_blue,
    "last_pulls": theme.colors.c1c_blue,
    "ancient": discord.Colour(0x5CC8FF),
    "void": discord.Colour(0xA970FF),
    "sacred": discord.Colour.gold(),
    "primal": discord.Colour.dark_red(),
}


_AUTHOR_NAMES: Mapping[str, str] = {
    "overview": "Shard Overview — C1C",
    "last_pulls": "Last Pulls & Mercy Info — C1C",
    "ancient": "Ancient Shards",
    "void": "Void Shards",
    "sacred": "Sacred Shards",
    "primal": "Primal Shards",
}


class ShardTrackerController:
    async def handle_button_interaction(
        self,
        *,
        interaction: discord.Interaction,
        custom_id: str,
        active_tab: str,
    ) -> None:
        raise NotImplementedError


class ShardReminderOptView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=None)
        self.add_item(
            _ShardReminderButton(
                custom_id="shard:opt_in",
                label="Opt In",
                style=discord.ButtonStyle.success,
            )
        )
        self.add_item(
            _ShardReminderButton(
                custom_id="shard:opt_out",
                label="Opt Out",
                style=discord.ButtonStyle.secondary,
            )
        )


class _ShardReminderButton(discord.ui.Button[ShardReminderOptView]):
    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        controller = getattr(interaction.client, "get_cog", lambda _name: None)("ShardTracker")
        if controller is None:
            await interaction.response.send_message("Shard tracker is unavailable.", ephemeral=True)
            return
        parts = str(self.custom_id or "").split(":")
        if len(parts) != 2:
            await interaction.response.send_message("Invalid shard reminder action.", ephemeral=True)
            return
        action = "in" if parts[1] == "opt_in" else "out"
        await controller.handle_reminder_opt_action(interaction=interaction, action=action, clan_key=None)


def register_persistent_shard_views(bot: discord.Client) -> None:
    bot.add_view(ShardReminderOptView())


def build_overview_embed(
    *,
    member: discord.abc.User,
    displays: Sequence[ShardDisplay],
    mythic: MythicDisplay,
    author_name: str | None = None,
    author_icon_url: str | None = None,
    color: discord.Colour | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        colour=color or _TAB_COLORS.get("overview"),
        description=(
            "Snapshot across all shard types. Use the tab buttons for details "
            "and the Legendary/Mythical buttons to log pulls."
        ),
    )
    embed.set_author(
        name=author_name or _AUTHOR_NAMES.get("overview"),
        icon_url=author_icon_url,
    )
    primal = next((display for display in displays if display.key == "primal"), None)
    other_displays = [display for display in displays if display.key != "primal"]

    for display in other_displays:
        line = (
            f"Owned: **{max(display.owned, 0):,}** | "
            f"Mercy: {display.mercy.pulls_since} / {display.mercy.threshold} | "
            f"Chance: {format_percent(display.mercy.chance)}"
        )
        if display.last_timestamp:
            line += f"\nLast Legendary: {human_time(display.last_timestamp)}"
        embed.add_field(name=display.label, value=line, inline=False)

    if primal:
        mythic_mercy = mythic.mercy
        primal_lines = [
            f"Owned: **{max(primal.owned, 0):,}**",
            "Legendary",
            (
                f"Mercy: {primal.mercy.pulls_since} / {primal.mercy.threshold} | "
                f"Chance: {format_percent(primal.mercy.chance)}"
            ),
        ]
        if primal.last_timestamp:
            primal_lines.append(f"Last Legendary: {human_time(primal.last_timestamp)}")
        primal_lines.extend(
            [
                "Mythical",
                (
                    f"Mercy: {mythic_mercy.pulls_since} / {mythic_mercy.threshold} | "
                    f"Chance: {format_percent(mythic_mercy.chance)}"
                ),
            ]
        )
        if mythic.last_timestamp:
            primal_lines.append(f"Last Mythical: {human_time(mythic.last_timestamp)}")
        primal_lines.append("Details:")
        embed.add_field(name="Primal", value="\n".join(primal_lines), inline=False)

    _apply_footer(embed)
    return embed


def build_detail_embed(
    *,
    member: discord.abc.User,
    display: ShardDisplay,
    mythic: MythicDisplay | None = None,
    author_name: str | None = None,
    author_icon_url: str | None = None,
    color: discord.Colour | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        colour=color or _TAB_COLORS.get(display.key, _TAB_COLORS["overview"])
    )
    embed.set_author(
        name=author_name or _AUTHOR_NAMES.get(display.key, display.label),
        icon_url=author_icon_url,
    )
    embed.description = _detail_block(display)
    embed.add_field(
        name="Progress",
        value=_progress_bar(display.mercy),
        inline=False,
    )
    if mythic:
        embed.add_field(name="Primal Mythical", value=_mythic_block(mythic), inline=False)
    _apply_footer(embed)
    return embed


def build_last_pulls_embed(
    *,
    member: discord.abc.User,
    displays: Sequence[ShardDisplay],
    mythic: MythicDisplay,
    base_rates: Mapping[str, str],
    author_name: str | None = None,
    author_icon_url: str | None = None,
    color: discord.Colour | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        colour=color or _TAB_COLORS.get("last_pulls", _TAB_COLORS["overview"])
    )
    embed.set_author(
        name=author_name or _AUTHOR_NAMES.get("last_pulls"),
        icon_url=author_icon_url,
    )
    last_lines = []
    for display in displays:
        stamp = human_time(display.last_timestamp) if display.last_timestamp else "Never"
        depth = f" ({display.last_depth} at pull)" if display.last_depth > 0 else ""
        last_lines.append(f"{display.label} Legendary: {stamp}{depth}")
    mythic_stamp = human_time(mythic.last_timestamp) if mythic.last_timestamp else "Never"
    mythic_depth = f" ({mythic.last_depth} at pull)" if mythic.last_depth > 0 else ""
    last_lines.append(f"Primal Mythical: {mythic_stamp}{mythic_depth}")
    last_lines.append("")
    embed.add_field(name="Last Pulls", value="\n".join(last_lines), inline=False)

    info_lines = [
        "Ancient/Void Legendary: after 200 pulls, +5% per shard",
        "Sacred Legendary: after 12 pulls, +2% per shard",
        "Primal Legendary: after 75 pulls, +1% per shard",
        "Primal Mythical: after 200 pulls, +10% per shard",
        "",
        "**Base chances:**",
    ]
    for label, rate in base_rates.items():
        info_lines.append(f"{label:<18} {rate}")
    embed.add_field(name="Mercy Info", value="\n".join(info_lines), inline=False)
    _apply_footer(embed)
    return embed


def _detail_block(display: ShardDisplay) -> str:
    mercy = display.mercy
    maxed = mercy.pulls_since >= mercy.threshold
    parts = [
        f"Stash: **{max(display.owned, 0):,}**",
    ]
    if display.key == "primal":
        parts.append("")
        parts.append("**Primal Legendary**")
    parts.extend(
        [
            f"Legendary Mercy: {mercy.pulls_since} / {mercy.threshold}" + (" (Maxed)" if maxed else ""),
            f"Legendary Chance: {format_percent(mercy.chance)}",
        ]
    )
    if display.last_timestamp:
        last_line = f"Last Legendary: {human_time(display.last_timestamp)}"
        if display.key != "primal" and display.last_depth:
            last_line += f" ({display.last_depth} depth)"
        parts.append(last_line)
    return "\n".join(parts)


def _mythic_block(display: MythicDisplay) -> str:
    mercy = display.mercy
    maxed = mercy.pulls_since >= mercy.threshold
    parts = [
        f"Mythical Mercy: {mercy.pulls_since} / {mercy.threshold}" + (" (Maxed)" if maxed else ""),
        f"Mythical Chance: {format_percent(mercy.chance)}",
    ]
    if display.last_timestamp:
        parts.append(f"Last Mythical: {human_time(display.last_timestamp)}")
    parts.extend(["Progress", _progress_bar(mercy)])
    return "\n".join(parts)


def _progress_bar(mercy: MercySnapshot, segments: int = 10) -> str:
    threshold = max(mercy.threshold, 1)
    if mercy.pulls_since <= mercy.threshold:
        ratio = mercy.pulls_since / threshold
        filled_char = "🟩"
        empty_char = "⬜"
    else:
        overflow = mercy.pulls_since - mercy.threshold
        ratio = overflow / OVERFLOW_RANGE
        filled_char = "🟧"
        empty_char = "⬛"

    ratio = max(0.0, min(ratio, 1.0))
    filled = int(ratio * segments)
    empty = max(0, segments - filled)
    return f"{filled_char * filled}{empty_char * empty}"


def _apply_footer(embed: discord.Embed) -> None:
    embed.set_footer(text=FOOTER_TEXT)


def human_time(iso_value: str) -> str:
    if not iso_value:
        return ""
    try:
        dt = datetime.fromisoformat(iso_value)
    except ValueError:
        return iso_value
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M UTC")


__all__ = [
    "ShardDisplay",
    "MythicDisplay",
    "ShardTrackerView",
    "ShardTrackerController",
    "build_overview_embed",
    "build_detail_embed",
    "build_last_pulls_embed",
    "human_time",
]
