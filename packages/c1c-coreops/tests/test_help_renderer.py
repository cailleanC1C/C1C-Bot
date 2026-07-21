import asyncio
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

from c1c_coreops.cog import CoreOpsCog  # noqa: E402
from cogs.recruitment_clan_profile import ClanProfileCog  # noqa: E402
from cogs.recruitment_member import RecruitmentMember  # noqa: E402
from cogs.recruitment_recruiter import RecruiterPanelCog  # noqa: E402
from cogs.recruitment_welcome import WelcomeBridge  # noqa: E402
from modules.ops.permissions_ui import PermissionsUICog  # noqa: E402


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


def _sheet_rows():
    from shared.sheets.help_commands import HelpCommandRow

    return (
        HelpCommandRow("woadkeeper", "clan", "!clan", "!clan <tag>", "Recruitment", "user", "Clan details", "Detailed clan help", 1, 0),
        HelpCommandRow("woadkeeper", "welcome", "!welcome", "!welcome", "Recruitment", "staff", "Welcome a recruit", "Detailed welcome help", 2, 1),
        HelpCommandRow("woadkeeper", "ops_config", "!ops config", "!ops config", "Recruitment", "admin", "Show config", "Detailed config help", 3, 2),
    )


def test_help_access_views_are_sheet_driven(monkeypatch: pytest.MonkeyPatch) -> None:
    async def rows():
        return _sheet_rows()

    monkeypatch.setattr("c1c_coreops.cog.help_commands.get_rows", rows)
    user = asyncio.run(_gather_help_embeds(monkeypatch, DummyMember()))[0]

    assert "!clan <tag>" in _fields(user)
    assert "!welcome" not in _fields(user)


def test_help_detail_uses_sheet_fields_and_normalized_lookup(monkeypatch: pytest.MonkeyPatch) -> None:
    async def runner() -> None:
        async def rows():
            return _sheet_rows()

        monkeypatch.setattr("c1c_coreops.cog.help_commands.get_rows", rows)
        bot = await _setup_test_bot(monkeypatch)
        try:
            ctx = HelpContext(bot, DummyMember())
            cog = bot.get_cog("CoreOpsCog")
            assert cog is not None
            await cog.render_help(ctx, query="!clan")
            fields = _fields(ctx._replies[0])
            assert fields["Usage"] == "!clan <tag>"
            assert fields["Details"] == "Detailed clan help"
            assert fields["Owning bot"] == "woadkeeper"
        finally:
            await bot.close()

    asyncio.run(runner())


def test_help_unavailable_is_an_embed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def rows():
        return None

    monkeypatch.setattr("c1c_coreops.cog.help_commands.get_rows", rows)
    embed = asyncio.run(_gather_help_embeds(monkeypatch, DummyMember()))[0]
    assert "temporarily unavailable" in (embed.title or "")


def test_all_commands_have_brief(monkeypatch: pytest.MonkeyPatch) -> None:
    async def runner() -> None:
        bot = await _setup_test_bot(monkeypatch)

        try:
            for command in bot.walk_commands():
                if getattr(command, "hidden", False):
                    continue
                description = getattr(command, "brief", None) or getattr(command, "help", None)
                assert isinstance(description, str) and description.strip(), command.qualified_name
        finally:
            await bot.close()

    asyncio.run(runner())
