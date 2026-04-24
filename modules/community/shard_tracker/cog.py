"""Discord cog implementing the Shard & Mercy tracker commands."""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone
import logging
import os
from dataclasses import dataclass
from typing import Dict, Iterable, Literal, Optional

import discord
from discord import PartialEmoji
from discord.ext import commands

from c1c_coreops.rbac import admin_only
from c1c_coreops.helpers import help_metadata, tier
from modules.common import feature_flags
from modules.recruitment import emoji_pipeline
from shared import config as shared_config
from shared.config import get_admin_role_ids
from shared.logfmt import user_label

from .data import (
    ShardClanRow,
    ShardRecord,
    ShardSheetStore,
    ShardTrackerConfigError,
    ShardTrackerSheetError,
)
from .mercy import MERCY_CONFIGS, MercyConfig, MercySnapshot, mercy_state
from .threads import ShardThreadRouter
from .views import (
    MythicDisplay,
    ShardDisplay,
    ShardTrackerController,
    ShardTrackerView,
    ShardReminderOptView,
    build_detail_embed,
    build_last_pulls_embed,
    build_overview_embed,
)
from modules.common import runtime

log = logging.getLogger("c1c.shards.cog")


@dataclass(frozen=True)
class ShardKind:
    key: str
    label: str
    stash_field: str
    mercy_field: str
    mercy_config: MercyConfig
    timestamp_field: str
    depth_field: str


SHARD_KINDS: Dict[str, ShardKind] = {
    "ancient": ShardKind(
        key="ancient",
        label="Ancient",
        stash_field="ancients_owned",
        mercy_field="ancients_since_lego",
        mercy_config=MERCY_CONFIGS["ancient"],
        timestamp_field="last_ancient_lego_iso",
        depth_field="last_ancient_lego_depth",
    ),
    "void": ShardKind(
        key="void",
        label="Void",
        stash_field="voids_owned",
        mercy_field="voids_since_lego",
        mercy_config=MERCY_CONFIGS["void"],
        timestamp_field="last_void_lego_iso",
        depth_field="last_void_lego_depth",
    ),
    "sacred": ShardKind(
        key="sacred",
        label="Sacred",
        stash_field="sacreds_owned",
        mercy_field="sacreds_since_lego",
        mercy_config=MERCY_CONFIGS["sacred"],
        timestamp_field="last_sacred_lego_iso",
        depth_field="last_sacred_lego_depth",
    ),
    "primal": ShardKind(
        key="primal",
        label="Primal",
        stash_field="primals_owned",
        mercy_field="primals_since_lego",
        mercy_config=MERCY_CONFIGS["primal"],
        timestamp_field="last_primal_lego_iso",
        depth_field="last_primal_lego_depth",
    ),
}

_BASE_RATES = {
    "Ancient Legendary": "0.5%",
    "Void Legendary": "0.5%",
    "Sacred Legendary": "6%",
    "Primal Legendary": "1%",
    "Primal Mythical": "0.5%",
}

_TYPE_ALIASES = {
    "a": "ancient",
    "anc": "ancient",
    "ancients": "ancient",
    "v": "void",
    "voids": "void",
    "s": "sacred",
    "sac": "sacred",
    "sacreds": "sacred",
    "p": "primal",
    "primals": "primal",
    "myth": "mythic",
    "mythical": "mythic",
}

_FEATURE_TOGGLE_KEYS = ("shardtracker", "shard_tracker")

_SKIP_DISABLED = "disabled"
_SKIP_REMINDER_DISABLED = "reminder disabled"
_SKIP_NOT_IN_TIME_WINDOW = "not in time window"
_SKIP_MISSING_DESTINATION = "missing destination"
_SKIP_MISSING_OPT_IN_ROLE = "missing opt_in_role_id"
_SKIP_MISSING_TEMPLATE = "missing template field"
_SKIP_ALREADY_SENT = "already sent/dedupe"
_SKIP_DESTINATION_NOT_FOUND = "destination not found"
_SKIP_PERMISSION_FAILURE = "permission failure"


@dataclass(slots=True)
class ShardReminderRunStats:
    rows_loaded: int = 0
    eligible: int = 0
    skipped: int = 0
    dedupe_skipped: int = 0
    send_attempted: int = 0
    sent: int = 0
    failed: int = 0
    skip_reasons: Dict[str, int] | None = None

    def __post_init__(self) -> None:
        if self.skip_reasons is None:
            self.skip_reasons = {}

    def add_skip(self, reason: str) -> None:
        self.skipped += 1
        assert self.skip_reasons is not None
        self.skip_reasons[reason] = self.skip_reasons.get(reason, 0) + 1
        if reason == _SKIP_ALREADY_SENT:
            self.dedupe_skipped += 1


