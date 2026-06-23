from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import discord
from discord.ext import commands

from modules.community.shard_tracker import setup as shard_setup
from modules.community.shard_tracker.cog import ShardTracker
from modules.community.shard_tracker.data import ShardTrackerConfig


def test_resolve_kind_aliases():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    tracker = ShardTracker(bot)

    assert tracker._resolve_kind_key("Anc") == "ancient"
    assert tracker._resolve_kind("primals").key == "primal"
    assert tracker._resolve_kind("unknown") is None


def test_resolve_thread_rejects_wrong_channel(fake_discord_env):
    async def runner():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        tracker = ShardTracker(bot)
        config = ShardTrackerConfig(sheet_id="s", tab_name="t", channel_id=999)
        tracker.store.get_config = AsyncMock(return_value=config)

        guild = fake_discord_env.Guild()
        channel = fake_discord_env.TextChannel(channel_id=555, guild=guild)
        ctx = fake_discord_env.Context(fake_discord_env.User(42), channel)

        allowed, parent, thread = await tracker._resolve_thread(ctx)

        assert not allowed
        assert parent is None
        assert thread is None
        assert "Shard & Mercy tracking is only available" in ctx.replies[-1]

    asyncio.run(runner())


def test_resolve_thread_creates_and_reuses(fake_discord_env):
    async def runner():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        tracker = ShardTracker(bot)
        config = ShardTrackerConfig(sheet_id="s", tab_name="t", channel_id=444)
        tracker.store.get_config = AsyncMock(return_value=config)

        bot_stub = fake_discord_env.Bot()
        guild = fake_discord_env.Guild()
        guild.bot = bot_stub
        channel = fake_discord_env.TextChannel(channel_id=444, guild=guild, bot=bot_stub)
        user = fake_discord_env.User(55)
        ctx = fake_discord_env.Context(user, channel)

        allowed, parent, thread = await tracker._resolve_thread(ctx)
        assert allowed and thread is not None
        assert channel.created_names and len(channel.created_names) == 1

        allowed_again, _, thread_again = await tracker._resolve_thread(ctx)
        assert allowed_again
        assert thread_again is thread
        assert len(channel.created_names) == 1, "Thread should be reused"

    asyncio.run(runner())


def test_resolve_thread_rejects_foreign_thread(fake_discord_env):
    async def runner():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        tracker = ShardTracker(bot)
        config = ShardTrackerConfig(sheet_id="s", tab_name="t", channel_id=222)
        tracker.store.get_config = AsyncMock(return_value=config)

        bot_stub = fake_discord_env.Bot()
        guild = fake_discord_env.Guild()
        guild.bot = bot_stub
        channel = fake_discord_env.TextChannel(channel_id=222, guild=guild, bot=bot_stub)
        first_user = fake_discord_env.User(70)
        first_ctx = fake_discord_env.Context(first_user, channel)
        allowed, _, thread = await tracker._resolve_thread(first_ctx)
        assert allowed and thread is not None

        other_user = fake_discord_env.User(71)
        thread_ctx = fake_discord_env.Context(other_user, thread)
        allowed_second, _, thread_second = await tracker._resolve_thread(thread_ctx)

        assert not allowed_second
        assert thread_second is None
        assert "Please use your own shard thread" in thread_ctx.replies[-1]

    asyncio.run(runner())


def test_commands_are_registered():
    async def runner():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        await shard_setup(bot)
        assert bot.get_command("shards") is not None
        assert bot.get_command("mercy") is None

    asyncio.run(runner())


def test_legendary_reset_tracks_depth():
    tracker = ShardTracker(commands.Bot(command_prefix="!", intents=discord.Intents.none()))
    record = tracker.store._new_record([], 1, "user")  # type: ignore[arg-type]
    kind = tracker._resolve_kind("ancient")
    record.ancients_since_lego = 7

    tracker._apply_legendary_reset(record, kind)  # type: ignore[arg-type]

    assert record.ancients_since_lego == 0
    assert record.last_ancient_lego_depth == 7


def test_logged_mythic_resets_counters():
    tracker = ShardTracker(commands.Bot(command_prefix="!", intents=discord.Intents.none()))
    record = tracker.store._new_record([], 2, "user")  # type: ignore[arg-type]
    record.primals_since_mythic = 50

    tracker._apply_primal_mythical(record, depth=record.primals_since_mythic)  # type: ignore[arg-type]

    assert record.primals_since_mythic == 50
    assert record.primals_since_lego == 0
    assert record.last_primal_mythic_depth == 50


