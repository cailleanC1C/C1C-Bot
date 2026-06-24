from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock

import discord
from discord.ext import commands

from modules.community.shard_tracker import setup as shard_setup
from modules.community.shard_tracker.cog import SHARD_KINDS, ShardTracker
from modules.community.shard_tracker.data import ShardTrackerConfig
from modules.community.shard_tracker.views import FOOTER_TEXT


def test_resolve_kind_aliases():
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    tracker = ShardTracker(bot)

    assert tracker._resolve_kind_key("Anc") == "ancient"
    assert tracker._resolve_kind("primals").key == "primal"
    assert tracker._resolve_kind("unknown") is None
    assert tracker._resolve_kind_key("mysteries") == "mystery"
    assert tracker._resolve_kind_key("cursed_remnants") == "remnant"


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


def _button_labels(view):
    return [getattr(child, "label", None) for child in view.children]


def _button_custom_ids(view):
    return [getattr(child, "custom_id", None) for child in view.children]


def test_overview_button_layout_includes_share_to_clan():
    from modules.community.shard_tracker.views import ShardTrackerView

    tracker = ShardTracker(commands.Bot(command_prefix="!", intents=discord.Intents.none()))
    view = ShardTrackerView(
        owner_id=42,
        controller=tracker,
        active_tab="overview",
        shard_labels={"ancient": "Ancient", "void": "Void", "sacred": "Sacred", "primal": "Primal"},
        shard_emojis={},
        timeout=None,
    )

    labels = _button_labels(view)
    custom_ids = _button_custom_ids(view)
    assert labels[0] == "Overview"
    assert "Last Pulls" in labels
    assert "Share to Clan" in labels
    assert "action:share:overview" in custom_ids


def test_section_headings_use_configured_icon_sources():
    tracker = ShardTracker(commands.Bot(command_prefix="!", intents=discord.Intents.none()))
    tracker._tab_emojis = {
        "mystery": discord.PartialEmoji.from_str("<:mystery:123456789012345678>"),
        "ancient": discord.PartialEmoji.from_str("<:ancient:223456789012345678>"),
        "primal": discord.PartialEmoji.from_str("<:primal:323456789012345678>"),
    }
    tracker._emoji_tags = {
        **tracker._emoji_tags,
        "void": "void_icon",
        "sacred": "sacred_icon",
        "remnant": "remnant_icon",
    }
    record = tracker.store._new_record([], 42, "Tester")  # type: ignore[arg-type]
    clan = _share_clan()
    user = type("User", (), {"id": 42, "display_name": "Tester", "name": "Tester"})()

    overview, _ = tracker._build_panel(user, record, None, "overview")
    last_pulls, _ = tracker._build_panel(user, record, None, "last_pulls")
    shared = tracker._build_share_embed(user, record, clan)

    assert overview.fields[0].name == "<:mystery:123456789012345678> Mystery"
    assert overview.fields[1].name == "<:ancient:223456789012345678> Ancient"
    assert overview.fields[2].name == "void_icon Void"
    assert overview.fields[3].name == "<:primal:323456789012345678> Primal"
    assert overview.fields[4].name == "sacred_icon Sacred"
    assert overview.fields[5].name == "remnant_icon Remnants"
    assert [field.name for field in last_pulls.fields] == [
        "<:ancient:223456789012345678> Ancient",
        "void_icon Void",
        "<:primal:323456789012345678> Primal",
        "sacred_icon Sacred",
        "remnant_icon Remnants",
    ]
    assert shared.fields[0].name == "<:mystery:123456789012345678> Mystery"
    assert shared.fields[1].name == "<:ancient:223456789012345678> Ancient"


def test_detail_button_layout_still_includes_share_to_clan():
    from modules.community.shard_tracker.views import ShardTrackerView

    tracker = ShardTracker(commands.Bot(command_prefix="!", intents=discord.Intents.none()))
    view = ShardTrackerView(
        owner_id=42,
        controller=tracker,
        active_tab="ancient",
        shard_labels={"ancient": "Ancient", "void": "Void", "sacred": "Sacred", "primal": "Primal"},
        shard_emojis={},
        timeout=None,
    )

    assert "Share to Clan" in _button_labels(view)
    assert "action:share:ancient" in _button_custom_ids(view)