class ShardTracker(commands.Cog, ShardTrackerController):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.store = ShardSheetStore()
        self.threads = ShardThreadRouter(bot)
        self._locks: Dict[int, asyncio.Lock] = {}
        self._emoji_warning_emitted = False
        self._emoji_tags = self._load_emoji_tags()
        self._tab_emojis = self._load_tab_emojis()

    # === Commands ===

    @tier("user")
    @help_metadata(
        function_group="milestones",
        section="community",
        access_tier="user",
        usage="!shards [type]",
    )
    @commands.group(
        name="shards",
        invoke_without_command=True,
        help=(
            "Shard & Mercy tracker with overview and detail tabs. Only runs in the Shards & Mercy "
            "channel; creates your personal thread when needed."
        ),
    )
    async def shards(self, ctx: commands.Context, *, shard_type: str | None = None) -> None:
        if not await self._ensure_feature_enabled(ctx):
            return
        await self._handle_shards(ctx, shard_type)

    @tier("user")
    @help_metadata(
        function_group="milestones",
        section="community",
        access_tier="user",
        usage="!shards set <type> <count>",
    )
    @shards.command(
        name="set",
        help="Set the shard stash count for a specific type (non-negative integers only).",
    )
    async def shards_set(self, ctx: commands.Context, shard_type: str, count: int) -> None:
        if not await self._ensure_feature_enabled(ctx):
            return
        await self._handle_stash_set(ctx, shard_type, count)

    @tier("admin")
    @help_metadata(
        function_group="milestones",
        section="community",
        access_tier="admin",
        usage="!shards reminder-debug [force|force-send]",
    )
    @shards.command(
        name="reminder-debug",
        help=(
            "Admin diagnostic: run the shard weekly reminder processor once "
            "(force bypasses day/time window; force-send also bypasses dedupe)."
        ),
    )
    @commands.guild_only()
    @admin_only()
    async def shards_reminder_debug(self, ctx: commands.Context, mode: str | None = None) -> None:
        token = (mode or "").strip().lower()
        force_window = token in {"force", "force-send"}
        force_send = token == "force-send"
        if token and token not in {"force", "force-send"}:
            await ctx.reply(
                "Usage: `!shards reminder-debug [force|force-send]`.",
                mention_author=False,
            )
            return
        stats = await self.process_weekly_clan_reminders(
            now=datetime.now(timezone.utc),
            force_window=force_window,
            force_send=force_send,
            source="debug_command",
        )
        embed = self._build_reminder_debug_embed(stats=stats, force_window=force_window, force_send=force_send)
        await ctx.reply(embed=embed, mention_author=False)

    # === Button controller ===

    async def handle_button_interaction(
        self,
        *,
        interaction: discord.Interaction,
        custom_id: str,
        active_tab: str,
    ) -> None:
        if not self._feature_enabled():
            await interaction.response.send_message(
                self._feature_disabled_message(), ephemeral=True
            )
            return
        ctx_author = interaction.user
        guild = getattr(interaction.guild, "id", None)
        if guild is None:
            await interaction.response.send_message(
                "Shard tracker is only available in guild channels.", ephemeral=True
            )
            return

        action = self._parse_custom_id(custom_id)
        if action is None:
            await interaction.response.send_message(
                "Unknown action for shard tracker.", ephemeral=True
            )
            return

        async with self._user_lock(ctx_author.id):
            try:
                config = await self.store.get_config()
                record = await self.store.load_record(
                    ctx_author.id, ctx_author.display_name or ctx_author.name
                )
            except ShardTrackerConfigError as exc:
                await interaction.response.send_message(
                    self._config_error_message(str(exc)),
                    ephemeral=True,
                )
                await self._notify_admins(str(exc))
                return
            except ShardTrackerSheetError as exc:
                await interaction.response.send_message(
                    "Shard tracker sheet misconfigured. Please contact an admin.",
                    ephemeral=True,
                )
                await self._notify_admins(str(exc))
                return

            action_name, action_tab = action

            if action_name == "tab":
                new_tab = action_tab
                embed, view = self._build_panel(
                    ctx_author, record, interaction.channel, new_tab
                )
                await interaction.response.edit_message(embed=embed, view=view)
                return

            tab = action_tab or active_tab or "overview"
            if tab not in SHARD_KINDS and action_name in {"stash", "pulls", "legendary"}:
                await interaction.response.send_message(
                    "Pick a shard tab to use these buttons.", ephemeral=True
                )
                return

            if action_name == "stash":
                modal = _StashModal(
                    controller=self,
                    owner_id=ctx_author.id,
                    shard_key=tab,
                    active_tab=tab,
                )
                await interaction.response.send_modal(modal)
                return

            if action_name == "pulls":
                modal = _PullsModal(
                    controller=self,
                    owner_id=ctx_author.id,
                    shard_key=tab,
                    active_tab=tab,
                )
                await interaction.response.send_modal(modal)
                return

            if action_name == "legendary":
                modal = _LegendaryModal(
                    controller=self,
                    owner_id=ctx_author.id,
                    shard_key=tab,
                    active_tab=tab,
                )
                await interaction.response.send_modal(modal)
                return

            if action_name == "last_pulls":
                kind = self._resolve_kind(tab)
                if kind is None:
                    await interaction.response.send_message(
                        "Unknown shard type.", ephemeral=True
                    )
                    return
                modal = _LastPullsModal(
                    controller=self,
                    owner_id=ctx_author.id,
                    shard_key=tab,
                    active_tab=tab,
                    legendary_mercy=max(
                        0, getattr(record, kind.mercy_field, 0)
                    ),
                    mythical_mercy=max(0, record.primals_since_mythic),
                )
                await interaction.response.send_modal(modal)
                return

            if action_name == "share":
                await self._handle_share_summary_action(
                    interaction=interaction,
                    record=record,
                    default_clan_key=None,
                )
                return

    # === Internal helpers ===

    def _feature_disabled_message(self) -> str:
        return "Shard & Mercy tracking is currently disabled. Please check back later."

    async def _ensure_feature_enabled(self, ctx: commands.Context) -> bool:
        if self._feature_enabled():
            return True
        await ctx.reply(self._feature_disabled_message(), mention_author=False)
        return False

    def _feature_enabled(self) -> bool:
        if getattr(shared_config.features, "shard_tracker_enabled", False):
            return True
        toggles: Dict[str, bool] = {}
        for key in _FEATURE_TOGGLE_KEYS:
            try:
                toggles[key] = feature_flags.is_enabled(key)
            except Exception:
                log.exception("feature toggle check failed", extra={"feature": key})
        if toggles:
            try:
                shared_config.update_feature_flags_snapshot(toggles)
            except Exception:
                log.exception("failed to refresh feature toggle snapshot")
        return any(toggles.get(key, False) for key in _FEATURE_TOGGLE_KEYS)

    async def _handle_shards(
        self,
        ctx: commands.Context,
        shard_type: str | None,
    ) -> None:
        allowed, parent_channel, thread = await self._resolve_thread(ctx)
        if not allowed:
            return
        async with self._user_lock(ctx.author.id):
            try:
                config = await self.store.get_config()
                record = await self.store.load_record(ctx.author.id, ctx.author.display_name or ctx.author.name)
            except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
                await self._handle_config_failure(ctx, exc)
                return
            record.snapshot_name(ctx.author.display_name or ctx.author.name)
            await self.store.save_record(config, record)
        tab = self._resolve_kind_key(shard_type) if shard_type else "overview"
        if tab and tab not in SHARD_KINDS:
            tab = "overview"
        embed, view = self._build_panel(ctx.author, record, thread, tab)
        await self._send_thread_message(ctx, parent_channel, thread, embed, view)

    async def _handle_stash_set(
        self, ctx: commands.Context, shard_type: str, count: int
    ) -> None:
        kind = self._resolve_kind(shard_type)
        if kind is None:
            await ctx.reply(self._invalid_type_message(), mention_author=False)
            return
        count = max(0, count)
        async with self._user_lock(ctx.author.id):
            try:
                config = await self.store.get_config()
                record = await self.store.load_record(ctx.author.id, ctx.author.display_name or ctx.author.name)
            except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
                await self._handle_config_failure(ctx, exc)
                return
            setattr(record, kind.stash_field, count)
            record.snapshot_name(ctx.author.display_name or ctx.author.name)
            await self.store.save_record(config, record)
        await ctx.reply(
            f"{kind.label} stash updated to **{count:,}**.",
            mention_author=False,
        )
        await self._log_action("stash_set", ctx.author, ctx.channel, f"{kind.label}={count}")

    def _parse_custom_id(self, custom_id: str) -> tuple[str, str | None] | None:
        if custom_id.startswith("tab:"):
            return ("tab", custom_id.split(":", 1)[1])
        parts = custom_id.split(":", 2)
        if len(parts) == 3 and parts[0] == "action":
            return (parts[1], parts[2])
        return None

    async def process_stash_modal(
        self,
        *,
        interaction: discord.Interaction,
        shard_key: str,
        amount: int,
        active_tab: str,
    ) -> None:
        kind = self._resolve_kind(shard_key)
        if kind is None:
            await interaction.response.send_message("Unknown shard type.", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message(
                "Please enter a positive number.", ephemeral=True
            )
            return

        async with self._user_lock(interaction.user.id):
            try:
                config = await self.store.get_config()
                record = await self.store.load_record(
                    interaction.user.id,
                    interaction.user.display_name or interaction.user.name,
                )
            except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
                await interaction.response.send_message(
                    self._config_error_message(str(exc)), ephemeral=True
                )
                await self._notify_admins(str(exc))
                return

            self._apply_stash_increase(record, kind, amount)
            record.snapshot_name(interaction.user.display_name or interaction.user.name)
            await self.store.save_record(config, record)

        embed, view = self._build_panel(interaction.user, record, interaction.channel, active_tab)
        await interaction.response.edit_message(embed=embed, view=view)
        await self._log_action(
            "stash_add",
            interaction.user,
            interaction.channel,
            f"{kind.label} stash +{amount}",
        )

    async def process_pulls_modal(
        self,
        *,
        interaction: discord.Interaction,
        shard_key: str,
        amount: int,
        active_tab: str,
    ) -> None:
        kind = self._resolve_kind(shard_key)
        if kind is None:
            await interaction.response.send_message("Unknown shard type.", ephemeral=True)
            return
        if amount <= 0:
            await interaction.response.send_message(
                "Please enter a positive number.", ephemeral=True
            )
            return

        async with self._user_lock(interaction.user.id):
            try:
                config = await self.store.get_config()
                record = await self.store.load_record(
                    interaction.user.id,
                    interaction.user.display_name or interaction.user.name,
                )
            except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
                await interaction.response.send_message(
                    self._config_error_message(str(exc)), ephemeral=True
                )
                await self._notify_admins(str(exc))
                return

            self._apply_pull_usage(record, kind, amount)
            record.snapshot_name(interaction.user.display_name or interaction.user.name)
            await self.store.save_record(config, record)

        embed, view = self._build_panel(interaction.user, record, interaction.channel, active_tab)
        await interaction.response.edit_message(embed=embed, view=view)
        await self._log_action(
            "pulls_logged",
            interaction.user,
            interaction.channel,
            f"{kind.label} -{amount}",
        )

    async def process_legendary_modal(
        self,
        *,
        interaction: discord.Interaction,
        shard_key: str,
        total_pulls: int,
        after_champion: int,
        active_tab: str,
    ) -> None:
        kind = self._resolve_kind(shard_key)
        if kind is None:
            await interaction.response.send_message("Unknown shard type.", ephemeral=True)
            return
        if total_pulls <= 0 or after_champion < 0:
            await interaction.response.send_message(
                "Please enter positive numbers.", ephemeral=True
            )
            return
        if after_champion > total_pulls:
            await interaction.response.send_message(
                "Pulls after the champion cannot exceed total pulls.",
                ephemeral=True,
            )
            return

        async with self._user_lock(interaction.user.id):
            try:
                config = await self.store.get_config()
                record = await self.store.load_record(
                    interaction.user.id,
                    interaction.user.display_name or interaction.user.name,
                )
            except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
                await interaction.response.send_message(
                    self._config_error_message(str(exc)), ephemeral=True
                )
                await self._notify_admins(str(exc))
                return

            legendary_before = max(0, record.primals_since_lego)
            mythical_before = max(0, record.primals_since_mythic)

            self._apply_pull_usage(record, kind, total_pulls)

            current_mercy = max(0, getattr(record, kind.mercy_field, 0))
            drop_depth = max(0, current_mercy - after_champion)
            setattr(record, kind.mercy_field, drop_depth)

            if kind.key == "primal":
                record.snapshot_name(interaction.user.display_name or interaction.user.name)
                await self.store.save_record(config, record)

                await interaction.response.send_message(
                    "What did you pull?",
                    view=_PrimalDropChoiceView(
                        controller=self,
                        owner_id=getattr(interaction.user, "id", 0),
                        active_tab=active_tab,
                        panel_message=interaction.message,
                        after_champion=after_champion,
                        total_pulls=total_pulls,
                        legendary_mercy=legendary_before,
                        mythical_mercy=mythical_before,
                    ),
                    ephemeral=True,
                )
                return

            self._apply_legendary_reset(record, kind)
            setattr(record, kind.mercy_field, after_champion)
            record.snapshot_name(interaction.user.display_name or interaction.user.name)
            await self.store.save_record(config, record)

        embed, view = self._build_panel(
            interaction.user, record, interaction.channel, active_tab
        )
        await interaction.response.edit_message(embed=embed, view=view)
        await self._log_action(
            "legendary_reset",
            interaction.user,
            interaction.channel,
            f"{kind.label} drop: pulls={total_pulls}, after={after_champion}",
        )

    async def process_last_pulls_modal(
        self,
        *,
        interaction: discord.Interaction,
        shard_key: str,
        active_tab: str,
        legendary_mercy: int,
        mythical_mercy: int | None,
    ) -> None:
        kind = self._resolve_kind(shard_key)
        if kind is None:
            await interaction.response.send_message("Unknown shard type.", ephemeral=True)
            return
        if legendary_mercy < 0 or (mythical_mercy is not None and mythical_mercy < 0):
            await interaction.response.send_message(
                "Please provide non-negative numbers.", ephemeral=True
            )
            return

        async with self._user_lock(interaction.user.id):
            try:
                config = await self.store.get_config()
                record = await self.store.load_record(
                    interaction.user.id,
                    interaction.user.display_name or interaction.user.name,
                )
            except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
                await interaction.response.send_message(
                    self._config_error_message(str(exc)), ephemeral=True
                )
                await self._notify_admins(str(exc))
                return

            self._apply_manual_mercy(
                record,
                kind,
                legendary_mercy=legendary_mercy,
                mythical_mercy=mythical_mercy,
            )
            record.snapshot_name(interaction.user.display_name or interaction.user.name)
            await self.store.save_record(config, record)

        embed, view = self._build_panel(
            interaction.user, record, interaction.channel, active_tab
        )
        await interaction.response.edit_message(embed=embed, view=view)
        await self._log_action(
            "manual_mercy",
            interaction.user,
            interaction.channel,
            (
                f"{kind.label} mercy set to {legendary_mercy}"
                if kind.key != "primal"
                else (
                    "Primal mercy set to "
                    f"legendary={legendary_mercy}, mythic={mythical_mercy or 0}"
                )
            ),
        )

    async def process_primal_choice(
        self,
        *,
        interaction: discord.Interaction,
        choice: str,
        active_tab: str,
        panel_message: discord.Message | None,
        after_champion: int,
        total_pulls: int,
        legendary_mercy: int,
        mythical_mercy: int,
    ) -> None:
        if choice not in {"legendary", "mythical"}:
            await interaction.response.send_message(
                "Unknown primal drop type.", ephemeral=True
            )
            return
        async with self._user_lock(interaction.user.id):
            try:
                config = await self.store.get_config()
                record = await self.store.load_record(
                    interaction.user.id,
                    interaction.user.display_name or interaction.user.name,
                )
            except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
                await interaction.response.send_message(
                    self._config_error_message(str(exc)), ephemeral=True
                )
                await self._notify_admins(str(exc))
                return

            before_champion = max(0, total_pulls - after_champion)
            legendary_before = max(0, legendary_mercy)
            mythical_before = max(0, mythical_mercy)

            if choice == "legendary":
                record.primals_since_lego = legendary_before + before_champion
                self._apply_primal_legendary(record)
                record.primals_since_lego = max(0, after_champion)
                record.primals_since_mythic = mythical_before + max(0, total_pulls)
            else:
                depth_mythical = mythical_before + before_champion
                self._apply_primal_mythical(record, depth=depth_mythical)
                record.primals_since_mythic = max(0, after_champion)
                record.primals_since_lego = legendary_before + max(0, total_pulls)
            record.snapshot_name(interaction.user.display_name or interaction.user.name)
            await self.store.save_record(config, record)

        target_message = panel_message or interaction.message
        if target_message:
            embed, view = self._build_panel(interaction.user, record, target_message.channel, active_tab)
            await target_message.edit(embed=embed, view=view)
        await interaction.response.edit_message(content="Logged!", view=None)
        await self._log_action(
            "primal_drop",
            interaction.user,
            interaction.channel,
            f"Primal {choice}",
        )

    def _apply_stash_increase(self, record: ShardRecord, kind: ShardKind, amount: int) -> None:
        owned = max(0, getattr(record, kind.stash_field, 0))
        setattr(record, kind.stash_field, owned + amount)

    def _apply_pull_usage(self, record: ShardRecord, kind: ShardKind, amount: int) -> None:
        owned = max(0, getattr(record, kind.stash_field, 0))
        new_owned = max(0, owned - amount)
        setattr(record, kind.stash_field, new_owned)
        current_mercy = max(0, getattr(record, kind.mercy_field, 0))
        setattr(record, kind.mercy_field, current_mercy + amount)
        if kind.key == "primal":
            record.primals_since_mythic = max(0, record.primals_since_mythic) + amount

    def _apply_legendary_reset(self, record: ShardRecord, kind: ShardKind) -> None:
        current_depth = max(0, getattr(record, kind.mercy_field, 0))
        setattr(record, kind.mercy_field, 0)
        setattr(record, kind.depth_field, current_depth)
        setattr(record, kind.timestamp_field, self._now_iso())

    def _apply_primal_legendary(self, record: ShardRecord) -> None:
        depth = max(0, record.primals_since_lego)
        record.primals_since_lego = 0
        record.last_primal_lego_depth = depth
        record.last_primal_lego_iso = self._now_iso()

    def _apply_primal_mythical(self, record: ShardRecord, *, depth: int) -> None:
        depth_value = max(0, depth)
        timestamp = self._now_iso()
        record.last_primal_mythic_depth = depth_value
        record.last_primal_mythic_iso = timestamp

    def _apply_manual_mercy(
        self,
        record: ShardRecord,
        kind: ShardKind,
        *,
        legendary_mercy: int,
        mythical_mercy: int | None,
    ) -> None:
        if kind.key == "primal":
            record.primals_since_lego = max(0, legendary_mercy)
            record.primals_since_mythic = max(
                0, mythical_mercy if mythical_mercy is not None else legendary_mercy
            )
        else:
            setattr(record, kind.mercy_field, max(0, legendary_mercy))

    async def _handle_mercy_set(
        self, ctx: commands.Context, shard_type: str, count: int
    ) -> None:
        key = self._resolve_kind_key(shard_type)
        if key == "mythic":
            attr = "primals_since_mythic"
            label = "Primal mythic"
        else:
            kind = self._resolve_kind(shard_type)
            if kind is None:
                await ctx.reply(self._invalid_type_message(), mention_author=False)
                return
            attr = kind.mercy_field
            label = kind.label
        count = max(0, count)
        async with self._user_lock(ctx.author.id):
            try:
                config = await self.store.get_config()
                record = await self.store.load_record(ctx.author.id, ctx.author.display_name or ctx.author.name)
            except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
                await self._handle_config_failure(ctx, exc)
                return
            setattr(record, attr, count)
            record.snapshot_name(ctx.author.display_name or ctx.author.name)
            await self.store.save_record(config, record)
        await ctx.reply(
            f"{label} mercy counter set to **{count:,}**.",
            mention_author=False,
        )
        await self._log_action("mercy_set", ctx.author, ctx.channel, f"{label}={count}")

    async def _handle_lego(
        self, ctx: commands.Context, shard_type: str, after_count: int
    ) -> None:
        kind = self._resolve_kind(shard_type)
        if kind is None:
            await ctx.reply(self._invalid_type_message(), mention_author=False)
            return
        after = max(0, after_count)
        async with self._user_lock(ctx.author.id):
            try:
                config = await self.store.get_config()
                record = await self.store.load_record(ctx.author.id, ctx.author.display_name or ctx.author.name)
            except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
                await self._handle_config_failure(ctx, exc)
                return
            setattr(record, kind.mercy_field, after)
            setattr(record, kind.timestamp_field, self._now_iso())
            record.snapshot_name(ctx.author.display_name or ctx.author.name)
            await self.store.save_record(config, record)
        await ctx.reply(
            f"Logged a {kind.label} LEGO. Counter now **{after:,}**.",
            mention_author=False,
        )
        await self._log_action("lego", ctx.author, ctx.channel, f"{kind.label} after={after}")

    async def _handle_mythic(self, ctx: commands.Context, after_count: int) -> None:
        after = max(0, after_count)
        async with self._user_lock(ctx.author.id):
            try:
                config = await self.store.get_config()
                record = await self.store.load_record(ctx.author.id, ctx.author.display_name or ctx.author.name)
            except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
                await self._handle_config_failure(ctx, exc)
                return
            record.primals_since_mythic = after
            record.primals_since_lego = after
            record.last_primal_mythic_iso = self._now_iso()
            record.snapshot_name(ctx.author.display_name or ctx.author.name)
            await self.store.save_record(config, record)
        await ctx.reply(
            f"Logged a Primal mythic. Mercy counter now **{after:,}**.",
            mention_author=False,
        )
        await self._log_action("mythic", ctx.author, ctx.channel, f"Primal after={after}")

    async def _resolve_thread(
        self, ctx: commands.Context
    ) -> tuple[bool, discord.TextChannel | None, discord.Thread | None]:
        try:
            config = await self.store.get_config()
        except ShardTrackerConfigError as exc:
            await self._handle_config_failure(ctx, exc)
            return False, None, None
        channel = ctx.channel
        target_channel: discord.TextChannel | None = None
        thread: discord.Thread | None = None
        if isinstance(channel, discord.Thread):
            parent = channel.parent
            if not parent or parent.id != config.channel_id:
                await ctx.reply(self._channel_error_message(config.channel_id), mention_author=False)
                return False, None, None
            owner_id = self.threads.owner_id_for(channel)
            if owner_id and owner_id != ctx.author.id:
                await ctx.reply("Please use your own shard thread in the feature channel.", mention_author=False)
                return False, None, None
            target_channel = parent
            thread = channel
        elif isinstance(channel, discord.TextChannel):
            if channel.id != config.channel_id:
                await ctx.reply(self._channel_error_message(config.channel_id), mention_author=False)
                return False, None, None
            thread, _created = await self.threads.ensure_thread(parent=channel, user=ctx.author)
            target_channel = channel
        else:
            await ctx.reply(self._channel_error_message(config.channel_id), mention_author=False)
            return False, None, None
        return True, target_channel, thread

    async def _send_thread_message(
        self,
        ctx: commands.Context,
        parent: discord.TextChannel | None,
        thread: discord.Thread | None,
        embed: discord.Embed,
        view: discord.ui.View | None,
    ) -> None:
        if thread is None:
            await ctx.reply("Unable to locate or create your shard thread.", mention_author=False)
            return
        content = f"{ctx.author.mention}"
        await thread.send(content=content, embed=embed, view=view)
        if parent and parent == ctx.channel:
            await ctx.reply(
                f"📬 Posted in your shard thread: {thread.mention}",
                mention_author=False,
            )

    def _build_panel(
        self,
        member: discord.abc.User,
        record: ShardRecord,
        channel: discord.abc.GuildChannel | discord.Thread | None,
        active_tab: str,
    ) -> tuple[discord.Embed, ShardTrackerView]:
        displays = [self._build_display(record, kind) for kind in SHARD_KINDS.values()]
        mythic = self._build_mythic_display(record)
        tab = active_tab if active_tab else "overview"
        author_name, author_key = self._author_meta(tab, member.display_name or member.name)
        guild = getattr(channel, "guild", None)
        author_icon_url = self._author_icon_url(guild, author_key)
        color = self._tab_color(tab)

        if tab == "last_pulls":
            embed = build_last_pulls_embed(
                member=member,
                displays=displays,
                mythic=mythic,
                base_rates=_BASE_RATES,
                author_name=author_name,
                author_icon_url=author_icon_url,
                color=color,
            )
        elif tab in SHARD_KINDS:
            target = next((d for d in displays if d.key == tab), displays[0])
            embed = build_detail_embed(
                member=member,
                display=target,
                mythic=mythic if tab == "primal" else None,
                author_name=author_name,
                author_icon_url=author_icon_url,
                color=color,
            )
        else:
            embed = build_overview_embed(
                member=member,
                displays=displays,
                mythic=mythic,
                author_name=author_name,
                author_icon_url=author_icon_url,
                color=color,
            )

        labels = {kind.key: kind.label for kind in SHARD_KINDS.values()}
        view = ShardTrackerView(
            owner_id=getattr(member, "id", 0),
            controller=self,
            shard_labels=labels,
            shard_emojis=self._tab_emojis,
            active_tab=tab,
        )
        return embed, view

    async def _handle_share_summary_action(
        self,
        *,
        interaction: discord.Interaction,
        record: ShardRecord,
        default_clan_key: str | None,
    ) -> None:
        try:
            clans = await self.store.get_enabled_clans()
        except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
            await interaction.response.send_message(self._config_error_message(str(exc)), ephemeral=True)
            await self._notify_admins(str(exc))
            return

        selected = await self._resolve_clan_selection(
            interaction=interaction,
            clans=clans,
            default_clan_key=default_clan_key,
            action="share",
        )
        if selected is None:
            return
        destination = self._resolve_share_destination(selected)
        if destination is None:
            await interaction.followup.send(
                f"Clan `{selected.clan_key}` is enabled but has no valid share destination configured.",
                ephemeral=True,
            )
            await self._notify_admins(
                f"SHARD_CLANS_TAB clan={selected.clan_key} missing/invalid share destination"
            )
            return

        embed = self._build_share_embed(interaction.user, record, selected)
        await destination.send(embed=embed)
        await interaction.followup.send(
            f"Shared your shard summary to {destination.mention} for `{selected.clan_key}`.",
            ephemeral=True,
        )

    async def handle_reminder_opt_action(
        self,
        *,
        interaction: discord.Interaction,
        action: Literal["in", "out"],
        clan_key: str | None,
    ) -> None:
        try:
            clans = await self.store.get_enabled_clans()
        except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
            await interaction.response.send_message(self._config_error_message(str(exc)), ephemeral=True)
            await self._notify_admins(str(exc))
            return
        selected = await self._resolve_clan_selection(
            interaction=interaction,
            clans=clans,
            default_clan_key=clan_key,
            action="opt_in" if action == "in" else "opt_out",
        )
        if selected is None:
            return
        if selected.opt_in_role_id is None:
            await interaction.followup.send(
                f"Clan `{selected.clan_key}` has no opt-in role configured.",
                ephemeral=True,
            )
            return
        guild = interaction.guild
        if guild is None:
            await interaction.followup.send("Reminder role actions only work in a server.", ephemeral=True)
            return
        member = interaction.user if isinstance(interaction.user, discord.Member) else guild.get_member(interaction.user.id)
        if member is None:
            await interaction.followup.send("Couldn’t resolve your member record right now.", ephemeral=True)
            return
        role = guild.get_role(selected.opt_in_role_id)
        if role is None:
            await interaction.followup.send(
                f"Reminder role for `{selected.clan_key}` is missing in this server.",
                ephemeral=True,
            )
            return
        try:
            if action == "in":
                if role in member.roles:
                    await interaction.followup.send("Already opted in.", ephemeral=True)
                    return
                await member.add_roles(role, reason=f"Shard reminder opt-in ({selected.clan_key})")
                await interaction.followup.send("Opted in for weekly shard reminders.", ephemeral=True)
                return
            if role not in member.roles:
                await interaction.followup.send("You’re already opted out.", ephemeral=True)
                return
            await member.remove_roles(role, reason=f"Shard reminder opt-out ({selected.clan_key})")
            await interaction.followup.send("Opted out from weekly shard reminders.", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("I don’t have permission to update that role.", ephemeral=True)
        except Exception:
            log.exception("shard reminder role update failed")
            await interaction.followup.send("Couldn’t update your reminder role right now.", ephemeral=True)

    async def handle_clan_choice(
        self,
        *,
        interaction: discord.Interaction,
        action: Literal["share", "opt_in", "opt_out"],
        clan_key: str,
    ) -> None:
        if action in {"opt_in", "opt_out"}:
            await self.handle_reminder_opt_action(
                interaction=interaction,
                action="in" if action == "opt_in" else "out",
                clan_key=clan_key,
            )
            return
        async with self._user_lock(interaction.user.id):
            try:
                record = await self.store.load_record(
                    interaction.user.id,
                    interaction.user.display_name or interaction.user.name,
                )
            except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
                await interaction.response.send_message(self._config_error_message(str(exc)), ephemeral=True)
                await self._notify_admins(str(exc))
                return
        await self._handle_share_summary_action(
            interaction=interaction,
            record=record,
            default_clan_key=clan_key,
        )

    async def _resolve_clan_selection(
        self,
        *,
        interaction: discord.Interaction,
        clans: list[ShardClanRow],
        default_clan_key: str | None,
        action: Literal["share", "opt_in", "opt_out"],
    ) -> ShardClanRow | None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        if not clans:
            await interaction.followup.send("No enabled shard clans are configured.", ephemeral=True)
            return None
        if default_clan_key:
            selected = next((row for row in clans if row.clan_key.lower() == default_clan_key.lower()), None)
            if selected is not None:
                return selected
        if len(clans) == 1:
            return clans[0]
        view = _ShardClanChoiceView(controller=self, clans=clans, action=action)
        await interaction.followup.send(
            "Choose a clan to continue.",
            view=view,
            ephemeral=True,
        )
        return None

    def _resolve_share_destination(
        self, clan: ShardClanRow
    ) -> tuple[discord.abc.Messageable | None, str | None]:
        if clan.share_thread_id:
            target = self.bot.get_channel(clan.share_thread_id)
            if isinstance(target, discord.Thread):
                return target, None
            return None, _SKIP_DESTINATION_NOT_FOUND
        if clan.share_channel_id:
            target = self.bot.get_channel(clan.share_channel_id)
            if isinstance(target, (discord.TextChannel, discord.Thread)):
                return target, None
            return None, _SKIP_DESTINATION_NOT_FOUND
        return None, _SKIP_MISSING_DESTINATION

    def _build_share_embed(
        self,
        member: discord.abc.User,
        record: ShardRecord,
        clan: ShardClanRow,
    ) -> discord.Embed:
        displays = [self._build_display(record, kind) for kind in SHARD_KINDS.values()]
        mythic = self._build_mythic_display(record)
        embed = build_overview_embed(member=member, displays=displays, mythic=mythic)
        embed.title = f"Shard Snapshot — {member.display_name}"
        embed.description = f"Shared to `{clan.clan_key}`."
        return embed

    def _build_clan_reminder_embed(
        self,
        *,
        clan: ShardClanRow,
        guild: discord.Guild | None,
    ) -> discord.Embed:
        color = self._parse_color_hex(clan.color_hex)
        embed = discord.Embed(
            title=clan.title,
            description=clan.body,
            colour=color,
            timestamp=datetime.now(timezone.utc),
        )
        footer_icon = self._resolve_footer_icon_url(guild, clan.emoji_name_or_id)
        embed.set_footer(text=clan.footer, icon_url=footer_icon)
        return embed

    def _build_reminder_debug_embed(
        self, *, stats: ShardReminderRunStats, force_window: bool, force_send: bool
    ) -> discord.Embed:
        mode_bits = []
        if force_window:
            mode_bits.append("force-window")
        if force_send:
            mode_bits.append("force-send")
        mode_display = ", ".join(mode_bits) if mode_bits else "normal"
        skip_lines = ["None"]
        if stats.skip_reasons:
            skip_lines = [f"• {reason}: {count}" for reason, count in sorted(stats.skip_reasons.items())]
        embed = discord.Embed(
            title="Shard Reminder Debug Run",
            description=f"Mode: `{mode_display}`",
            colour=discord.Colour.orange(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(
            name="Counts",
            value=(
                f"rows loaded: **{stats.rows_loaded}**\n"
                f"eligible: **{stats.eligible}**\n"
                f"skipped: **{stats.skipped}**\n"
                f"dedupe-skipped: **{stats.dedupe_skipped}**\n"
                f"send-attempted: **{stats.send_attempted}**\n"
                f"sent: **{stats.sent}**\n"
                f"failed: **{stats.failed}**"
            ),
            inline=False,
        )
        embed.add_field(name="Skip reasons", value="\n".join(skip_lines), inline=False)
        return embed

    async def process_weekly_clan_reminders(
        self,
        *,
        now: datetime | None = None,
        force_window: bool = False,
        force_send: bool = False,
        source: str = "scheduler",
    ) -> ShardReminderRunStats:
        reference = now.astimezone(timezone.utc) if now else datetime.now(timezone.utc)
        stats = ShardReminderRunStats()
        log.info(
            "shard reminder scheduler tick started",
            extra={"source": source, "force_window": force_window, "force_send": force_send},
        )
        try:
            clans = await self.store.get_clans()
        except Exception:
            log.exception("failed to load shard clans for weekly reminders")
            return stats
        stats.rows_loaded = len(clans)
        log.info(
            "shard reminder rows loaded",
            extra={"rows_loaded": stats.rows_loaded, "source": source},
        )
        for clan in clans:
            if not clan.enabled:
                stats.add_skip(_SKIP_DISABLED)
                log.info("shard reminder row skipped", extra={"clan_key": clan.clan_key, "reason": _SKIP_DISABLED})
                continue
            if not clan.reminder_enabled:
                stats.add_skip(_SKIP_REMINDER_DISABLED)
                log.info(
                    "shard reminder row skipped",
                    extra={"clan_key": clan.clan_key, "reason": _SKIP_REMINDER_DISABLED},
                )
                continue
            if not clan.opt_in_role_id:
                await self._notify_admins(f"SHARD_CLANS_TAB clan={clan.clan_key} missing opt_in_role_id")
                stats.add_skip(_SKIP_MISSING_OPT_IN_ROLE)
                log.info(
                    "shard reminder row skipped",
                    extra={"clan_key": clan.clan_key, "reason": _SKIP_MISSING_OPT_IN_ROLE},
                )
                continue
            scheduled = self._scheduled_window_start(clan, reference)
            if scheduled is None:
                await self._notify_admins(f"SHARD_CLANS_TAB clan={clan.clan_key} has invalid reminder_day/time")
                stats.add_skip(_SKIP_NOT_IN_TIME_WINDOW)
                log.info(
                    "shard reminder row skipped",
                    extra={"clan_key": clan.clan_key, "reason": _SKIP_NOT_IN_TIME_WINDOW},
                )
                continue
            window_key = scheduled.date().isoformat()
            if not force_window and (reference < scheduled or reference > (scheduled + timedelta(minutes=30))):
                stats.add_skip(_SKIP_NOT_IN_TIME_WINDOW)
                log.info(
                    "shard reminder row skipped",
                    extra={
                        "clan_key": clan.clan_key,
                        "reason": _SKIP_NOT_IN_TIME_WINDOW,
                        "scheduled": scheduled.isoformat(),
                        "reference": reference.isoformat(),
                    },
                )
                continue
            try:
                sent_keys = await self.store.get_sent_weekly_reminder_keys(clan.clan_key)
            except Exception:
                log.exception("failed loading shard reminder dedupe", extra={"clan_key": clan.clan_key})
                sent_keys = set()
            if (not force_send) and window_key in sent_keys:
                stats.add_skip(_SKIP_ALREADY_SENT)
                log.info(
                    "shard reminder row skipped",
                    extra={"clan_key": clan.clan_key, "reason": _SKIP_ALREADY_SENT, "window_key": window_key},
                )
                continue
            destination, destination_reason = self._resolve_share_destination(clan)
            if destination is None:
                await self._notify_admins(f"SHARD_CLANS_TAB clan={clan.clan_key} destination missing")
                reason = destination_reason or _SKIP_MISSING_DESTINATION
                stats.add_skip(reason)
                log.info("shard reminder row skipped", extra={"clan_key": clan.clan_key, "reason": reason})
                continue
            if not clan.title or not clan.body or not clan.footer:
                await self._notify_admins(f"SHARD_CLANS_TAB clan={clan.clan_key} missing reminder embed fields")
                stats.add_skip(_SKIP_MISSING_TEMPLATE)
                log.info(
                    "shard reminder row skipped",
                    extra={"clan_key": clan.clan_key, "reason": _SKIP_MISSING_TEMPLATE},
                )
                continue
            stats.eligible += 1
            log.info("shard reminder row eligible", extra={"clan_key": clan.clan_key, "window_key": window_key})
            embed = self._build_clan_reminder_embed(clan=clan, guild=getattr(destination, "guild", None))
            mention = f"<@&{clan.opt_in_role_id}>"
            stats.send_attempted += 1
            log.info(
                "shard reminder send attempt started",
                extra={"clan_key": clan.clan_key, "window_key": window_key, "source": source},
            )
            try:
                message = await destination.send(content=mention, embed=embed, view=ShardReminderOptView())
                await self.store.mark_weekly_reminder_sent(
                    clan_key=clan.clan_key, window_key=window_key, sent_at=reference
                )
                stats.sent += 1
                log.info(
                    "shard reminder send success",
                    extra={"clan_key": clan.clan_key, "message_id": getattr(message, "id", None)},
                )
            except discord.Forbidden:
                stats.failed += 1
                stats.add_skip(_SKIP_PERMISSION_FAILURE)
                log.exception(
                    "shard reminder send failure",
                    extra={"clan_key": clan.clan_key, "reason": _SKIP_PERMISSION_FAILURE},
                )
            except Exception:
                stats.failed += 1
                log.exception("shard reminder send failure", extra={"clan_key": clan.clan_key})
        return stats

    def _scheduled_window_start(self, clan: ShardClanRow, now_utc: datetime) -> datetime | None:
        day_names = {
            "monday": 0,
            "tuesday": 1,
            "wednesday": 2,
            "thursday": 3,
            "friday": 4,
            "saturday": 5,
            "sunday": 6,
        }
        day_token = clan.reminder_day.strip().lower()
        if day_token not in day_names:
            return None
        time_token = clan.reminder_time_utc.strip()
        try:
            hour, minute = [int(part) for part in time_token.split(":", 1)]
            scheduled_time = time(hour=hour, minute=minute, tzinfo=timezone.utc)
        except Exception:
            return None
        now_day = now_utc.weekday()
        delta_days = (now_day - day_names[day_token]) % 7
        candidate_date = (now_utc - timedelta(days=delta_days)).date()
        return datetime.combine(candidate_date, scheduled_time)

    @staticmethod
    def _parse_color_hex(value: str) -> discord.Colour:
        text = str(value or "").strip()
        if text.startswith("#"):
            text = text[1:]
        elif text.lower().startswith("0x"):
            text = text[2:]
        try:
            return discord.Colour(int(text, 16))
        except Exception:
            try:
                return discord.Colour(int(str(value or "").strip()))
            except Exception:
                return discord.Colour.blurple()

    @staticmethod
    def _resolve_footer_icon_url(guild: discord.Guild | None, emoji_token: str) -> str | None:
        token = str(emoji_token or "").strip()
        if not token or guild is None:
            return None
        try:
            emoji_id = int(token)
        except ValueError:
            emoji = emoji_pipeline.emoji_for_tag(guild, token)
            return str(emoji.url) if emoji else None
        emoji = guild.get_emoji(emoji_id)
        return str(emoji.url) if emoji else None

    def _build_display(self, record: ShardRecord, kind: ShardKind) -> ShardDisplay:
        owned = max(0, getattr(record, kind.stash_field, 0))
        since = max(0, getattr(record, kind.mercy_field, 0))
        mercy = mercy_state(kind.key, since)
        timestamp = getattr(record, kind.timestamp_field, "")
        depth = max(0, getattr(record, kind.depth_field, 0))
        return ShardDisplay(
            key=kind.key,
            label=kind.label,
            owned=owned,
            mercy=mercy,
            last_timestamp=timestamp,
            last_depth=depth,
        )

    def _build_mythic_display(self, record: ShardRecord) -> MythicDisplay:
        state = mercy_state("primal_mythic", max(0, record.primals_since_mythic))
        return MythicDisplay(
            mercy=state,
            last_timestamp=record.last_primal_mythic_iso,
            last_depth=max(0, record.last_primal_mythic_depth),
        )

    def _load_emoji_tags(self) -> dict[str, str]:
        return {
            "overview": shared_config.get_shard_panel_overview_emoji("c1c"),
            "ancient": shared_config.get_shard_emoji_ancient("ancient"),
            "void": shared_config.get_shard_emoji_void("void"),
            "sacred": shared_config.get_shard_emoji_sacred("sacred"),
            "primal": shared_config.get_shard_emoji_primal("primal"),
        }

    def _load_tab_emojis(self) -> dict[str, discord.PartialEmoji | None]:
        return {
            "ancient": self._parse_partial_emoji(os.getenv("SHARD_EMOJI_ANCIENT", "")),
            "void": self._parse_partial_emoji(os.getenv("SHARD_EMOJI_VOID", "")),
            "sacred": self._parse_partial_emoji(os.getenv("SHARD_EMOJI_SACRED", "")),
            "primal": self._parse_partial_emoji(os.getenv("SHARD_EMOJI_PRIMAL", "")),
        }

    def _parse_partial_emoji(self, value: str | None) -> discord.PartialEmoji | None:
        if not value:
            return None
        try:
            emoji = PartialEmoji.from_str(value)
            if emoji.id:
                return emoji
        except Exception:
            if not self._emoji_warning_emitted:
                log.warning("Invalid shard emoji config: %r", value)
                self._emoji_warning_emitted = True
        return None

    def _emoji_tag_value(self, key: str) -> str:
        raw = self._emoji_tags.get(key, "")
        parsed = self._parse_partial_emoji(raw)
        if parsed and parsed.name:
            return parsed.name
        return str(raw).strip(": ") or key

    def _author_meta(self, tab: str, username: str) -> tuple[str, str]:
        if tab in SHARD_KINDS:
            label = SHARD_KINDS[tab].label
            return (f"{label} Shards | {username}", tab)
        if tab == "last_pulls":
            return (f"Last Pulls & Mercy Info — C1C | {username}", "overview")
        return (f"Shard Overview — C1C | {username}", "overview")

    @staticmethod
    def _tab_color(tab: str) -> discord.Colour:
        from modules.community.shard_tracker.views import _TAB_COLORS

        return _TAB_COLORS.get(tab, _TAB_COLORS["overview"])

    def _author_icon_url(self, guild: discord.Guild | None, emoji_key: str) -> str | None:
        tag = self._emoji_tag_value(emoji_key)
        if not tag:
            return None
        return emoji_pipeline.padded_emoji_url(guild, tag)

    def _resolve_kind(self, value: str | None) -> ShardKind | None:
        key = self._resolve_kind_key(value)
        if key in SHARD_KINDS:
            return SHARD_KINDS[key]
        return None

    def _resolve_kind_key(self, value: str | None) -> str:
        if not value:
            return ""
        normalized = value.strip().lower()
        return _TYPE_ALIASES.get(normalized, normalized)

    def _invalid_type_message(self) -> str:
        options = ", ".join(sorted(kind.label.lower() for kind in SHARD_KINDS.values()))
        return f"Unknown shard type. Choose from: {options}."

    def _channel_error_message(self, channel_id: int) -> str:
        return f"Shard & Mercy tracking is only available in <#{channel_id}>."

    async def _handle_config_failure(
        self, ctx: commands.Context, exc: Exception
    ) -> None:
        await ctx.reply(
            "Shard tracker is misconfigured. Please contact an admin.",
            mention_author=False,
        )
        await self._notify_admins(str(exc))

    def _config_error_message(self, reason: str) -> str:
        return f"Shard tracker misconfigured: {reason}. Please contact an admin."

    async def _notify_admins(self, detail: str) -> None:
        mention = self._admin_mention()
        await runtime.send_log_message(f"❌ {mention} Shard tracker error: {detail}")

    def _admin_mention(self) -> str:
        roles = sorted(get_admin_role_ids())
        if roles:
            return f"<@&{roles[0]}>"
        return "@Administrator"

    async def _log_action(
        self,
        action: str,
        user: discord.abc.User,
        channel: discord.abc.Messageable,
        detail: str,
    ) -> None:
        guild = getattr(channel, "guild", None)
        log.info(
            "shard action",
            extra={
                "action": action,
                "user": getattr(user, "id", None),
                "detail": detail,
                "channel": getattr(channel, "id", None),
            },
        )
        await runtime.send_log_message(
            f"📘 Shards — {user_label(guild, getattr(user, 'id', None))} • {detail}"
        )

    def _user_lock(self, user_id: int) -> asyncio.Lock:
        lock = self._locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[user_id] = lock
        return lock

    def _now_iso(self) -> str:
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class _ShardClanChoiceView(discord.ui.View):
    def __init__(
        self,
        *,
        controller: ShardTracker,
        clans: list[ShardClanRow],
        action: Literal["share", "opt_in", "opt_out"],
    ) -> None:
        super().__init__(timeout=120)
        options = [discord.SelectOption(label=row.clan_key[:100], value=row.clan_key[:100]) for row in clans[:25]]
        self.add_item(_ShardClanChoiceSelect(controller=controller, action=action, options=options))


class _ShardClanChoiceSelect(discord.ui.Select[_ShardClanChoiceView]):
    def __init__(
        self,
        *,
        controller: ShardTracker,
        action: Literal["share", "opt_in", "opt_out"],
        options: list[discord.SelectOption],
    ) -> None:
        super().__init__(placeholder="Select clan", min_values=1, max_values=1, options=options)
        self.controller = controller
        self.action = action

    async def callback(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        clan_key = str(self.values[0] if self.values else "").strip()
        if not clan_key:
            await interaction.response.send_message("Select a clan first.", ephemeral=True)
            return
        await self.controller.handle_clan_choice(
            interaction=interaction,
            action=self.action,
            clan_key=clan_key,
        )


class _NumberModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        title: str,
        label: str,
        placeholder: str,
    ) -> None:
        super().__init__(title=title)
        self.amount = discord.ui.TextInput(
            label=label,
            min_length=1,
            max_length=6,
            required=True,
            placeholder=placeholder,
        )
        self.add_item(self.amount)

    def _parsed_amount(self) -> int | None:
        try:
            return int(str(self.amount.value))
        except (TypeError, ValueError):
            return None


class _StashModal(_NumberModal):
    def __init__(
        self,
        *,
        controller: ShardTracker,
        owner_id: int,
        shard_key: str,
        active_tab: str,
    ) -> None:
        super().__init__(
            title="Add to Stash",
            label="How many shards are you adding to your stash?",
            placeholder="e.g., 10",
        )
        self.controller = controller
        self.owner_id = owner_id
        self.shard_key = shard_key
        self.active_tab = active_tab

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the tracker owner can log pulls.", ephemeral=True
            )
            return
        amount = self._parsed_amount()
        if amount is None:
            await interaction.response.send_message(
                "Please provide a numeric value.", ephemeral=True
            )
            return
        await self.controller.process_stash_modal(
            interaction=interaction,
            shard_key=self.shard_key,
            amount=amount,
            active_tab=self.active_tab,
        )


class _PullsModal(_NumberModal):
    def __init__(
        self,
        *,
        controller: ShardTracker,
        owner_id: int,
        shard_key: str,
        active_tab: str,
    ) -> None:
        super().__init__(
            title="Log pulls",
            label="How many shards did you pull?",
            placeholder="e.g., 5",
        )
        self.controller = controller
        self.owner_id = owner_id
        self.shard_key = shard_key
        self.active_tab = active_tab

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the tracker owner can log pulls.", ephemeral=True
            )
            return
        amount = self._parsed_amount()
        if amount is None:
            await interaction.response.send_message(
                "Please provide a numeric value.", ephemeral=True
            )
            return
        await self.controller.process_pulls_modal(
            interaction=interaction,
            shard_key=self.shard_key,
            amount=amount,
            active_tab=self.active_tab,
        )


class _LegendaryModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        controller: ShardTracker,
        owner_id: int,
        shard_key: str,
        active_tab: str,
    ) -> None:
        super().__init__(title="Log champion pull")
        self.controller = controller
        self.owner_id = owner_id
        self.shard_key = shard_key
        self.active_tab = active_tab
        self.total_pulls = discord.ui.TextInput(
            label="How many shards did you pull?",
            min_length=1,
            max_length=6,
            required=True,
            placeholder="e.g., 10",
        )
        self.after_champion = discord.ui.TextInput(
            label="How many shards after the champion?",
            min_length=1,
            max_length=6,
            required=True,
            placeholder="e.g., 2",
        )
        self.add_item(self.total_pulls)
        self.add_item(self.after_champion)

    def _parse_field(self, field: discord.ui.TextInput) -> int | None:
        try:
            return int(str(field.value))
        except (TypeError, ValueError):
            return None

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the tracker owner can log pulls.", ephemeral=True
            )
            return
        total = self._parse_field(self.total_pulls)
        after = self._parse_field(self.after_champion)
        if total is None or after is None:
            await interaction.response.send_message(
                "Please provide numeric values.", ephemeral=True
            )
            return

        await self.controller.process_legendary_modal(
            interaction=interaction,
            shard_key=self.shard_key,
            total_pulls=total,
            after_champion=after,
            active_tab=self.active_tab,
        )


