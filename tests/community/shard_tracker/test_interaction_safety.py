from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from modules.community.shard_tracker import ShardTracker
from modules.community.shard_tracker.data import (
    EXPECTED_HEADERS,
    ShardClanRow,
    ShardRecord,
)


def run(awaitable):
    return asyncio.run(awaitable)


class _Response:
    def __init__(self) -> None:
        self._done = False
        self.defer = AsyncMock(side_effect=self._mark_done)
        self.send_message = AsyncMock(side_effect=self._mark_done)
        self.send_modal = AsyncMock(side_effect=self._mark_done)
        self.edit_message = AsyncMock(side_effect=self._mark_done)

    def is_done(self) -> bool:
        return self._done

    async def _mark_done(self, *_args, **_kwargs) -> None:
        self._done = True


def _interaction():
    user = SimpleNamespace(id=10, display_name="Tester", name="Tester")
    guild = SimpleNamespace(id=1)
    return SimpleNamespace(
        user=user,
        guild=guild,
        channel=SimpleNamespace(guild=guild),
        message=SimpleNamespace(channel=SimpleNamespace(guild=guild), edit=AsyncMock()),
        response=_Response(),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def _record(**overrides):
    values = dict(
        header=EXPECTED_HEADERS,
        discord_id=10,
        username_snapshot="Tester",
        ancients_owned=20,
        ancients_since_lego=7,
        primals_since_mythic=9,
    )
    values.update(overrides)
    return ShardRecord(**values)


def _tracker(store=None):
    tracker = object.__new__(ShardTracker)
    tracker.bot = SimpleNamespace()
    tracker.store = store or SimpleNamespace()
    tracker._locks = {}
    tracker._panel_record_snapshots = {}
    tracker._tab_emojis = {}
    tracker._emoji_tags = {}
    tracker._emoji_warning_emitted = False
    tracker._feature_enabled = lambda: True
    tracker._notify_admins = AsyncMock()
    tracker._log_action = AsyncMock()
    return tracker


def test_modal_launch_does_not_wait_for_sheets():
    interaction = _interaction()
    store = SimpleNamespace(
        get_config=AsyncMock(side_effect=AssertionError("must not read Sheets")),
        load_record=AsyncMock(side_effect=AssertionError("must not read Sheets")),
    )
    tracker = _tracker(store)

    run(
        tracker.handle_button_interaction(
            interaction=interaction,
            custom_id="action:stash:ancient",
            active_tab="ancient",
        )
    )

    interaction.response.send_modal.assert_awaited_once()
    store.get_config.assert_not_awaited()
    store.load_record.assert_not_awaited()


def test_tab_change_defers_before_record_read():
    interaction = _interaction()
    record = _record()

    async def get_config():
        assert interaction.response.is_done() is True
        return object()

    async def load_record(_user_id, _display_name):
        assert interaction.response.is_done() is True
        return record

    store = SimpleNamespace(get_config=AsyncMock(side_effect=get_config), load_record=AsyncMock(side_effect=load_record))
    tracker = _tracker(store)
    view = discord.ui.View()
    tracker._build_panel = Mock(return_value=(discord.Embed(title="Ancient"), view))

    run(
        tracker.handle_button_interaction(
            interaction=interaction,
            custom_id="tab:ancient",
            active_tab="overview",
        )
    )

    interaction.response.defer.assert_awaited_once_with()
    interaction.edit_original_response.assert_awaited_once()
    store.get_config.assert_awaited_once()
    store.load_record.assert_awaited_once()


def test_last_pulls_modal_uses_panel_snapshot_without_sheet_read():
    interaction = _interaction()
    store = SimpleNamespace(
        get_config=AsyncMock(side_effect=AssertionError("must not read Sheets")),
        load_record=AsyncMock(side_effect=AssertionError("must not read Sheets")),
    )
    tracker = _tracker(store)
    tracker._panel_record_snapshots[interaction.user.id] = _record(
        ancients_since_lego=13
    )

    run(
        tracker.handle_button_interaction(
            interaction=interaction,
            custom_id="action:last_pulls:ancient",
            active_tab="ancient",
        )
    )

    interaction.response.send_modal.assert_awaited_once()
    modal = interaction.response.send_modal.await_args.args[0]
    assert str(modal.legendary_mercy.default) == "13"
    store.get_config.assert_not_awaited()
    store.load_record.assert_not_awaited()


def test_stash_submit_defers_before_sheet_read_and_write():
    interaction = _interaction()
    record = _record()

    async def get_config():
        assert interaction.response.is_done() is True
        return object()

    async def load_record(_user_id, _display_name):
        assert interaction.response.is_done() is True
        return record

    async def save_record(_config, saved):
        assert interaction.response.is_done() is True
        assert saved.ancients_owned == 25

    store = SimpleNamespace(
        get_config=AsyncMock(side_effect=get_config),
        load_record=AsyncMock(side_effect=load_record),
        save_record=AsyncMock(side_effect=save_record),
    )
    tracker = _tracker(store)
    tracker._build_panel = Mock(
        return_value=(discord.Embed(title="Ancient"), discord.ui.View())
    )

    run(
        tracker.process_stash_modal(
            interaction=interaction,
            shard_key="ancient",
            amount=5,
            active_tab="ancient",
        )
    )

    interaction.response.defer.assert_awaited_once_with()
    store.save_record.assert_awaited_once()
    interaction.edit_original_response.assert_awaited_once()


def test_reminder_opt_in_defers_before_clan_read():
    interaction = _interaction()
    role = SimpleNamespace(id=77)
    member = SimpleNamespace(id=10, roles=[], add_roles=AsyncMock(), remove_roles=AsyncMock())
    interaction.guild.get_member = lambda _user_id: member
    interaction.guild.get_role = lambda _role_id: role
    clan = ShardClanRow(
        clan_key="C1C",
        enabled=True,
        share_channel_id=None,
        share_thread_id=None,
        reminder_enabled=True,
        opt_in_role_id=77,
        reminder_day="friday",
        reminder_time_utc="12:00",
        title="title",
        body="body",
        footer="footer",
        color_hex="#000000",
        emoji_name_or_id="",
    )

    async def get_enabled_clans():
        assert interaction.response.is_done() is True
        return [clan]

    tracker = _tracker(
        SimpleNamespace(get_enabled_clans=AsyncMock(side_effect=get_enabled_clans))
    )

    run(
        tracker.handle_reminder_opt_action(
            interaction=interaction,
            action="in",
            clan_key="C1C",
        )
    )

    interaction.response.defer.assert_awaited_once_with(
        ephemeral=True, thinking=True
    )
    member.add_roles.assert_awaited_once_with(
        role, reason="Shard reminder opt-in (C1C)"
    )
