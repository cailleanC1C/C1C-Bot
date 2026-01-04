import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs.app_admin import AppAdmin
from modules.common import feature_flags
from modules.common import runtime as runtime_helpers
from modules.ops import cluster_role_map, server_map
from shared.sheets import recruitment as recruitment_sheet


class FakeAuthor:
    def __init__(self, user_id: int) -> None:
        self.id = user_id
        self.mention = f"<@{user_id}>"
        self.name = f"user-{user_id}"


class FakeMessage:
    _next_id = 1000

    def __init__(
        self,
        channel,
        content: str,
        author: FakeAuthor,
        *,
        created_at: dt.datetime | None = None,
    ) -> None:
        self.id = FakeMessage._next_id
        FakeMessage._next_id += 1
        self.channel = channel
        self.author = author
        self.content = content
        self.created_at = created_at or dt.datetime.now(dt.timezone.utc)
        self.deleted = False
        self.edited_content: str | None = None

    async def delete(self) -> None:
        self.deleted = True

    async def edit(self, *, content: str) -> None:
        self.content = content
        self.edited_content = content


class FakeChannel:
    def __init__(self, channel_id: int, guild, bot_user: FakeAuthor) -> None:
        self.id = channel_id
        self.guild = guild
        self.mention = f"<#{channel_id}>"
        self.name = f"channel-{channel_id}"
        self._bot_user = bot_user
        self.history_messages: list[FakeMessage] = []
        self.sent_messages: list[FakeMessage] = []
        self.deleted_messages: list[FakeMessage] = []

    def seed_history(self, messages: list[FakeMessage]) -> None:
        for message in messages:
            message.channel = self
            self.history_messages.append(message)

    async def history(self, limit: int | None = None):
        count = 0
        for message in reversed(list(self.history_messages)):
            if limit is not None and count >= limit:
                break
            count += 1
            yield message

    async def send(self, content: str) -> FakeMessage:
        message = FakeMessage(self, content, author=self._bot_user)
        self.history_messages.append(message)
        self.sent_messages.append(message)
        return message

    async def delete_messages(self, messages: list[FakeMessage]) -> None:
        for message in messages:
            await message.delete()
            self.deleted_messages.append(message)


class FakeCategory:
    def __init__(self, channel_id: int, guild) -> None:
        self.id = channel_id
        self.guild = guild


class FakeGuild:
    def __init__(self, guild_id: int = 1, name: str = "Guild") -> None:
        self.id = guild_id
        self.name = name


class FakeBot:
    def __init__(self) -> None:
        self.user = FakeAuthor(999)
        self._channels: dict[int, FakeChannel] = {}

    def register_channel(self, channel: FakeChannel) -> None:
        self._channels[channel.id] = channel

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)

    async def fetch_channel(self, channel_id: int):
        return self._channels.get(channel_id)


def test_servermap_refresh_command_respects_feature_toggle(monkeypatch):
    async def _run() -> None:
        bot = object()
        cog = AppAdmin(bot)
        ctx = SimpleNamespace()
        ctx.reply = AsyncMock()

        monkeypatch.setattr(server_map.feature_flags, "is_enabled", lambda key: False)

        async def _fail(*_args, **_kwargs):  # pragma: no cover - defensive assertion
            raise AssertionError("refresh_server_map should not be invoked when disabled")

        monkeypatch.setattr(server_map, "refresh_server_map", _fail)

        log_messages: list[str] = []

        async def fake_log(message: str) -> None:
            log_messages.append(message)

        monkeypatch.setattr(runtime_helpers, "send_log_message", fake_log)

        await cog.servermap_refresh.callback(cog, ctx)

        ctx.reply.assert_awaited_once_with(
            "Server map feature is currently disabled in FeatureToggles.",
            mention_author=False,
        )
        assert log_messages == ["📘 Server map — skipped • reason=feature_disabled"]

    asyncio.run(_run())


def test_servermap_refresh_command_invokes_refresh_when_enabled(monkeypatch):
    async def _run() -> None:
        bot = object()
        cog = AppAdmin(bot)
        ctx = SimpleNamespace()
        ctx.reply = AsyncMock()

        monkeypatch.setattr(server_map.feature_flags, "is_enabled", lambda key: True)

        refresh = AsyncMock(
            return_value=server_map.ServerMapResult(status="ok", message_count=2, total_chars=1200)
        )
        monkeypatch.setattr(server_map, "refresh_server_map", refresh)

        await cog.servermap_refresh.callback(cog, ctx)

        refresh.assert_awaited_once_with(bot, force=True, actor="command", requested_channel="ctx")
        ctx.reply.assert_awaited_once_with(
            "Server map refreshed — messages=2 • chars=1200.",
            mention_author=False,
        )

    asyncio.run(_run())