class _LastPullsModal(discord.ui.Modal):
    def __init__(
        self,
        *,
        controller: ShardTracker,
        owner_id: int,
        shard_key: str,
        active_tab: str,
        legendary_mercy: int,
        mythical_mercy: int,
    ) -> None:
        super().__init__(title="Edit Last Pulls / Mercy")
        self.controller = controller
        self.owner_id = owner_id
        self.shard_key = shard_key
        self.active_tab = active_tab

        self.legendary_mercy = discord.ui.TextInput(
            label="Legendary mercy after last pull",
            min_length=1,
            max_length=6,
            required=True,
            placeholder="e.g., 10",
            default=str(max(0, legendary_mercy)),
        )
        self.add_item(self.legendary_mercy)

        self.mythical_mercy: discord.ui.TextInput | None = None
        if shard_key == "primal":
            self.mythical_mercy = discord.ui.TextInput(
                label="Mythical mercy after last pull",
                min_length=1,
                max_length=6,
                required=True,
                placeholder="e.g., 5",
                default=str(max(0, mythical_mercy)),
            )
            self.add_item(self.mythical_mercy)

    def _parse_field(self, field: discord.ui.TextInput) -> int | None:
        try:
            return int(str(field.value))
        except (TypeError, ValueError):
            return None

    async def on_submit(self, interaction: discord.Interaction) -> None:  # type: ignore[override]
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the tracker owner can edit mercy counters.", ephemeral=True
            )
            return

        legendary_value = self._parse_field(self.legendary_mercy)
        mythical_value: int | None = None
        if self.mythical_mercy is not None:
            mythical_value = self._parse_field(self.mythical_mercy)

        if legendary_value is None or (
            self.mythical_mercy is not None and mythical_value is None
        ):
            await interaction.response.send_message(
                "Please provide numeric values.", ephemeral=True
            )
            return

        await self.controller.process_last_pulls_modal(
            interaction=interaction,
            shard_key=self.shard_key,
            active_tab=self.active_tab,
            legendary_mercy=legendary_value,
            mythical_mercy=mythical_value,
        )


