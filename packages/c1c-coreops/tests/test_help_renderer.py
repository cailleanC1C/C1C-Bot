import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable, Mapping, Sequence

import discord
import pytest
from discord.ext import commands


def _ensure_src_on_path() -> None:
    root = Path(__file__).resolve().parents[3]
    src = root / "packages" / "c1c-coreops" / "src"
    root_str = str(root)
    src_str = str(src)
    sys_path = __import__("sys").path
    if root_str not in sys_path:
        sys_path.insert(0, root_str)
    if src_str not in sys_path:
        sys_path.insert(0, src_str)


def _resolve_member(target):
    if isinstance(target, commands.Context):
        return getattr(target, "author", None)
    return target


_ensure_src_on_path()

from c1c_coreops import help as help_module
from c1c_coreops.cog import CoreOpsCog, _HelpSectionConfig
from cogs.recruitment_clan_profile import ClanProfileCog
from cogs.recruitment_member import RecruitmentMember
from cogs.recruitment_recruiter import RecruiterPanelCog
from cogs.recruitment_welcome import WelcomeBridge
from modules.ops.permissions_ui import PermissionsUICog


class DummyMember:
    def __init__(self, *, is_admin: bool = False, is_staff: bool = False) -> None:
        self.display_name = "Member"
        self.id = 1 if is_admin else 2 if is_staff else 3
        self.roles: list[SimpleNamespace] = []
        self.guild_permissions = SimpleNamespace(administrator=is_admin)
        self._is_admin = is_admin
        self._is_staff = is_staff

    def __str__(self) -> str:  # pragma: no cover - defensive fallback
        return self.display_name


class HelpContext:
    def __init__(self, bot: commands.Bot, author: DummyMember) -> None:
        self.bot = bot
        self.author = author
        self.guild = SimpleNamespace(id=1234)
        self._coreops_suppress_denials = True
        self._replies: list[discord.Embed] = []
        self.command = None

    async def reply(
        self,
        *args: object,
        embed: discord.Embed | None = None,
        embeds: Sequence[discord.Embed] | None = None,
        **_: object,
    ) -> None:
        if embed is not None:
            self._replies.append(embed)
        if embeds:
            self._replies.extend(embeds)


@pytest.fixture(autouse=True)
def patch_rbac(monkeypatch: pytest.MonkeyPatch) -> Iterable[None]:
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


async def _setup_test_bot(
    monkeypatch: pytest.MonkeyPatch,
    *,
    show_empty: bool = False,
    allowlist: str | None = None,
) -> commands.Bot:
    if show_empty:
        monkeypatch.setenv("SHOW_EMPTY_SECTIONS", "1")
    else:
        monkeypatch.delenv("SHOW_EMPTY_SECTIONS", raising=False)

    if allowlist is None:
        monkeypatch.delenv("COREOPS_ADMIN_BANG_ALLOWLIST", raising=False)
    else:
        monkeypatch.setenv("COREOPS_ADMIN_BANG_ALLOWLIST", allowlist)

    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot.help_command = None

    await bot.add_cog(CoreOpsCog(bot))
    await bot.add_cog(PermissionsUICog(bot))
    await bot.add_cog(RecruiterPanelCog(bot))
    await bot.add_cog(WelcomeBridge(bot))
    await bot.add_cog(RecruitmentMember(bot))
    await bot.add_cog(ClanProfileCog(bot))

    return bot


async def _gather_help_embeds(
    monkeypatch: pytest.MonkeyPatch,
    member: DummyMember,
    *,
    show_empty: bool = False,
    allowlist: str | None = None,
) -> list[discord.Embed]:
    bot = await _setup_test_bot(
        monkeypatch, show_empty=show_empty, allowlist=allowlist
    )

    try:
        cog = bot.get_cog("CoreOpsCog")
        assert cog is not None
        ctx = HelpContext(bot, author=member)
        await cog.render_help(ctx)
        return ctx._replies
    finally:
        await bot.close()


def _fields(embed: discord.Embed) -> Mapping[str, str]:
    mapping: dict[str, str] = {}
    for field in getattr(embed, "fields", []):
        mapping[getattr(field, "name", "")] = getattr(field, "value", "")
    return mapping


def _collect_text(embed: discord.Embed) -> str:
    return " \n ".join(_fields(embed).values())


OVERVIEW_SNAPSHOT = (
    "**C1C-Recruitment keeps the doors open and the hearths warm.**  \n"
    "It’s how we find new clanmates, help old friends move up, and keep every hall filled with good company.\n\n"
    "**Members** can peek at which clans have room, check what’s needed to join or dig into details about any clan across the cluster.  \n\n"
    "**Recruiters** use it to spot open slots, match new arrivals and drop welcome notes so nobody gets lost on day one.  \n\n"
    "_All handled right here on Discord — fast, friendly, and stitched together with that usual C1C chaos and care._ \n\n"
    "**To learn what a command does, type like this:**  \n"
    "`C1C-Recruitment help ping` → shows info for `C1C-Recruitment ping`"
)


