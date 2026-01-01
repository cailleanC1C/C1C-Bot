"""Helpers for inspecting CoreOps help surfaces."""

from __future__ import annotations

import asyncio
import contextlib
import os
from dataclasses import dataclass
from types import SimpleNamespace
from typing import AsyncIterator, Iterator, Sequence

import discord
from discord.ext import commands

from c1c_coreops import rbac
from c1c_coreops.cog import CoreOpsCog
from cogs.recruitment_clan_profile import ClanProfileCog
from cogs.recruitment_member import RecruitmentMember
from cogs.recruitment_recruiter import RecruiterPanelCog
from cogs.recruitment_welcome import WelcomeBridge
from modules.ops.permissions_ui import PermissionsUICog
from shared.testing.environment import apply_required_test_environment

__all__ = [
    "HelpCommandSummary",
    "HelpSurfaceSection",
    "build_admin_help_surface",
    "build_admin_help_surface_async",
]


@dataclass(frozen=True)
class HelpCommandSummary:
    """Summarised command entry rendered in a help surface."""

    usage: str
    description: str


@dataclass(frozen=True)
class HelpSurfaceSection:
    """Section rendered in a help surface embed."""

    label: str
    commands: tuple[HelpCommandSummary, ...]


class _HelpContext:
    """Minimal context object that captures replies from ``CoreOpsCog``."""

    __slots__ = ("bot", "author", "guild", "command", "_replies", "_coreops_suppress_denials")

    def __init__(self, bot: commands.Bot, author: object) -> None:
        self.bot = bot
        self.author = author
        self.guild = SimpleNamespace(id=1234)
        self.command = None
        self._replies: list[discord.Embed] = []
        self._coreops_suppress_denials = True

    async def reply(  # type: ignore[override]
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


class _DummyMember:
    __slots__ = ("display_name", "id", "roles", "guild_permissions", "_is_admin", "_is_staff")

    def __init__(self, *, is_admin: bool = False, is_staff: bool = False) -> None:
        self.display_name = "Member"
        self.id = 1 if is_admin else 2 if is_staff else 3
        self.roles: list[SimpleNamespace] = []
        self.guild_permissions = SimpleNamespace(administrator=is_admin)
        self._is_admin = is_admin
        self._is_staff = is_staff


@contextlib.contextmanager
def _override_environment(**entries: str | None) -> Iterator[None]:
    previous: dict[str, str] = {}
    removed: set[str] = set()
    try:
        for key, value in entries.items():
            if key in os.environ:
                previous[key] = os.environ[key]
            else:
                removed.add(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            os.environ[key] = value
        for key in removed:
            os.environ.pop(key, None)


@contextlib.contextmanager
def _patched_help_guards() -> Iterator[None]:
    """Patch RBAC helpers so dummy members satisfy access checks."""

    def _resolve_member(target):
        if isinstance(target, commands.Context):
            return getattr(target, "author", None)
        return target

    original_items: list[tuple[object, str, object]] = []

    def _swap(obj: object, name: str, replacement: object) -> None:
        original_items.append((obj, name, getattr(obj, name)))
        setattr(obj, name, replacement)

    _swap(rbac, "get_admin_role_ids", lambda: set())
    _swap(rbac, "get_staff_role_ids", lambda: set())
    _swap(rbac, "_resolve_member", _resolve_member)
    _swap(rbac, "_member_has_admin_role", lambda member: bool(getattr(member, "_is_admin", False)))
    _swap(
        rbac,
        "_has_administrator_permission",
        lambda member: bool(
            getattr(getattr(member, "guild_permissions", None), "administrator", False)
        ),
    )
    _swap(rbac, "is_admin_member", lambda target: bool(getattr(_resolve_member(target), "_is_admin", False)))
    _swap(
        rbac,
        "is_staff_member",
        lambda target: bool(
            getattr(_resolve_member(target), "_is_staff", False)
            or getattr(_resolve_member(target), "_is_admin", False)
        ),
    )
    _swap(rbac, "is_recruiter", lambda target: bool(getattr(_resolve_member(target), "_is_staff", False)))
    _swap(rbac, "is_lead", lambda target: bool(getattr(_resolve_member(target), "_is_staff", False)))
    _swap(
        rbac,
        "ops_gate",
        lambda member: bool(getattr(member, "_is_admin", False) or getattr(member, "_is_staff", False)),
    )
    _swap(rbac, "can_view_admin", lambda target: bool(getattr(_resolve_member(target), "_is_admin", False)))
    _swap(
        rbac,
        "can_view_staff",
        lambda target: bool(
            getattr(_resolve_member(target), "_is_staff", False)
            or getattr(_resolve_member(target), "_is_admin", False)
        ),
    )

    import c1c_coreops.cog as cog_module  # late import to reuse patched functions

    _swap(cog_module, "can_view_admin", rbac.can_view_admin)
    _swap(cog_module, "can_view_staff", rbac.can_view_staff)

    import cogs.recruitment_welcome as welcome_module

    _swap(welcome_module, "is_staff_member", rbac.is_staff_member)
    _swap(welcome_module, "is_admin_member", rbac.is_admin_member)

    import c1c_coreops.rbac as rbac_module
    import c1c_coreops.cog as cog_mod

    _swap(discord, "Member", _DummyMember)
    _swap(rbac_module.discord, "Member", _DummyMember)
    _swap(cog_mod.discord, "Member", _DummyMember)

    try:
        yield
    finally:
        for obj, name, original in reversed(original_items):
            setattr(obj, name, original)


async def _setup_help_bot() -> commands.Bot:
    bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
    bot.help_command = None
    await bot.add_cog(CoreOpsCog(bot))
    await bot.add_cog(PermissionsUICog(bot))
    await bot.add_cog(RecruiterPanelCog(bot))
    await bot.add_cog(WelcomeBridge(bot))
    await bot.add_cog(RecruitmentMember(bot))
    await bot.add_cog(ClanProfileCog(bot))
    return bot


def _parse_field(field: discord.EmbedProxy) -> HelpSurfaceSection:
    commands: list[HelpCommandSummary] = []
    for line in field.value.splitlines():
        stripped = line.strip()
        if not stripped.startswith("• "):
            continue
        body = stripped[2:].strip()
        if " — " in body:
            usage, description = body.split(" — ", 1)
        else:
            usage, description = body, ""
        commands.append(
            HelpCommandSummary(
                usage=usage.strip().strip("`"),
                description=description.strip(),
            )
        )
    return HelpSurfaceSection(label=field.name, commands=tuple(commands))


async def build_admin_help_surface_async(
    *, allowlist: str | None = None
) -> tuple[HelpSurfaceSection, ...]:
    """Return the admin help surface sections for CoreOps."""

    apply_required_test_environment()

    async with _help_bot_context(allowlist=allowlist) as embeds:
        for embed in embeds:
            if embed.title == "Admin / Operational":
                return tuple(_parse_field(field) for field in embed.fields)
    raise RuntimeError("Admin help surface not available")


def build_admin_help_surface(*, allowlist: str | None = None) -> tuple[HelpSurfaceSection, ...]:
    """Synchronously gather the admin help surface sections."""

    return asyncio.run(build_admin_help_surface_async(allowlist=allowlist))


@contextlib.asynccontextmanager
async def _help_bot_context(*, allowlist: str | None) -> AsyncIterator[list[discord.Embed]]:
    if allowlist is None:
        manager = contextlib.nullcontext()
    else:
        manager = _override_environment(COREOPS_ADMIN_BANG_ALLOWLIST=allowlist)

    with manager, _patched_help_guards():
        bot = await _setup_help_bot()
        try:
            ctx = _HelpContext(bot, _DummyMember(is_admin=True, is_staff=True))
            cog = bot.get_cog("CoreOpsCog")
            if cog is None:
                raise RuntimeError("CoreOpsCog missing")
            await cog.render_help(ctx)
            yield list(ctx._replies)
        finally:
            await bot.close()
