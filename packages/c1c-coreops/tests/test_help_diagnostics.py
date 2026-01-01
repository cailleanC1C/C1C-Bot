import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

import discord
import pytest
from discord.ext import commands


def _ensure_src_on_path() -> None:
    root = Path(__file__).resolve().parents[3]
    src = root / "packages" / "c1c-coreops" / "src"
    root_str = str(root)
    src_str = str(src)
    if root_str not in __import__("sys").path:
        __import__("sys").path.insert(0, root_str)
    if src_str not in __import__("sys").path:
        __import__("sys").path.insert(0, src_str)


_ensure_src_on_path()

from c1c_coreops.cog import CoreOpsCog, _reset_help_diagnostics_cache
from modules.ops.permissions_ui import PermissionsUICog
from cogs.recruitment_clan_profile import ClanProfileCog
from cogs.recruitment_member import RecruitmentMember
from cogs.recruitment_recruiter import RecruiterPanelCog
from cogs.recruitment_welcome import WelcomeBridge


class DummyMember:
    def __init__(self, *, is_admin: bool = False, is_staff: bool = False) -> None:
        self.display_name = "Member"
        self.id = 1 if is_admin else 2 if is_staff else 3
        self.roles: list[SimpleNamespace] = []
        self.guild_permissions = SimpleNamespace(administrator=is_admin)
        self._is_admin = is_admin
        self._is_staff = is_staff
        self.sent_messages: list[str | None] = []

    def __str__(self) -> str:  # pragma: no cover - defensive fallback
        return self.display_name

    async def send(self, content: str | None = None, **_: object) -> None:
        self.sent_messages.append(content)


class HelpContext:
    def __init__(self, bot: commands.Bot, author: DummyMember) -> None:
        self.bot = bot
        self.author = author
        self.guild = SimpleNamespace(id=1234, name="Recruitment Guild")
        self._coreops_suppress_denials = True
        self._replies: list[discord.Embed] = []
        self.command = None

    async def reply(
        self,
        *args: object,
        embed: discord.Embed | None = None,
        embeds: Iterable[discord.Embed] | None = None,
        **_: object,
    ) -> None:
        if embed is not None:
            self._replies.append(embed)
        if embeds:
            self._replies.extend(list(embeds))


class DummyLogChannel:
    def __init__(self) -> None:
        self.messages: list[str | None] = []

    async def send(self, content: str | None = None, **_: object) -> None:
        self.messages.append(content)


def _resolve_member(target):
    if isinstance(target, commands.Context):
        return getattr(target, "author", None)
    return target


@pytest.fixture(autouse=True)
def patch_rbac(monkeypatch: pytest.MonkeyPatch) -> Iterable[None]:
    _reset_help_diagnostics_cache()
    monkeypatch.setattr("c1c_coreops.rbac.get_admin_role_ids", lambda: set())
    monkeypatch.setattr("c1c_coreops.rbac.get_staff_role_ids", lambda: set())
    monkeypatch.setattr("c1c_coreops.rbac._resolve_member", _resolve_member)
    monkeypatch.setattr(
        "c1c_coreops.rbac._member_has_admin_role",
        lambda member: bool(getattr(member, "_is_admin", False)),
    )
    monkeypatch.setattr(
        "c1c_coreops.rbac._has_administrator_permission",
        lambda member: bool(
            getattr(getattr(member, "guild_permissions", None), "administrator", False)
        ),
    )
    monkeypatch.setattr(
        "c1c_coreops.rbac.is_admin_member",
        lambda target: bool(getattr(_resolve_member(target), "_is_admin", False)),
    )
    monkeypatch.setattr(
        "c1c_coreops.rbac.is_staff_member",
        lambda target: bool(
            getattr(_resolve_member(target), "_is_staff", False)
            or getattr(_resolve_member(target), "_is_admin", False)
        ),
    )
    monkeypatch.setattr(
        "c1c_coreops.rbac.is_recruiter",
        lambda target: bool(getattr(_resolve_member(target), "_is_staff", False)),
    )
    monkeypatch.setattr(
        "c1c_coreops.rbac.is_lead",
        lambda target: bool(getattr(_resolve_member(target), "_is_staff", False)),
    )
    monkeypatch.setattr(
        "c1c_coreops.rbac.ops_gate",
        lambda member: bool(
            getattr(member, "_is_admin", False) or getattr(member, "_is_staff", False)
        ),
    )
    monkeypatch.setattr(
        "c1c_coreops.rbac.can_view_admin",
        lambda target: bool(getattr(_resolve_member(target), "_is_admin", False)),
    )
    monkeypatch.setattr(
        "c1c_coreops.rbac.can_view_staff",
        lambda target: bool(
            getattr(_resolve_member(target), "_is_staff", False)
            or getattr(_resolve_member(target), "_is_admin", False)
        ),
    )
    monkeypatch.setattr(
        "c1c_coreops.cog.can_view_admin",
        lambda target: bool(getattr(_resolve_member(target), "_is_admin", False)),
    )
    monkeypatch.setattr(
        "c1c_coreops.cog.can_view_staff",
        lambda target: bool(
            getattr(_resolve_member(target), "_is_staff", False)
            or getattr(_resolve_member(target), "_is_admin", False)
        ),
    )
    monkeypatch.setattr(
        "cogs.recruitment_welcome.is_staff_member",
        lambda target: bool(
            getattr(_resolve_member(target), "_is_staff", False)
            or getattr(_resolve_member(target), "_is_admin", False)
        ),
    )
    monkeypatch.setattr(
        "cogs.recruitment_welcome.is_admin_member",
        lambda target: bool(getattr(_resolve_member(target), "_is_admin", False)),
    )
    monkeypatch.setattr("c1c_coreops.rbac.discord.Member", DummyMember)
    monkeypatch.setattr("c1c_coreops.cog.discord.Member", DummyMember)
    yield
    _reset_help_diagnostics_cache()