def test_help_admin_view(monkeypatch: pytest.MonkeyPatch) -> None:
    async def runner() -> None:
        embeds = await _gather_help_embeds(
            monkeypatch,
            DummyMember(is_admin=True, is_staff=True),
            allowlist="env,health,refresh all",
        )
        assert len(embeds) == 4
        overview, admin_embed, staff_embed, user_embed = embeds

        assert overview.title == "C1C-Recruitment — help"
        assert admin_embed.title == "Admin / Operational"
        assert staff_embed.title == "Staff"
        assert user_embed.title == "User"

        admin_text = _collect_text(admin_embed)
        staff_text = _collect_text(staff_embed)
        user_text = _collect_text(user_embed)

        assert "`!health`" in admin_text
        assert "`!refresh all`" in admin_text
        assert "`!perm`" in admin_text
        assert "`!welcome-refresh`" in admin_text
        assert "@Bot" not in admin_text
        assert "`!clan`" not in admin_text

        assert "`!clanmatch`" in staff_text
        assert "`!welcome`" in staff_text
        assert "`!ops digest`" in staff_text
        assert "`!welcome-refresh`" not in staff_text
        assert "`!refresh all`" not in staff_text

        assert "`!clan`" in user_text
        assert "`!clansearch`" in user_text
        assert "help" in user_text
        assert "ping" in user_text
        assert "@Bot" not in user_text
        assert "!ops" not in user_text

        combined_text = " \n ".join(
            value for embed in embeds for value in _fields(embed).values()
        )
        assert "!rec" not in combined_text

    asyncio.run(runner())


def test_help_staff_view(monkeypatch: pytest.MonkeyPatch) -> None:
    async def runner() -> None:
        embeds = await _gather_help_embeds(
            monkeypatch,
            DummyMember(is_staff=True),
            allowlist="env,health,refresh all",
        )
        assert len(embeds) == 3
        titles = {embed.title: embed for embed in embeds}
        assert "Admin / Operational" not in titles

        staff_embed = titles["Staff"]
        user_embed = titles["User"]

        staff_text = _collect_text(staff_embed)
        user_text = _collect_text(user_embed)

        assert "`!clanmatch`" in staff_text
        assert "`!welcome`" in staff_text
        assert "`!ops digest`" in staff_text
        assert "`!ops config`" not in staff_text
        assert "`!welcome-refresh`" not in staff_text
        assert "`!refresh all`" not in staff_text

        assert "`!clan`" in user_text
        assert "`!clansearch`" in user_text
        assert "help" in user_text
        assert "ping" in user_text
        assert "@Bot" not in user_text
        assert "!ops" not in user_text

    asyncio.run(runner())


def test_help_staff_view_with_empty_sections(monkeypatch: pytest.MonkeyPatch) -> None:
    async def runner() -> None:
        placeholder = _HelpSectionConfig("void", "Placeholder", ())
        staff_config = CoreOpsCog._HELP_AUDIENCE_CONFIGS["staff"]
        monkeypatch.setitem(
            CoreOpsCog._HELP_AUDIENCE_CONFIGS,
            "staff",
            replace(staff_config, sections=(placeholder,)),
        )

        embeds = await _gather_help_embeds(
            monkeypatch,
            DummyMember(is_staff=True),
            show_empty=True,
            allowlist="env,health,refresh all",
        )

        titles = {embed.title: embed for embed in embeds}
        assert "Staff" in titles
        staff_embed = titles["Staff"]

        fields = getattr(staff_embed, "fields", ())
        assert fields, "expected placeholder fields for empty staff tier"
        assert all(getattr(field, "value", "") == "Coming soon" for field in fields)

    asyncio.run(runner())


def test_help_user_view(monkeypatch: pytest.MonkeyPatch) -> None:
    async def runner() -> None:
        embeds = await _gather_help_embeds(
            monkeypatch,
            DummyMember(),
            allowlist="env,health,refresh all",
        )
        assert len(embeds) == 2
        titles = {embed.title: embed for embed in embeds}
        assert "Admin / Operational" not in titles
        assert "Staff" not in titles

        user_embed = titles["User"]

        user_text = _collect_text(user_embed)
        assert "`!clan`" in user_text
        assert "`!clansearch`" in user_text
        assert "help" in user_text
        assert "ping" in user_text
        assert "@Bot" not in user_text
        assert "!ops" not in user_text

    asyncio.run(runner())


def test_help_no_hardcoding(monkeypatch: pytest.MonkeyPatch) -> None:
    async def runner() -> None:
        called = False

        embeds: list[discord.Embed] = []

        async def gather() -> None:
            nonlocal called, embeds
            bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())

            await bot.add_cog(CoreOpsCog(bot))

            original_walk = bot.walk_commands

            def _walk_commands():
                nonlocal called
                called = True
                yield from original_walk()

            monkeypatch.setattr(bot, "walk_commands", _walk_commands)

            await bot.add_cog(PermissionsUICog(bot))
            await bot.add_cog(RecruiterPanelCog(bot))
            await bot.add_cog(WelcomeBridge(bot))
            await bot.add_cog(RecruitmentMember(bot))
            await bot.add_cog(ClanProfileCog(bot))

            try:
                ctx = HelpContext(bot, author=DummyMember(is_admin=True, is_staff=True))
                cog = bot.get_cog("CoreOpsCog")
                assert cog is not None
                await cog.render_help(ctx)
                embeds = ctx._replies
            finally:
                await bot.close()

        await gather()
        assert called, "walk_commands was not invoked"
        assert embeds, "expected help embeds"
        assert not hasattr(help_module, "HELP_COMMAND_REGISTRY")

    asyncio.run(runner())


def test_all_commands_have_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    async def runner() -> None:
        bot = await _setup_test_bot(monkeypatch)

        try:
            for command in bot.walk_commands():
                if getattr(command, "hidden", False):
                    continue
                brief = getattr(command, "brief", None)
                assert isinstance(brief, str) and brief.strip(), command.qualified_name
        finally:
            await bot.close()

    asyncio.run(runner())


def test_overview_text_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    async def runner() -> None:
        embeds = await _gather_help_embeds(
            monkeypatch,
            DummyMember(is_admin=True, is_staff=True),
        )
        assert embeds[0].description == OVERVIEW_SNAPSHOT

    asyncio.run(runner())