def test_whoweare_command_respects_feature_toggle(monkeypatch):
    async def _run() -> None:
        bot = object()
        cog = AppAdmin(bot)
        ctx = SimpleNamespace()
        ctx.guild = SimpleNamespace(name="Guild")
        ctx.reply = AsyncMock()

        monkeypatch.setattr(feature_flags, "is_enabled", lambda key: False)

        log_messages: list[str] = []

        async def fake_log(message: str) -> None:
            log_messages.append(message)

        monkeypatch.setattr(runtime_helpers, "send_log_message", fake_log)

        await cog.whoweare.callback(cog, ctx)

        ctx.reply.assert_awaited_once_with(
            "Cluster role map feature is disabled in FeatureToggles.",
            mention_author=False,
        )
        assert log_messages == [
            "📘 **Cluster role map** — cmd=whoweare • guild=Guild • status=disabled"
        ]

    asyncio.run(_run())


def test_whoweare_command_reports_sheet_errors(monkeypatch):
    async def _run() -> None:
        bot = object()
        cog = AppAdmin(bot)
        ctx = SimpleNamespace()
        ctx.guild = SimpleNamespace(name="Guild")
        ctx.reply = AsyncMock()

        monkeypatch.setattr(feature_flags, "is_enabled", lambda key: True)
        monkeypatch.setattr(recruitment_sheet, "get_role_map_tab_name", lambda: "WhoWeAre")

        async def raise_error(*_args, **_kwargs):
            raise cluster_role_map.RoleMapLoadError("sheet offline")

        monkeypatch.setattr(cluster_role_map, "fetch_role_map_rows", raise_error)

        log_messages: list[str] = []

        async def fake_log(message: str) -> None:
            log_messages.append(message)

        monkeypatch.setattr(runtime_helpers, "send_log_message", fake_log)

        await cog.whoweare.callback(cog, ctx)

        ctx.reply.assert_awaited_once_with(
            "I couldn’t read the role map sheet (`WhoWeAre`). Please check Config and try again.",
            mention_author=False,
        )
        assert log_messages == [
            "📘 **Cluster role map** — cmd=whoweare • guild=Guild • status=error • reason=sheet offline"
        ]

    asyncio.run(_run())


def test_whoweare_command_cleans_old_map_messages(monkeypatch):
    async def _run() -> None:
        bot = FakeBot()
        guild = FakeGuild()
        channel = FakeChannel(111, guild, bot.user)
        bot.register_channel(channel)

        now = dt.datetime.now(dt.timezone.utc)
        recent = FakeMessage(
            channel,
            f"recent {cluster_role_map.INVISIBLE_MARKER}",
            bot.user,
            created_at=now - dt.timedelta(days=1),
        )
        older = FakeMessage(
            channel,
            f"older {cluster_role_map.INVISIBLE_MARKER}",
            bot.user,
            created_at=now - dt.timedelta(days=20),
        )
        other = FakeMessage(channel, "other", FakeAuthor(123))
        channel.seed_history([other, older, recent])

        cog = AppAdmin(bot)
        ctx = SimpleNamespace()
        ctx.guild = guild
        ctx.channel = channel
        ctx.reply = AsyncMock()
        ctx.bot = bot

        monkeypatch.setattr(runtime_helpers.discord, "TextChannel", FakeChannel)

        monkeypatch.setattr(feature_flags, "is_enabled", lambda key: True)
        monkeypatch.setattr(recruitment_sheet, "get_role_map_tab_name", lambda: "WhoWeAre")

        async def fake_fetch(*_args, **_kwargs):
            return []

        monkeypatch.setattr(cluster_role_map, "fetch_role_map_rows", fake_fetch)

        role_entry = cluster_role_map.RoleEntryRender(
            role_id=101,
            display_name="Guardian",
            description="Keeps order",
            members=[],
            usage="",
        )
        category = cluster_role_map.RoleMapCategoryRender(
            name="ClusterSupport",
            emoji="🛡️",
            roles=[role_entry],
        )
        render = cluster_role_map.RoleMapRender(
            categories=[category],
            category_count=1,
            role_count=1,
            unassigned_roles=1,
        )

        monkeypatch.setattr(cluster_role_map, "build_role_map_render", lambda guild, entries: render)
        monkeypatch.setattr("cogs.app_admin.get_who_we_are_channel_id", lambda: channel.id)

        log_messages: list[str] = []

        async def fake_log(message: str) -> None:
            log_messages.append(message)

        monkeypatch.setattr(runtime_helpers, "send_log_message", fake_log)

        await cog.whoweare.callback(cog, ctx)

        assert recent.deleted is True
        assert older.deleted is True
        assert other.deleted is False
        assert any("cleaned_messages=2" in message for message in log_messages)

    asyncio.run(_run())