def test_share_embed_uses_overview_payload():
    tracker = ShardTracker(commands.Bot(command_prefix="!", intents=discord.Intents.none()))
    record = tracker.store._new_record([], 42, "Tester")  # type: ignore[arg-type]
    clan = _share_clan()
    user = type("User", (), {"id": 42, "display_name": "Tester", "name": "Tester"})()

    embed = tracker._build_share_embed(user, record, clan)

    assert embed.title == "Shard Snapshot — Tester"
    assert embed.description == "Shared to `alpha`."
    assert [field.name for field in embed.fields] == ["Mystery", "Ancient", "Void", "Primal", "Sacred", "Remnants"]


def test_share_button_action_routes_overview_without_active_tab_payload():
    async def runner():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        tracker = ShardTracker(bot)
        tracker._feature_enabled = lambda: True
        tracker.store.get_config = AsyncMock(return_value=ShardTrackerConfig(sheet_id="s", tab_name="t", channel_id=999))
        tracker.store.load_record = AsyncMock(return_value=tracker.store._new_record([], 42, "Tester"))  # type: ignore[arg-type]
        tracker._handle_share_summary_action = AsyncMock()
        interaction = _ShareInteraction()
        interaction.guild = type("Guild", (), {"id": 123})()

        await tracker.handle_button_interaction(
            interaction=interaction,
            custom_id="action:share:overview",
            active_tab="overview",
        )

        assert "active_tab" not in tracker._handle_share_summary_action.await_args.kwargs

    asyncio.run(runner())


def test_detail_share_button_action_preserves_overview_share_behavior():
    async def runner():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        tracker = ShardTracker(bot)
        tracker._feature_enabled = lambda: True
        tracker.store.get_config = AsyncMock(return_value=ShardTrackerConfig(sheet_id="s", tab_name="t", channel_id=999))
        tracker.store.load_record = AsyncMock(return_value=tracker.store._new_record([], 42, "Tester"))  # type: ignore[arg-type]
        tracker._handle_share_summary_action = AsyncMock()
        interaction = _ShareInteraction()
        interaction.guild = type("Guild", (), {"id": 123})()

        await tracker.handle_button_interaction(
            interaction=interaction,
            custom_id="action:share:ancient",
            active_tab="ancient",
        )

        assert "active_tab" not in tracker._handle_share_summary_action.await_args.kwargs

        record = tracker.store._new_record([], 42, "Tester")  # type: ignore[arg-type]
        clan = _share_clan()
        embed = tracker._build_share_embed(interaction.user, record, clan)
        assert embed.title == "Shard Snapshot — Tester"
        assert [field.name for field in embed.fields] == ["Mystery", "Ancient", "Void", "Primal", "Sacred", "Remnants"]

    asyncio.run(runner())


def test_shard_tab_order_matches_required_sequence():
    from modules.community.shard_tracker.views import ShardTrackerView

    tracker = ShardTracker(commands.Bot(command_prefix="!", intents=discord.Intents.none()))
    labels = {kind.key: kind.label for kind in SHARD_KINDS.values()}
    view = ShardTrackerView(
        owner_id=42,
        controller=tracker,
        active_tab="overview",
        shard_labels=labels,
        shard_emojis={},
        action_capabilities=tracker._action_capabilities(),
        timeout=None,
    )

    assert _button_labels(view)[:8] == [
        "Overview",
        "Mystery",
        "Ancient",
        "Void",
        "Primal",
        "Sacred",
        "Remnant",
        "Last Pulls",
    ]