class _PrimalDropChoiceView(discord.ui.View):
    def __init__(
        self,
        *,
        controller: ShardTracker,
        owner_id: int,
        active_tab: str,
        panel_message: discord.Message | None,
        after_champion: int,
        total_pulls: int,
        legendary_mercy: int,
        mythical_mercy: int,
    ) -> None:
        super().__init__(timeout=120)
        self.controller = controller
        self.owner_id = owner_id
        self.active_tab = active_tab
        self.panel_message = panel_message
        self.after_champion = after_champion
        self.total_pulls = total_pulls
        self.legendary_mercy = legendary_mercy
        self.mythical_mercy = mythical_mercy

    async def interaction_check(self, interaction: discord.Interaction) -> bool:  # type: ignore[override]
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Only the tracker owner can log pulls.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Legendary", style=discord.ButtonStyle.primary)
    async def handle_legendary(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.controller.process_primal_choice(
            interaction=interaction,
            choice="legendary",
            active_tab=self.active_tab,
            panel_message=self.panel_message,
            after_champion=self.after_champion,
            total_pulls=self.total_pulls,
            legendary_mercy=self.legendary_mercy,
            mythical_mercy=self.mythical_mercy,
        )

    @discord.ui.button(label="Mythical", style=discord.ButtonStyle.success)
    async def handle_mythical(
        self, interaction: discord.Interaction, _: discord.ui.Button
    ) -> None:
        await self.controller.process_primal_choice(
            interaction=interaction,
            choice="mythical",
            active_tab=self.active_tab,
            panel_message=self.panel_message,
            after_champion=self.after_champion,
            total_pulls=self.total_pulls,
            legendary_mercy=self.legendary_mercy,
            mythical_mercy=self.mythical_mercy,
        )
