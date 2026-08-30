import asyncio
from unittest.mock import AsyncMock

import discord

from modules.ops import server_rules_share_destinations as sharing


class Permissions:
    def __init__(self, *, view=True, send=True, embed=True):
        self.view_channel = view
        self.send_messages = send
        self.send_messages_in_threads = False
        self.embed_links = embed


class Channel:
    def __init__(
        self,
        channel_id,
        name,
        *,
        category_id=None,
        position=0,
        user_ok=True,
        bot_ok=True,
        channel_type=discord.ChannelType.text,
    ):
        self.id = channel_id
        self.name = name
        self.category_id = category_id
        self.position = position
        self.type = channel_type
        self.mention = f"<#{channel_id}>"
        self._user_ok = user_ok
        self._bot_ok = bot_ok
        self._user = None
        self._bot = None

    def permissions_for(self, member):
        if member is self._user:
            return Permissions(view=self._user_ok, send=self._user_ok)
        if member is self._bot:
            return Permissions(view=self._bot_ok, send=self._bot_ok, embed=self._bot_ok)
        return Permissions(view=False, send=False, embed=False)


class Category:
    def __init__(self, category_id, channels):
        self.id = category_id
        self.channels = channels
        self.type = discord.ChannelType.category


class Guild:
    def __init__(self, general, category, extras=()):
        self.me = object()
        self._channels = {general.id: general, category.id: category}
        self._channels.update({channel.id: channel for channel in category.channels})
        self._channels.update({channel.id: channel for channel in extras})

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


class Interaction:
    def __init__(self, guild, user):
        self.guild = guild
        self.user = user


def build_fixture():
    user = object()
    general = Channel(100000000000000001, "general-chat", position=99)
    clan_one = Channel(
        100000000000000002,
        "clan-one",
        category_id=100000000000000010,
        position=2,
    )
    clan_two = Channel(
        100000000000000003,
        "clan-two",
        category_id=100000000000000010,
        position=1,
    )
    hidden_clan = Channel(
        100000000000000004,
        "hidden-clan",
        category_id=100000000000000010,
        user_ok=False,
    )
    admin = Channel(100000000000000005, "admin", category_id=100000000000000020)
    category = Category(
        100000000000000010, [clan_one, hidden_clan, clan_two]
    )
    guild = Guild(general, category, extras=(admin,))
    for channel in (general, clan_one, clan_two, hidden_clan, admin):
        channel._user = user
        channel._bot = guild.me
    interaction = Interaction(guild, user)
    config = sharing.ShareDestinationConfig(general.id, category.id)
    return interaction, config, general, clan_one, clan_two, hidden_clan, admin


def test_candidates_only_include_general_and_accessible_clan_chats():
    interaction, config, general, clan_one, clan_two, hidden_clan, admin = build_fixture()
    channels = sharing._candidate_channels(interaction.guild, interaction, config)

    assert [channel.id for channel in channels] == [
        general.id,
        clan_two.id,
        clan_one.id,
    ]
    assert hidden_clan not in channels
    assert admin not in channels


def test_bot_permission_failure_removes_destination():
    interaction, config, general, clan_one, clan_two, _hidden, _admin = build_fixture()
    clan_two._bot_ok = False

    channels = sharing._candidate_channels(interaction.guild, interaction, config)

    assert [channel.id for channel in channels] == [general.id, clan_one.id]


def test_allowed_by_config_rejects_unrelated_text_channels():
    interaction, config, general, clan_one, _clan_two, _hidden, admin = build_fixture()

    assert sharing._allowed_by_config(general, config)
    assert sharing._allowed_by_config(clan_one, config)
    assert not sharing._allowed_by_config(admin, config)


def test_destination_select_is_regular_select_with_only_supplied_channels():
    interaction, config, general, clan_one, clan_two, _hidden, _admin = build_fixture()
    ui = type(
        "UI",
        (),
        {"share_channel_placeholder": "Choose where to share"},
    )()

    select = sharing.FAQShareDestinationSelect(
        object(), "topic", "question", ui, config, [general, clan_two, clan_one]
    )

    assert isinstance(select, discord.ui.Select)
    assert not isinstance(select, discord.ui.ChannelSelect)
    assert [option.value for option in select.options] == [
        str(general.id),
        str(clan_two.id),
        str(clan_one.id),
    ]
    assert [option.label for option in select.options] == [
        "#general-chat",
        "#clan-two",
        "#clan-one",
    ]


def test_config_is_loaded_from_mirralith_keys(monkeypatch):
    async def run():
        getter = AsyncMock(
            side_effect=lambda key, default=None: {
                sharing.GENERAL_CHAT_KEY: "868947253849649152",
                sharing.CLAN_CHAT_CATEGORY_KEY: "689507571450773512",
            }.get(key, default)
        )
        monkeypatch.setattr(
            sharing.base.recruitment_sheet, "get_config_value_async", getter
        )

        config, errors = await sharing._load_config()

        assert errors == []
        assert config == sharing.ShareDestinationConfig(
            868947253849649152, 689507571450773512
        )
        assert getter.await_count == 2

    asyncio.run(run())
