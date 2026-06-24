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
    "mystery": "Mystery",
    "remnant": "Remnant",
    "last_pulls": "Last Pulls",
}

FOOTER_TEXT = "For info about how this works type '@C1C Woadkeeper help shards'\nUse !shards again to summon a new panel if the previous one has expired."
OVERFLOW_RANGE = 100


@dataclass(frozen=True)
class ShardDisplay:
    key: str
    label: str
    owned: int
    mercy: MercySnapshot | None
    last_timestamp: str
    last_depth: int
    mercy_label: str = "Legendary"
    detail_note: str = ""


@dataclass(frozen=True)
class MythicDisplay:
    mercy: MercySnapshot | None
    last_timestamp: str
    last_depth: int
    mercy_label: str = "Legendary"
    detail_note: str = ""


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
        action_capabilities: Mapping[str, Sequence[str]] | None = None,
        mythic_controls: bool = True,
        timeout: float | None = None,
    ) -> None:
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.active_tab = active_tab
        self._controller = controller
        self._action_capabilities = dict(action_capabilities or {})
        # Tab buttons
        for tab in ("overview", "mystery", "ancient", "void", "primal", "sacred", "remnant", "last_pulls"):
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
        if active_tab == "overview":
            self._add_share_button(active_tab)
        elif active_tab in shard_labels:
            actions = tuple(self._action_capabilities.get(active_tab, ("stash", "pulls", "share", "legendary", "last_pulls")))
            self._add_primary_buttons(actions)
            if "legendary" in actions:
                self._add_legendary_button()
            if "mythical" in actions:
                self._add_mythical_button()
            if "last_pulls" in actions:
                self._add_last_pulls_button()

    def _add_primary_buttons(self, actions: Sequence[str]) -> None:
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
        if "pulls" in actions or "summons" in actions:
            self.add_item(
                _ShardButton(
                    custom_id=f"action:pulls:{self.active_tab}",
                    label="- Summons" if "summons" in actions else "- Pulls",
                    emoji=None,
                    style=discord.ButtonStyle.secondary,
                    owner_id=self.owner_id,
                    controller=self._controller,
                )
            )
        if "share" in actions:
            self._add_share_button(self.active_tab)

    def _add_share_button(self, tab: str) -> None:
        self.add_item(
            _ShardButton(
                custom_id=f"action:share:{tab}",
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

    def _add_mythical_button(self) -> None:
        self.add_item(
            _ShardButton(
                custom_id=f"action:mythical:{self.active_tab}",
                label="Got Mythical",
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
    "mystery": discord.Colour.green(),
    "remnant": discord.Colour.red(),
}


_AUTHOR_NAMES: Mapping[str, str] = {
    "overview": "Shard Overview — C1C",
    "last_pulls": "Last Pulls — C1C",
    "ancient": "Ancient Shards",
    "void": "Void Shards",
    "sacred": "Sacred Shards",
    "primal": "Primal Shards",
    "mystery": "Mystery Shards",
    "remnant": "Cursed Remnants",
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
    shard_emojis: Mapping[str, object] | None = None,
    author_name: str | None = None,
    author_icon_url: str | None = None,
    color: discord.Colour | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        colour=color or _TAB_COLORS.get("overview"),
        description="Your shard stash at a glance. May mercy be kinder than usual.",
    )
    embed.set_author(name=author_name or _AUTHOR_NAMES.get("overview"), icon_url=author_icon_url)

    by_key = {display.key: display for display in displays}
    for key in ("mystery", "ancient", "void", "primal", "sacred", "remnant"):
        display = by_key.get(key)
        if display is None:
            continue
        field_name = _section_heading(display, shard_emojis)
        if display.mercy is None:
            lines = [f"Owned: {max(display.owned, 0):,}"]
            embed.add_field(name=field_name, value=_code_block(lines), inline=False)
            continue
        if display.key == "primal":
            mythic_mercy = mythic.mercy
            lines = [
                f"Owned: {max(display.owned, 0):,}",
                "",
                "Legendary",
                f"Mercy: {display.mercy.pulls_since} / {display.mercy.threshold} | Chance: {format_percent(display.mercy.chance)}",
            ]
            if display.last_timestamp:
                lines.append(f"Last Legendary: {human_time(display.last_timestamp)}")
            lines += [
                "",
                "Mythical",
                f"Mercy: {mythic_mercy.pulls_since} / {mythic_mercy.threshold} | Chance: {format_percent(mythic_mercy.chance)}",
            ]
            if mythic.last_timestamp:
                lines.append(f"Last Mythical: {human_time(mythic.last_timestamp)}")
            embed.add_field(name=field_name, value=_code_block(lines), inline=False)
            continue
        lines = [
            f"Owned: {max(display.owned, 0):,}",
            f"Mercy: {display.mercy.pulls_since} / {display.mercy.threshold} | Chance: {format_percent(display.mercy.chance)}",
        ]
        last_label = display.mercy_label
        if display.last_timestamp:
            lines.append(f"Last {last_label}: {human_time(display.last_timestamp)}")
        embed.add_field(name=field_name, value=_code_block(lines), inline=False)

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
    if display.mercy is not None:
        embed.add_field(
            name="Progress",
            value=_progress_bar(display.mercy),
            inline=False,
        )
    if mythic:
        name = "Remnant Mythical" if display.key == "remnant" else "Primal Mythical"
        embed.add_field(name=name, value=_mythic_block(mythic), inline=False)
    _apply_footer(embed)
    return embed


def build_last_pulls_embed(
    *,
    member: discord.abc.User,
    displays: Sequence[ShardDisplay],
    mythic: MythicDisplay,
    base_rates: Mapping[str, str] = None,
    shard_emojis: Mapping[str, object] | None = None,
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
    by_key = {display.key: display for display in displays}
    for key in ("ancient", "void", "primal", "sacred", "remnant"):
        display = by_key.get(key)
        if display is None or display.mercy is None:
            continue
        field_name = _section_heading(display, shard_emojis)
        if key == "primal":
            leg_stamp = human_time(display.last_timestamp) if display.last_timestamp else "Never"
            leg_depth = f" ({display.last_depth} at pull)" if display.last_depth > 0 else ""
            mythic_stamp = human_time(mythic.last_timestamp) if mythic.last_timestamp else "Never"
            mythic_depth = f" ({mythic.last_depth} at pull)" if mythic.last_depth > 0 else ""
            lines = [
                "Legendary",
                f"Last Legendary: {leg_stamp}{leg_depth}",
                "",
                "Mythical",
                f"Last Mythical: {mythic_stamp}{mythic_depth}",
            ]
        else:
            stamp = human_time(display.last_timestamp) if display.last_timestamp else "Never"
            depth_label = "summon" if key == "remnant" else "pull"
            depth = f" ({display.last_depth} at {depth_label})" if display.last_depth > 0 else ""
            lines = [f"Last {display.mercy_label}: {stamp}{depth}"]
        embed.add_field(name=field_name, value=_code_block(lines), inline=False)
    _apply_footer(embed)
    return embed


def _section_heading(display: ShardDisplay, shard_emojis: Mapping[str, object] | None) -> str:
    emoji = str((shard_emojis or {}).get(display.key) or "").strip()
    if emoji:
        return f"{emoji} {display.label}"
    return display.label


def _code_block(lines: Sequence[str]) -> str:
    return "```text\n" + "\n".join(lines) + "\n```"


def _detail_block(display: ShardDisplay) -> str:
    parts = [f"Stash: **{max(display.owned, 0):,}**"]
    if display.detail_note:
        parts.append(display.detail_note)
    if display.mercy is None:
        return "\n".join(parts)
    mercy = display.mercy
    maxed = mercy.pulls_since >= mercy.threshold
    if display.key == "primal":
        parts += ["", "**Primal Legendary**"]
    label = display.mercy_label
    parts += [
        f"{label} Mercy: {mercy.pulls_since} / {mercy.threshold}" + (" (Maxed)" if maxed else ""),
        f"{label} Chance: {format_percent(mercy.chance)}",
    ]
    if display.last_timestamp:
        last_line = f"Last {label}: {human_time(display.last_timestamp)}"
        if display.last_depth:
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