def test_whoweare_command_posts_multi_message_map(monkeypatch):
    async def _run() -> None:
        bot = FakeBot()
        guild = FakeGuild()
        channel = FakeChannel(321, guild, bot.user)
        bot.register_channel(channel)

        cog = AppAdmin(bot)
        ctx = SimpleNamespace()
        ctx.guild = guild
        ctx.channel = channel
        ctx.reply = AsyncMock()
        ctx.bot = bot

        monkeypatch.setattr(runtime_helpers.discord, "TextChannel", FakeChannel)

        monkeypatch.setattr(feature_flags, "is_enabled", lambda key: True)
        monkeypatch.setattr(recruitment_sheet, "get_role_map_tab_name", lambda: "WhoWeAre")

        async def fake_fetch(*_args, **_kwargs):
            return []

        monkeypatch.setattr(cluster_role_map, "fetch_role_map_rows", fake_fetch)

        role_entry = cluster_role_map.RoleEntryRender(
            role_id=100,
            display_name="Cluster Leader",
            description="Runs it",
            members=["<@100>"],
            usage="",
        )
        category = cluster_role_map.RoleMapCategoryRender(
            name="ClusterLeadership",
            emoji="🔥",
            roles=[role_entry],
        )
        render = cluster_role_map.RoleMapRender(
            categories=[category],
            category_count=1,
            role_count=1,
            unassigned_roles=0,
        )

        monkeypatch.setattr(cluster_role_map, "build_role_map_render", lambda guild, entries: render)
        monkeypatch.setattr("cogs.app_admin.get_who_we_are_channel_id", lambda: channel.id)

        log_messages: list[str] = []

        async def fake_log(message: str) -> None:
            log_messages.append(message)

        monkeypatch.setattr(runtime_helpers, "send_log_message", fake_log)

        await cog.whoweare.callback(cog, ctx)

        ctx.reply.assert_awaited_once_with("Cluster role map updated.", mention_author=False)
        assert len(channel.sent_messages) == 2
        index_message, category_message = channel.sent_messages
        assert "Jump to:" in index_message.content
        expected_link = cluster_role_map.build_jump_url(
            guild.id,
            channel.id,
            category_message.id,
        )
        assert f"🔥 [ClusterLeadership]({expected_link})" in index_message.content
        assert "**🔥 ClusterLeadership**" in category_message.content
        assert "Cluster Leader" in category_message.content
        assert ":small_blue_diamond: <@100>" in category_message.content
        assert index_message.content.rstrip().endswith(cluster_role_map.INVISIBLE_MARKER)
        assert category_message.content.rstrip().endswith(cluster_role_map.INVISIBLE_MARKER)
        assert log_messages[-1] == (
            "📘 **Cluster role map** — cmd=whoweare • guild=Guild • categories=1 "
            "• roles=1 • unassigned_roles=0 • category_messages=1 • target_channel=#Guild:321"
        )

    asyncio.run(_run())


def test_whoweare_command_marks_unassigned_with_blue_diamond(monkeypatch):
    async def _run() -> None:
        bot = FakeBot()
        guild = FakeGuild()
        channel = FakeChannel(654, guild, bot.user)
        bot.register_channel(channel)

        cog = AppAdmin(bot)
        ctx = SimpleNamespace()
        ctx.guild = guild
        ctx.channel = channel
        ctx.reply = AsyncMock()
        ctx.bot = bot

        monkeypatch.setattr(runtime_helpers.discord, "TextChannel", FakeChannel)

        monkeypatch.setattr(feature_flags, "is_enabled", lambda key: True)
        monkeypatch.setattr(recruitment_sheet, "get_role_map_tab_name", lambda: "WhoWeAre")

        async def fake_fetch(*_args, **_kwargs):
            return []

        monkeypatch.setattr(cluster_role_map, "fetch_role_map_rows", fake_fetch)

        role_entry = cluster_role_map.RoleEntryRender(
            role_id=200,
            display_name="Cluster Supporter",
            description="Helps out",
            members=[],
            usage="",
        )
        category = cluster_role_map.RoleMapCategoryRender(
            name="ClusterSupport",
            emoji="🛡️",
            roles=[role_entry],
        )
        render = cluster_role_map.RoleMapRender(
            categories=[category],
            category_count=1,
            role_count=1,
            unassigned_roles=1,
        )

        monkeypatch.setattr(cluster_role_map, "build_role_map_render", lambda guild, entries: render)
        monkeypatch.setattr("cogs.app_admin.get_who_we_are_channel_id", lambda: channel.id)

        await cog.whoweare.callback(cog, ctx)

        assert len(channel.sent_messages) == 2
        category_message = channel.sent_messages[1]
        assert ":small_blue_diamond: (currently unassigned)" in category_message.content
        assert category_message.content.rstrip().endswith(cluster_role_map.INVISIBLE_MARKER)

    asyncio.run(_run())


