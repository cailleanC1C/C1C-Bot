"""Interaction-safe runtime wrapper for the Shard & Mercy tracker.

The existing cog remains the source of truth for tracker business rules and Sheet
persistence. This adapter only changes the Discord interaction boundary: slow I/O
is acknowledged first, and the base cog's legacy response calls are translated to
followups/original-response edits after deferral.
"""

from __future__ import annotations

from copy import deepcopy

import discord

from modules.community.shard_tracker.cog import (
    SHARD_KINDS,
    ShardTracker as _BaseShardTracker,
    _LegendaryModal,
    _LastPullsModal,
    _PullsModal,
    _StashModal,
)
from modules.community.shard_tracker.data import ShardRecord


class _DeferredResponse:
    """Translate initial-response calls after a real interaction was deferred."""

    def __init__(self, interaction) -> None:
        self._interaction = interaction

    def is_done(self) -> bool:
        checker = getattr(self._interaction.response, "is_done", None)
        return bool(checker()) if callable(checker) else False

    async def defer(self, *args, **kwargs):
        if not self.is_done():
            return await self._interaction.response.defer(*args, **kwargs)
        return None

    async def send_message(self, *args, **kwargs):
        return await self._interaction.followup.send(*args, **kwargs)

    async def edit_message(self, **kwargs):
        return await self._interaction.edit_original_response(**kwargs)

    async def send_modal(self, modal):
        return await self._interaction.response.send_modal(modal)


class _DeferredInteraction:
    """Delegate an interaction while exposing deferred-safe response semantics."""

    def __init__(self, interaction) -> None:
        self._interaction = interaction
        self.response = _DeferredResponse(interaction)

    def __getattr__(self, name):
        return getattr(self._interaction, name)


class ShardTracker(_BaseShardTracker):
    """Runtime ShardTracker that acknowledges before potentially slow I/O."""

    def __init__(self, bot) -> None:
        super().__init__(bot)
        self._panel_record_snapshots: dict[int, ShardRecord] = {}

    @staticmethod
    def _response_done(interaction) -> bool:
        checker = getattr(interaction.response, "is_done", None)
        return bool(checker()) if callable(checker) else False

    async def _defer_update(self, interaction) -> None:
        if not self._response_done(interaction):
            await interaction.response.defer()

    async def _defer_ephemeral(self, interaction) -> None:
        if not self._response_done(interaction):
            await interaction.response.defer(ephemeral=True, thinking=True)

    @staticmethod
    def _deferred(interaction):
        if isinstance(interaction, _DeferredInteraction):
            return interaction
        return _DeferredInteraction(interaction)

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

        # Modal launch must itself be the initial interaction response. These
        # actions do not need a fresh Sheet read just to show their form.
        if action_name == "stash":
            await interaction.response.send_modal(
                _StashModal(
                    controller=self,
                    owner_id=interaction.user.id,
                    shard_key=tab,
                    active_tab=tab,
                )
            )
            return
        if action_name == "pulls":
            await interaction.response.send_modal(
                _PullsModal(
                    controller=self,
                    owner_id=interaction.user.id,
                    shard_key=tab,
                    active_tab=tab,
                )
            )
            return
        if action_name == "legendary" or (
            action_name == "mythical" and tab == "remnant"
        ):
            await interaction.response.send_modal(
                _LegendaryModal(
                    controller=self,
                    owner_id=interaction.user.id,
                    shard_key=tab,
                    active_tab=tab,
                )
            )
            return
        if action_name == "last_pulls":
            kind = self._resolve_kind(tab)
            snapshot = self._panel_record_snapshots.get(interaction.user.id)
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
                    owner_id=interaction.user.id,
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

        if action_name == "share":
            await self._defer_ephemeral(interaction)
        else:
            await self._defer_update(interaction)
        await super().handle_button_interaction(
            interaction=self._deferred(interaction),
            custom_id=custom_id,
            active_tab=active_tab,
        )

    async def process_stash_modal(self, *, interaction, **kwargs) -> None:
        await self._defer_update(interaction)
        await super().process_stash_modal(
            interaction=self._deferred(interaction), **kwargs
        )

    async def process_pulls_modal(self, *, interaction, **kwargs) -> None:
        await self._defer_update(interaction)
        await super().process_pulls_modal(
            interaction=self._deferred(interaction), **kwargs
        )

    async def process_legendary_modal(
        self, *, interaction, shard_key: str, **kwargs
    ) -> None:
        if self._resolve_kind_key(shard_key) == "primal":
            await self._defer_ephemeral(interaction)
        else:
            await self._defer_update(interaction)
        await super().process_legendary_modal(
            interaction=self._deferred(interaction),
            shard_key=shard_key,
            **kwargs,
        )

    async def process_last_pulls_modal(self, *, interaction, **kwargs) -> None:
        await self._defer_update(interaction)
        await super().process_last_pulls_modal(
            interaction=self._deferred(interaction), **kwargs
        )

    async def process_primal_choice(self, *, interaction, **kwargs) -> None:
        await self._defer_update(interaction)
        await super().process_primal_choice(
            interaction=self._deferred(interaction), **kwargs
        )

    async def _handle_share_summary_action(self, *, interaction, **kwargs) -> None:
        await self._defer_ephemeral(interaction)
        await super()._handle_share_summary_action(
            interaction=self._deferred(interaction), **kwargs
        )

    async def handle_reminder_opt_action(self, *, interaction, **kwargs) -> None:
        await self._defer_ephemeral(interaction)
        await super().handle_reminder_opt_action(
            interaction=self._deferred(interaction), **kwargs
        )

    async def handle_clan_choice(self, *, interaction, **kwargs) -> None:
        await self._defer_ephemeral(interaction)
        await super().handle_clan_choice(
            interaction=self._deferred(interaction), **kwargs
        )