def test_mystery_and_remnant_button_layouts_are_capability_aware():
    from modules.community.shard_tracker.views import ShardTrackerView

    tracker = ShardTracker(commands.Bot(command_prefix="!", intents=discord.Intents.none()))
    labels = {kind.key: kind.label for kind in SHARD_KINDS.values()}

    mystery = ShardTrackerView(
        owner_id=42,
        controller=tracker,
        active_tab="mystery",
        shard_labels=labels,
        shard_emojis={},
        action_capabilities=tracker._action_capabilities(),
        timeout=None,
    )
    mystery_action_labels = _button_labels(mystery)[8:]
    assert mystery_action_labels == ["+ Stash", "- Pulls", "Share to Clan"]

    remnant = ShardTrackerView(
        owner_id=42,
        controller=tracker,
        active_tab="remnant",
        shard_labels=labels,
        shard_emojis={},
        action_capabilities=tracker._action_capabilities(),
        timeout=None,
    )
    remnant_action_labels = _button_labels(remnant)[8:]
    assert remnant_action_labels == [
        "+ Stash",
        "- Summons",
        "Share to Clan",
        "Got Mythical",
        "Last Pulls / Mercy",
    ]


def test_mystery_and_remnant_rendering_shapes():
    tracker = ShardTracker(commands.Bot(command_prefix="!", intents=discord.Intents.none()))
    record = tracker.store._new_record([], 42, "Tester")  # type: ignore[arg-type]
    record.mysteries_owned = 12
    record.remnants_owned = 450
    record.remnants_since_mythic = 25
    record.last_remnant_mythic_iso = "2024-01-01T00:00:00+00:00"
    user = type("User", (), {"id": 42, "display_name": "Tester", "name": "Tester"})()

    overview, _ = tracker._build_panel(user, record, None, "overview")
    fields = {field.name: field.value for field in overview.fields}
    assert "Mystery" in fields
    assert overview.description == "Your shard stash at a glance. May mercy be kinder than usual."
    assert overview.footer.text == FOOTER_TEXT
    assert FOOTER_TEXT not in (overview.description or "")
    assert fields["Mystery"] == "```text\nOwned: 12\n```"
    assert "Remnants" in fields
    assert "Owned: 450" in fields["Remnants"]
    assert "Mercy: 25 / 24" in fields["Remnants"]
    assert "Chance: 3.50%" in fields["Remnants"]
    assert "Last Mythical: 2024-01-01 00:00 UTC" in fields["Remnants"]

    mystery, _ = tracker._build_panel(user, record, None, "mystery")
    assert mystery.colour == discord.Colour.green()
    assert mystery.author.name == "Mystery Shards | Tester"
    assert "Stash: **12**" in (mystery.description or "")
    assert not mystery.fields

    remnant, _ = tracker._build_panel(user, record, None, "remnant")
    assert remnant.colour == discord.Colour.red()
    assert remnant.author.name == "Cursed Remnants | Tester"
    assert "Shards" not in remnant.author.name
    assert "Each summon costs 100 Cursed Remnants." in (remnant.description or "")
    assert "Mythical Mercy: 25 / 24" in (remnant.description or "")
    assert "Mythical Chance: 3.50%" in (remnant.description or "")
    assert "Last Mythical: 2024-01-01 00:00 UTC" in (remnant.description or "")
    assert remnant.fields and remnant.fields[0].name == "Progress"
    assert all("Legendary" not in (field.name + field.value) for field in remnant.fields)