def test_whoweare_command_handles_empty_categories(monkeypatch):
    async def _run() -> None:
        bot = FakeBot()
        guild = FakeGuild()
        channel = FakeChannel(222, guild, bot.user)
        bot.register_channel(channel)

        cog = AppAdmin(bot)
        ctx = SimpleNamespace()
        ctx.guild = guild
        ctx.channel = channel
        ctx.reply = AsyncMock()
        ctx.bot = bot

        monkeypatch.setattr(runtime_helpers.discord, "TextChannel", FakeChannel)

        monkeypatch.setattr(feature_flags, "is_enabled", lambda key: True)
        monkeypatch.setattr(recruitment_sheet, "get_role_map_tab_name", lambda: "WhoWeAre")

        async def fake_fetch(*_args, **_kwargs):
            return []

        monkeypatch.setattr(cluster_role_map, "fetch_role_map_rows", fake_fetch)

        render = cluster_role_map.RoleMapRender(
            categories=[],
            category_count=0,
            role_count=0,
            unassigned_roles=0,
        )

        monkeypatch.setattr(cluster_role_map, "build_role_map_render", lambda guild, entries: render)
        monkeypatch.setattr("cogs.app_admin.get_who_we_are_channel_id", lambda: channel.id)

        log_messages: list[str] = []

        async def fake_log(message: str) -> None:
            log_messages.append(message)

        monkeypatch.setattr(runtime_helpers, "send_log_message", fake_log)

        await cog.whoweare.callback(cog, ctx)

        assert len(channel.sent_messages) == 1
        assert "No categories are currently available" in channel.sent_messages[0].content
        ctx.reply.assert_awaited_once_with("Cluster role map updated.", mention_author=False)
        assert log_messages[-1].endswith("category_messages=0 • target_channel=#Guild:222")

    asyncio.run(_run())


def test_whoweare_command_logs_channel_fallback(monkeypatch):
    async def _run() -> None:
        bot = FakeBot()
        guild = FakeGuild()
        channel = FakeChannel(333, guild, bot.user)
        bot.register_channel(channel)

        cog = AppAdmin(bot)
        ctx = SimpleNamespace()
        ctx.guild = guild
        ctx.channel = channel
        ctx.reply = AsyncMock()
        ctx.bot = bot

        monkeypatch.setattr(runtime_helpers.discord, "TextChannel", FakeChannel)

        monkeypatch.setattr(feature_flags, "is_enabled", lambda key: True)
        monkeypatch.setattr(recruitment_sheet, "get_role_map_tab_name", lambda: "WhoWeAre")

        async def fake_fetch(*_args, **_kwargs):
            return []

        monkeypatch.setattr(cluster_role_map, "fetch_role_map_rows", fake_fetch)

        role_entry = cluster_role_map.RoleEntryRender(
            role_id=300,
            display_name="Lead",
            description="Keeps lights on",
            members=["<@200>"],
            usage="",
        )
        category = cluster_role_map.RoleMapCategoryRender(
            name="ClusterLeadership",
            emoji="🔥",
            roles=[role_entry],
        )
        render = cluster_role_map.RoleMapRender(
            categories=[category],
            category_count=1,
            role_count=1,
            unassigned_roles=0,
        )

        monkeypatch.setattr(cluster_role_map, "build_role_map_render", lambda guild, entries: render)
        monkeypatch.setattr("cogs.app_admin.get_who_we_are_channel_id", lambda: 999999)

        log_messages: list[str] = []

        async def fake_log(message: str) -> None:
            log_messages.append(message)

        monkeypatch.setattr(runtime_helpers, "send_log_message", fake_log)

        await cog.whoweare.callback(cog, ctx)

        assert channel.sent_messages == []
        assert log_messages == [
            "📘 **Cluster role map** — cmd=whoweare • guild=Guild • status=error • reason=invalid_channel"
        ]
        ctx.reply.assert_awaited_once_with(
            "I couldn’t determine where to post the role map. Please try again in a guild channel.",
            mention_author=False,
        )

    asyncio.run(_run())


