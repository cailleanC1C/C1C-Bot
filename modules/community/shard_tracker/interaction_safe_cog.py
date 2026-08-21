"""Interaction-safe runtime wrapper for the Shard & Mercy tracker.

The base tracker owns all business rules and Sheet persistence. This wrapper only
moves Discord acknowledgement ahead of potentially slow I/O and keeps a short-
lived per-user panel snapshot for modal prefills that must be available before a
modal can be opened.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Literal

import discord

from .cog import (
    SHARD_KINDS,
    ShardTracker as _BaseShardTracker,
    _LegendaryModal,
    _LastPullsModal,
    _PrimalDropChoiceView,
    _PullsModal,
    _StashModal,
)
from .data import ShardRecord, ShardTrackerConfigError, ShardTrackerSheetError


class ShardTracker(_BaseShardTracker):
    """Shard tracker with Discord interaction acknowledgement safety."""

    def __init__(self, bot) -> None:
        super().__init__(bot)
        self._panel_record_snapshots: dict[int, ShardRecord] = {}

    @staticmethod
    def _response_done(interaction: discord.Interaction) -> bool:
        is_done = getattr(interaction.response, "is_done", None)
        return bool(is_done()) if callable(is_done) else False

    async def _defer_update(self, interaction: discord.Interaction) -> None:
        if not self._response_done(interaction):
            await interaction.response.defer()

    async def _defer_ephemeral(self, interaction: discord.Interaction) -> None:
        if not self._response_done(interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)

    async def _send_ephemeral_after_ack(
        self, interaction: discord.Interaction, message: str
    ) -> None:
        if self._response_done(interaction):
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def _edit_original_panel(
        self,
        interaction: discord.Interaction,
        *,
        embed: discord.Embed,
        view: discord.ui.View,
    ) -> None:
        if self._response_done(interaction):
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)

    async def _load_record_after_ack(
        self, interaction: discord.Interaction
    ) -> ShardRecord | None:
        user = interaction.user
        try:
            await self.store.get_config()
            return await self.store.load_record(
                user.id, user.display_name or user.name
            )
        except ShardTrackerConfigError as exc:
            await self._send_ephemeral_after_ack(
                interaction, self._config_error_message(str(exc))
            )
            await self._notify_admins(str(exc))
        except ShardTrackerSheetError as exc:
            await self._send_ephemeral_after_ack(
                interaction,
                "Shard tracker sheet misconfigured. Please contact an admin.",
            )
            await self._notify_admins(str(exc))
        return None

    def _build_panel(self, member, record, channel, active_tab):
        result = super()._build_panel(member, record, channel, active_tab)
        user_id = getattr(member, "id", 0)
        if user_id:
            self._panel_record_snapshots[user_id] = deepcopy(record)
        return result

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

        user = interaction.user
        if getattr(interaction.guild, "id", None) is None:
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

        action_name, action_tab = action
        tab = action_tab or active_tab or "overview"
        if tab not in SHARD_KINDS and action_name in {
            "stash",
            "pulls",
            "legendary",
            "mythical",
            "last_pulls",
        }:
            await interaction.response.send_message(
                "Pick a shard tab to use these buttons.", ephemeral=True
            )
            return

        # Modal launch actions must be the initial interaction response. They do
        # not need a fresh Sheet read merely to open the modal.
        if action_name == "stash":
            await interaction.response.send_modal(
                _StashModal(
                    controller=self,
                    owner_id=user.id,
                    shard_key=tab,
                    active_tab=tab,
                )
            )
            return
        if action_name == "pulls":
            await interaction.response.send_modal(
                _PullsModal(
                    controller=self,
                    owner_id=user.id,
                    shard_key=tab,
                    active_tab=tab,
                )
            )
            return
        if action_name == "mythical" and tab == "remnant":
            await interaction.response.send_modal(
                _LegendaryModal(
                    controller=self,
                    owner_id=user.id,
                    shard_key=tab,
                    active_tab=tab,
                )
            )
            return
        if action_name == "legendary":
            await interaction.response.send_modal(
                _LegendaryModal(
                    controller=self,
                    owner_id=user.id,
                    shard_key=tab,
                    active_tab=tab,
                )
            )
            return
        if action_name == "last_pulls":
            kind = self._resolve_kind(tab)
            snapshot = self._panel_record_snapshots.get(user.id)
            if kind is None:
                await interaction.response.send_message(
                    "Unknown shard type.", ephemeral=True
                )
                return
            if snapshot is None:
                await interaction.response.send_message(
                    "This tracker panel needs refreshing before mercy can be edited. Use `!shards` to open a fresh panel.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_modal(
                _LastPullsModal(
                    controller=self,
                    owner_id=user.id,
                    shard_key=tab,
                    active_tab=tab,
                    legendary_mercy=(
                        max(0, getattr(snapshot, kind.mercy_field, 0))
                        if kind.mercy_field
                        else 0
                    ),
                    mythical_mercy=max(0, snapshot.primals_since_mythic),
                )
            )
            return

        if action_name == "tab":
            await self._defer_update(interaction)
            async with self._user_lock(user.id):
                record = await self._load_record_after_ack(interaction)
            if record is None:
                return
            embed, view = self._build_panel(
                user, record, interaction.channel, action_tab or "overview"
            )
            await self._edit_original_panel(
                interaction, embed=embed, view=view
            )
            return

        if action_name == "share":
            await self._defer_ephemeral(interaction)
            async with self._user_lock(user.id):
                record = await self._load_record_after_ack(interaction)
            if record is None:
                return
            await self._handle_share_summary_action(
                interaction=interaction,
                record=record,
                default_clan_key=None,
            )

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

        await self._defer_update(interaction)
        async with self._user_lock(interaction.user.id):
            try:
                config = await self.store.get_config()
                record = await self.store.load_record(
                    interaction.user.id,
                    interaction.user.display_name or interaction.user.name,
                )
                self._apply_stash_increase(record, kind, amount)
                record.snapshot_name(
                    interaction.user.display_name or interaction.user.name
                )
                await self.store.save_record(config, record)
            except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
                await self._send_ephemeral_after_ack(
                    interaction, self._config_error_message(str(exc))
                )
                await self._notify_admins(str(exc))
                return

        embed, view = self._build_panel(
            interaction.user, record, interaction.channel, active_tab
        )
        await self._edit_original_panel(interaction, embed=embed, view=view)
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

        await self._defer_update(interaction)
        async with self._user_lock(interaction.user.id):
            try:
                config = await self.store.get_config()
                record = await self.store.load_record(
                    interaction.user.id,
                    interaction.user.display_name or interaction.user.name,
                )
                ok, message = self._apply_pull_usage(record, kind, amount)
                if not ok:
                    await self._send_ephemeral_after_ack(
                        interaction, message or "Unable to log pulls."
                    )
                    return
                record.snapshot_name(
                    interaction.user.display_name or interaction.user.name
                )
                await self.store.save_record(config, record)
            except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
                await self._send_ephemeral_after_ack(
                    interaction, self._config_error_message(str(exc))
                )
                await self._notify_admins(str(exc))
                return

        embed, view = self._build_panel(
            interaction.user, record, interaction.channel, active_tab
        )
        await self._edit_original_panel(interaction, embed=embed, view=view)
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

        panel_message = interaction.message
        if kind.key == "primal":
            await self._defer_ephemeral(interaction)
        else:
            await self._defer_update(interaction)

        async with self._user_lock(interaction.user.id):
            try:
                config = await self.store.get_config()
                record = await self.store.load_record(
                    interaction.user.id,
                    interaction.user.display_name or interaction.user.name,
                )
                legendary_before = max(0, record.primals_since_lego)
                mythical_before = max(0, record.primals_since_mythic)
                ok, message = self._apply_pull_usage(record, kind, total_pulls)
                if not ok:
                    await self._send_ephemeral_after_ack(
                        interaction, message or "Unable to log pulls."
                    )
                    return

                if kind.key == "remnant":
                    depth = max(0, record.remnants_since_mythic - after_champion)
                    record.last_remnant_mythic_depth = depth
                    record.last_remnant_mythic_iso = self._now_iso()
                    record.remnants_since_mythic = max(0, after_champion)
                    record.snapshot_name(
                        interaction.user.display_name or interaction.user.name
                    )
                    await self.store.save_record(config, record)
                else:
                    current_mercy = max(0, getattr(record, kind.mercy_field, 0))
                    drop_depth = max(0, current_mercy - after_champion)
                    setattr(record, kind.mercy_field, drop_depth)

                    if kind.key == "primal":
                        record.snapshot_name(
                            interaction.user.display_name or interaction.user.name
                        )
                        await self.store.save_record(config, record)
                    else:
                        self._apply_legendary_reset(record, kind)
                        setattr(record, kind.mercy_field, after_champion)
                        record.snapshot_name(
                            interaction.user.display_name or interaction.user.name
                        )
                        await self.store.save_record(config, record)
            except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
                await self._send_ephemeral_after_ack(
                    interaction, self._config_error_message(str(exc))
                )
                await self._notify_admins(str(exc))
                return

        if kind.key == "primal":
            await interaction.followup.send(
                "What did you pull?",
                view=_PrimalDropChoiceView(
                    controller=self,
                    owner_id=getattr(interaction.user, "id", 0),
                    active_tab=active_tab,
                    panel_message=panel_message,
                    after_champion=after_champion,
                    total_pulls=total_pulls,
                    legendary_mercy=legendary_before,
                    mythical_mercy=mythical_before,
                ),
                ephemeral=True,
            )
            return

        embed, view = self._build_panel(
            interaction.user, record, interaction.channel, active_tab
        )
        await self._edit_original_panel(interaction, embed=embed, view=view)
        await self._log_action(
            "remnant_mythical_reset" if kind.key == "remnant" else "legendary_reset",
            interaction.user,
            interaction.channel,
            (
                f"Remnant Mythical: summons={total_pulls}, after={after_champion}"
                if kind.key == "remnant"
                else f"{kind.label} drop: pulls={total_pulls}, after={after_champion}"
            ),
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
        if legendary_mercy < 0 or (
            mythical_mercy is not None and mythical_mercy < 0
        ):
            await interaction.response.send_message(
                "Please provide non-negative numbers.", ephemeral=True
            )
            return

        await self._defer_update(interaction)
        async with self._user_lock(interaction.user.id):
            try:
                config = await self.store.get_config()
                record = await self.store.load_record(
                    interaction.user.id,
                    interaction.user.display_name or interaction.user.name,
                )
                self._apply_manual_mercy(
                    record,
                    kind,
                    legendary_mercy=legendary_mercy,
                    mythical_mercy=mythical_mercy,
                )
                record.snapshot_name(
                    interaction.user.display_name or interaction.user.name
                )
                await self.store.save_record(config, record)
            except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
                await self._send_ephemeral_after_ack(
                    interaction, self._config_error_message(str(exc))
                )
                await self._notify_admins(str(exc))
                return

        embed, view = self._build_panel(
            interaction.user, record, interaction.channel, active_tab
        )
        await self._edit_original_panel(interaction, embed=embed, view=view)
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

        await self._defer_update(interaction)
        async with self._user_lock(interaction.user.id):
            try:
                config = await self.store.get_config()
                record = await self.store.load_record(
                    interaction.user.id,
                    interaction.user.display_name or interaction.user.name,
                )
                before_champion = max(0, total_pulls - after_champion)
                legendary_before = max(0, legendary_mercy)
                mythical_before = max(0, mythical_mercy)

                if choice == "legendary":
                    record.primals_since_lego = legendary_before + before_champion
                    self._apply_primal_legendary(record)
                    record.primals_since_lego = max(0, after_champion)
                    record.primals_since_mythic = (
                        mythical_before + max(0, total_pulls)
                    )
                else:
                    depth_mythical = mythical_before + before_champion
                    self._apply_primal_mythical(record, depth=depth_mythical)
                    record.primals_since_mythic = max(0, after_champion)
                    record.primals_since_lego = (
                        legendary_before + max(0, total_pulls)
                    )
                record.snapshot_name(
                    interaction.user.display_name or interaction.user.name
                )
                await self.store.save_record(config, record)
            except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
                await self._send_ephemeral_after_ack(
                    interaction, self._config_error_message(str(exc))
                )
                await self._notify_admins(str(exc))
                return

        target_message = panel_message or interaction.message
        if target_message:
            embed, view = self._build_panel(
                interaction.user, record, target_message.channel, active_tab
            )
            await target_message.edit(embed=embed, view=view)
        await interaction.edit_original_response(content="Logged!", view=None)
        await self._log_action(
            "primal_drop",
            interaction.user,
            interaction.channel,
            f"Primal {choice}",
        )

    async def _handle_share_summary_action(
        self,
        *,
        interaction: discord.Interaction,
        record: ShardRecord,
        default_clan_key: str | None,
    ) -> None:
        await self._defer_ephemeral(interaction)
        await super()._handle_share_summary_action(
            interaction=interaction,
            record=record,
            default_clan_key=default_clan_key,
        )

    async def handle_reminder_opt_action(
        self,
        *,
        interaction: discord.Interaction,
        action: Literal["in", "out"],
        clan_key: str | None,
    ) -> None:
        await self._defer_ephemeral(interaction)
        try:
            clans = await self.store.get_enabled_clans()
        except (ShardTrackerConfigError, ShardTrackerSheetError) as exc:
            await self._send_ephemeral_after_ack(
                interaction, self._config_error_message(str(exc))
            )
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
            await interaction.followup.send(
                "Reminder role actions only work in a server.", ephemeral=True
            )
            return
        member = (
            interaction.user
            if isinstance(interaction.user, discord.Member)
            else guild.get_member(interaction.user.id)
        )
        if member is None:
            await interaction.followup.send(
                "Couldn’t resolve your member record right now.", ephemeral=True
            )
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
                await member.add_roles(
                    role, reason=f"Shard reminder opt-in ({selected.clan_key})"
                )
                await interaction.followup.send(
                    "Opted in for weekly shard reminders.", ephemeral=True
                )
                return
            if role not in member.roles:
                await interaction.followup.send(
                    "You’re already opted out.", ephemeral=True
                )
                return
            await member.remove_roles(
                role, reason=f"Shard reminder opt-out ({selected.clan_key})"
            )
            await interaction.followup.send(
                "Opted out from weekly shard reminders.", ephemeral=True
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I don’t have permission to update that role.", ephemeral=True
            )
        except Exception:
            self.log.exception("shard reminder role update failed") if hasattr(self, "log") else None
            await interaction.followup.send(
                "Couldn’t update your reminder role right now.", ephemeral=True
            )

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

        await self._defer_ephemeral(interaction)
        async with self._user_lock(interaction.user.id):
            record = await self._load_record_after_ack(interaction)
        if record is None:
            return
        await self._handle_share_summary_action(
            interaction=interaction,
            record=record,
            default_clan_key=clan_key,
        )