def test_manual_mercy_sets_primal_independently():
    tracker = ShardTracker(commands.Bot(command_prefix="!", intents=discord.Intents.none()))
    record = tracker.store._new_record([], 3, "user")  # type: ignore[arg-type]
    kind = tracker._resolve_kind("primal")

    tracker._apply_manual_mercy(  # type: ignore[arg-type]
        record, kind, legendary_mercy=12, mythical_mercy=7
    )

    assert record.primals_since_lego == 12
    assert record.primals_since_mythic == 7


def test_manual_mercy_sets_non_primal_counter():
    tracker = ShardTracker(commands.Bot(command_prefix="!", intents=discord.Intents.none()))
    record = tracker.store._new_record([], 4, "user")  # type: ignore[arg-type]
    kind = tracker._resolve_kind("sacred")

    tracker._apply_manual_mercy(  # type: ignore[arg-type]
        record, kind, legendary_mercy=5, mythical_mercy=None
    )

    assert record.sacreds_since_lego == 5


def _share_clan(**overrides):
    from modules.community.shard_tracker.data import ShardClanRow

    values = dict(
        clan_key="alpha",
        enabled=True,
        share_channel_id=123,
        share_thread_id=None,
        reminder_enabled=False,
        opt_in_role_id=None,
        reminder_day="",
        reminder_time_utc="",
        title="",
        body="",
        footer="",
        color_hex="",
        emoji_name_or_id="",
    )
    values.update(overrides)
    return ShardClanRow(**values)


class _ShareResponse:
    def __init__(self):
        self.deferred = False
        self.messages = []

    def is_done(self):
        return self.deferred or bool(self.messages)

    async def defer(self, *, ephemeral=False):
        self.deferred = True

    async def send_message(self, message, *, ephemeral=False):
        self.messages.append((message, ephemeral))


class _ShareFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, message, *, ephemeral=False, view=None):
        self.messages.append((message, ephemeral, view))


class _ShareDestination:
    id = 123
    mention = "<#123>"
    guild = None

    def __init__(self):
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)


class _ShareInteraction:
    guild = object()
    channel = type("Channel", (), {"id": 999})()

    def __init__(self):
        self.user = type("User", (), {"id": 42, "display_name": "Tester", "name": "Tester"})()
        self.response = _ShareResponse()
        self.followup = _ShareFollowup()


def test_share_summary_sends_to_resolved_destination():
    async def runner():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        tracker = ShardTracker(bot)
        clan = _share_clan()
        destination = _ShareDestination()
        tracker.store.get_enabled_clans = AsyncMock(return_value=[clan])
        tracker._resolve_share_destination = lambda _clan: (destination, None)
        tracker._share_destination_block_reason = lambda _interaction, _destination: None
        tracker._notify_admins = AsyncMock()
        record = tracker.store._new_record([], 42, "Tester")  # type: ignore[arg-type]
        interaction = _ShareInteraction()

        await tracker._handle_share_summary_action(
            interaction=interaction,
            record=record,
            default_clan_key=None,
        )

        assert len(destination.sent) == 1
        assert interaction.followup.messages[-1][1] is True
        assert "Shared your shard summary" in interaction.followup.messages[-1][0]

    asyncio.run(runner())


def test_share_summary_missing_destination_is_ephemeral_warning(caplog):
    async def runner():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        tracker = ShardTracker(bot)
        clan = _share_clan(share_channel_id=None, share_thread_id=None)
        tracker.store.get_enabled_clans = AsyncMock(return_value=[clan])
        tracker._notify_admins = AsyncMock()
        record = tracker.store._new_record([], 42, "Tester")  # type: ignore[arg-type]
        interaction = _ShareInteraction()

        with caplog.at_level(logging.WARNING, logger="c1c.shards.cog"):
            await tracker._handle_share_summary_action(
                interaction=interaction,
                record=record,
                default_clan_key=None,
            )

        assert interaction.followup.messages[-1][1] is True
        assert "does not have a share destination configured" in interaction.followup.messages[-1][0]
        assert "shard share blocked" in caplog.text
        assert any(getattr(record, "component", None) == "shard_tracker.share_to_clan" for record in caplog.records)

    asyncio.run(runner())
