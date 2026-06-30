from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import discord
from discord.ext import commands

import sys


def _ensure_src_on_path() -> None:
    root = Path(__file__).resolve().parents[3]
    src = root / "packages" / "c1c-coreops" / "src"
    root_str = str(root)
    src_str = str(src)
    if root_str not in sys.path:
        sys.path.insert(0, root_str)
    if src_str not in sys.path:
        sys.path.insert(0, src_str)


_ensure_src_on_path()

from c1c_coreops import cog as coreops_cog  # noqa: E402
from c1c_coreops.cog import CoreOpsCog  # noqa: E402


class DummyContext:
    def __init__(self) -> None:
        self.guild = SimpleNamespace(id=12345)
        self.sent: list[str] = []

    async def send(self, message: str, **_: object) -> None:
        self.sent.append(message)


def test_refresh_clansinfo_impl_is_ctx_free_for_internal_refresh(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        coreops_cog.cache_telemetry,
        "get_snapshot",
        lambda _name: SimpleNamespace(
            available=True,
            age_seconds=3600,
            next_refresh_delta_seconds=None,
            next_refresh_human=None,
        ),
    )

    refresh_calls: list[tuple[str, str]] = []

    async def refresh_now(name: str, *, actor: str):
        refresh_calls.append((name, actor))

    monkeypatch.setattr(coreops_cog.cache_telemetry, "refresh_now", refresh_now)

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        cog = CoreOpsCog(bot)
        try:
            message, queued = await cog._refresh_clansinfo_impl(guild=None)
            assert message == "Refreshing clans (background)."
            assert queued is True
            await asyncio.sleep(0)
            assert refresh_calls == [("clans", "internal")]
        finally:
            await bot.close()

    asyncio.run(runner())


def test_refresh_clansinfo_commands_own_ctx_and_user_reply(monkeypatch) -> None:
    calls: list[object] = []

    async def fake_impl(self, *, guild):
        assert guild is not None
        calls.append(guild)
        return "✅ Clans cache fresh (0m old).", False

    monkeypatch.setattr(CoreOpsCog, "_refresh_clansinfo_impl", fake_impl)

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        cog = CoreOpsCog(bot)
        refresh_ctx = DummyContext()
        ops_ctx = DummyContext()
        try:
            await CoreOpsCog.refresh_clansinfo.callback(cog, refresh_ctx)
            await CoreOpsCog.ops_refresh_clansinfo.callback(cog, ops_ctx)
            assert refresh_ctx.sent == ["✅ Clans cache fresh (0m old)."]
            assert ops_ctx.sent == ["✅ Clans cache fresh (0m old)."]
            assert calls == [refresh_ctx.guild, ops_ctx.guild]
        finally:
            await bot.close()

    asyncio.run(runner())


def test_refresh_clansinfo_command_permissions_and_text_are_preserved() -> None:
    refresh_command = CoreOpsCog.refresh_clansinfo
    ops_refresh_command = CoreOpsCog.ops_refresh_clansinfo

    assert refresh_command.name == "clansinfo"
    assert refresh_command.help == "Updates the clan info list."
    assert refresh_command.brief == "Updates the clan info list."
    assert getattr(refresh_command, "access_tier", None) == "admin"

    assert ops_refresh_command.name == "clansinfo"
    assert ops_refresh_command.help == "Updates the clan info list."
    assert ops_refresh_command.brief == "Updates the clan info list."
    assert getattr(ops_refresh_command, "access_tier", None) == "staff"