def test_whoweare_command_falls_back_for_non_messageable_channel(monkeypatch):
    async def _run() -> None:
        bot = FakeBot()
        guild = FakeGuild()
        fallback_channel = FakeChannel(555, guild, bot.user)
        configured = FakeCategory(444, guild)
        bot.register_channel(fallback_channel)
        bot.register_channel(configured)

        cog = AppAdmin(bot)
        ctx = SimpleNamespace()
        ctx.guild = guild
        ctx.channel = fallback_channel
        ctx.reply = AsyncMock()
        ctx.bot = bot

        monkeypatch.setattr(runtime_helpers.discord, "TextChannel", FakeChannel)

        monkeypatch.setattr(feature_flags, "is_enabled", lambda key: True)
        monkeypatch.setattr(recruitment_sheet, "get_role_map_tab_name", lambda: "WhoWeAre")

        async def fake_fetch(*_args, **_kwargs):
            return []

        monkeypatch.setattr(cluster_role_map, "fetch_role_map_rows", fake_fetch)

        role_entry = cluster_role_map.RoleEntryRender(
            role_id=400,
            display_name="Lead",
            description="Keeps lights on",
            members=["<@200>"],
            usage="",
        )
        category = cluster_role_map.RoleMapCategoryRender(
            name="ClusterLeadership",
            emoji="🔥",
            roles=[role_entry],
        )
        render = cluster_role_map.RoleMapRender(
            categories=[category],
            category_count=1,
            role_count=1,
            unassigned_roles=0,
        )

        monkeypatch.setattr(cluster_role_map, "build_role_map_render", lambda guild, entries: render)
        monkeypatch.setattr("cogs.app_admin.get_who_we_are_channel_id", lambda: configured.id)

        log_messages: list[str] = []

        async def fake_log(message: str) -> None:
            log_messages.append(message)

        monkeypatch.setattr(runtime_helpers, "send_log_message", fake_log)

        await cog.whoweare.callback(cog, ctx)

        assert fallback_channel.sent_messages == []
        assert log_messages == [
            "📘 **Cluster role map** — cmd=whoweare • guild=Guild • status=error • reason=invalid_channel"
        ]
        ctx.reply.assert_awaited_once_with(
            "I couldn’t determine where to post the role map. Please try again in a guild channel.",
            mention_author=False,
        )

    asyncio.run(_run())


def test_whoweare_command_falls_back_for_foreign_channel(monkeypatch):
    async def _run() -> None:
        bot = FakeBot()
        guild = FakeGuild()
        other_guild = FakeGuild(guild_id=2, name="Elsewhere")
        channel = FakeChannel(666, guild, bot.user)
        foreign = FakeChannel(777, other_guild, bot.user)
        bot.register_channel(channel)
        bot.register_channel(foreign)

        cog = AppAdmin(bot)
        ctx = SimpleNamespace()
        ctx.guild = guild
        ctx.channel = channel
        ctx.reply = AsyncMock()
        ctx.bot = bot

        monkeypatch.setattr(runtime_helpers.discord, "TextChannel", FakeChannel)

        monkeypatch.setattr(feature_flags, "is_enabled", lambda key: True)
        monkeypatch.setattr(recruitment_sheet, "get_role_map_tab_name", lambda: "WhoWeAre")

        async def fake_fetch(*_args, **_kwargs):
            return []

        monkeypatch.setattr(cluster_role_map, "fetch_role_map_rows", fake_fetch)

        role_entry = cluster_role_map.RoleEntryRender(
            role_id=500,
            display_name="Lead",
            description="Keeps lights on",
            members=["<@200>"],
            usage="",
        )
        category = cluster_role_map.RoleMapCategoryRender(
            name="ClusterLeadership",
            emoji="🔥",
            roles=[role_entry],
        )
        render = cluster_role_map.RoleMapRender(
            categories=[category],
            category_count=1,
            role_count=1,
            unassigned_roles=0,
        )

        monkeypatch.setattr(cluster_role_map, "build_role_map_render", lambda guild, entries: render)
        monkeypatch.setattr("cogs.app_admin.get_who_we_are_channel_id", lambda: foreign.id)

        log_messages: list[str] = []

        async def fake_log(message: str) -> None:
            log_messages.append(message)

        monkeypatch.setattr(runtime_helpers, "send_log_message", fake_log)

        await cog.whoweare.callback(cog, ctx)

        assert channel.sent_messages == []
        assert len(foreign.sent_messages) == 2
        assert "channel_fallback=#Elsewhere:777" in log_messages[0]
        ctx.reply.assert_awaited_once_with(
            "Cluster role map refreshed in <#777>.", mention_author=False
        )

    asyncio.run(_run())