def test_overview_code_blocks_keep_single_label_spacing_and_primal_sections_tight():
    tracker = ShardTracker(commands.Bot(command_prefix="!", intents=discord.Intents.none()))
    record = tracker.store._new_record([], 42, "Tester")  # type: ignore[arg-type]
    record.ancients_owned = 1127
    record.ancients_since_lego = 36
    record.primals_owned = 5
    record.primals_since_lego = 16
    record.primals_since_mythic = 21
    record.last_primal_mythic_iso = "2026-06-05T11:05:00+00:00"
    user = type("User", (), {"id": 42, "display_name": "Tester", "name": "Tester"})()

    overview, _ = tracker._build_panel(user, record, None, "overview")
    fields = {field.name: field.value for field in overview.fields}

    assert overview.footer.text == FOOTER_TEXT
    assert FOOTER_TEXT not in (overview.description or "")
    assert all("Owned:  " not in field.value for field in overview.fields)
    assert all("Mercy:  " not in field.value for field in overview.fields)
    assert "Owned: 1,127" in fields["Ancient"]
    assert "Mercy: 36 / 200 | Chance: 0.50%" in fields["Ancient"]
    assert fields["Primal"] == (
        "```text\n"
        "Owned: 5\n"
        "Legendary\n"
        "Mercy: 16 / 75 | Chance: 1.00%\n"
        "Mythical\n"
        "Mercy: 21 / 200 | Chance: 0.50%\n"
        "Last Mythical: 2026-06-05 11:05 UTC\n"
        "```"
    )


def test_mystery_and_remnant_state_mutations():
    tracker = ShardTracker(commands.Bot(command_prefix="!", intents=discord.Intents.none()))
    record = tracker.store._new_record([], 42, "Tester")  # type: ignore[arg-type]

    tracker._apply_stash_increase(record, SHARD_KINDS["mystery"], 10)
    assert record.mysteries_owned == 10
    ok, message = tracker._apply_pull_usage(record, SHARD_KINDS["mystery"], 99)
    assert ok and message == ""
    assert record.mysteries_owned == 0
    assert record.ancients_since_lego == 0
    assert record.last_ancient_lego_iso == ""

    tracker._apply_stash_increase(record, SHARD_KINDS["remnant"], 250)
    ok, message = tracker._apply_pull_usage(record, SHARD_KINDS["remnant"], 3)
    assert not ok
    assert "2 summons" in message
    assert record.remnants_owned == 250
    assert record.remnants_since_mythic == 0

    ok, message = tracker._apply_pull_usage(record, SHARD_KINDS["remnant"], 2)
    assert ok and message == ""
    assert record.remnants_owned == 50
    assert record.remnants_since_mythic == 2


def test_last_pulls_embed_includes_remnant_mythical_once():
    tracker = ShardTracker(commands.Bot(command_prefix="!", intents=discord.Intents.none()))
    record = tracker.store._new_record([], 42, "Tester")  # type: ignore[arg-type]
    record.remnants_since_mythic = 25
    record.last_remnant_mythic_iso = "2024-01-01T00:00:00+00:00"
    record.last_remnant_mythic_depth = 25
    user = type("User", (), {"id": 42, "display_name": "Tester", "name": "Tester"})()

    embed, _ = tracker._build_panel(user, record, None, "last_pulls")
    field_values = "\n".join(field.value for field in embed.fields)
    field_names = [field.name for field in embed.fields]

    assert field_values.count("Last Mythical:") >= 1
    assert "Remnants" in field_names
    assert "Mystery" not in field_names
    assert "Mercy Info" not in field_names
    assert "Base chances" not in field_values
    assert embed.footer.text == FOOTER_TEXT
    assert FOOTER_TEXT not in field_values


def test_shards_help_text_covers_user_facing_actions():
    tracker = ShardTracker(commands.Bot(command_prefix="!", intents=discord.Intents.none()))
    help_text = tracker.shards.help or ""

    assert "Mystery" in help_text or "mystery" in help_text
    assert "Remnant" in help_text or "remnant" in help_text
    assert "+ Stash" in help_text
    assert "- Pulls" in help_text
    assert "- Summons" in help_text
    assert "Share to Clan" in help_text
    assert "!shards set <type> <count>" in help_text
    assert "Mercy rules" in help_text
    assert "Ancient/Void Legendary: after 200 pulls, +5% per shard" in help_text
    assert "Remnant Mythical: after 24 summons, +1% per summon" in help_text
    assert "Base chances" in help_text
    assert "Ancient Legendary: 0.5%" in help_text
    assert "Remnant Mythical: 2.5%" in help_text
    assert "100 Cursed Remnants" in help_text