async def _setup_bot(monkeypatch: pytest.MonkeyPatch) -> tuple[commands.Bot, DummyLogChannel]:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    channel = DummyLogChannel()
    monkeypatch.setattr(
        "c1c_coreops.cog.resolve_ops_log_channel_id", lambda *_, **__: 999,
    )
    monkeypatch.setattr(bot, "get_channel", lambda snowflake: channel if snowflake == 999 else None)

    await bot.add_cog(CoreOpsCog(bot))
    await bot.add_cog(PermissionsUICog(bot))
    await bot.add_cog(RecruiterPanelCog(bot))
    await bot.add_cog(WelcomeBridge(bot))
    await bot.add_cog(RecruitmentMember(bot))
    await bot.add_cog(ClanProfileCog(bot))

    return bot, channel
def test_help_diagnostics_staff_logs_hidden_admin_commands(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HELP_DIAGNOSTICS", "1")
    monkeypatch.delenv("HELP_DIAGNOSTICS_TTL_SEC", raising=False)

    async def runner() -> str:
        bot, channel = await _setup_bot(monkeypatch)
        try:
            cog = bot.get_cog("CoreOpsCog")
            assert cog is not None
            ctx = HelpContext(bot, DummyMember(is_staff=True))
            await cog.render_help(ctx)
            assert channel.messages, "expected diagnostics message"
            return channel.messages[0] or ""
        finally:
            await bot.close()

    message = asyncio.run(runner())
    assert "ops config | admin | operational" in message
    assert "false | no | not runnable for staff" in message


def test_help_diagnostics_admin_logs_admin_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HELP_DIAGNOSTICS", "1")
    monkeypatch.delenv("HELP_DIAGNOSTICS_TTL_SEC", raising=False)

    async def runner() -> str:
        bot, channel = await _setup_bot(monkeypatch)
        try:
            cog = bot.get_cog("CoreOpsCog")
            assert cog is not None
            cmd = bot.get_command("welcome-refresh")
            assert cmd is not None
            assert getattr(cmd, "hidden", False) is False
            assert bot.get_command("ops welcome-refresh") is None
            ctx = HelpContext(bot, DummyMember(is_admin=True, is_staff=True))
            await cog.render_help(ctx)
            assert channel.messages, "expected diagnostics message"
            return channel.messages[0] or ""
        finally:
            await bot.close()

    message = asyncio.run(runner())
    assert "welcome-refresh | admin | operational | true | yes | ok" in message
    assert "perm | admin | operational | true | yes | ok" in message


def test_help_diagnostics_ttl_throttles(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELP_DIAGNOSTICS", "1")
    monkeypatch.setenv("HELP_DIAGNOSTICS_TTL_SEC", "120")

    async def runner() -> list[str | None]:
        bot, channel = await _setup_bot(monkeypatch)
        try:
            cog = bot.get_cog("CoreOpsCog")
            assert cog is not None
            member = DummyMember(is_staff=True)
            ctx1 = HelpContext(bot, member)
            await cog.render_help(ctx1)
            ctx2 = HelpContext(bot, member)
            await cog.render_help(ctx2)
            return channel.messages
        finally:
            await bot.close()

    messages = asyncio.run(runner())
    assert len(messages) == 1


def test_help_diagnostics_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HELP_DIAGNOSTICS", "0")
    monkeypatch.delenv("HELP_DIAGNOSTICS_TTL_SEC", raising=False)

    async def runner() -> list[str | None]:
        bot, channel = await _setup_bot(monkeypatch)
        try:
            cog = bot.get_cog("CoreOpsCog")
            assert cog is not None
            ctx = HelpContext(bot, DummyMember(is_staff=True))
            await cog.render_help(ctx)
            return channel.messages
        finally:
            await bot.close()

    messages = asyncio.run(runner())
    assert messages == []
