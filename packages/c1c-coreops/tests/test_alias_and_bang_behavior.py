from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Iterable

import discord
import pytest
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

from c1c_coreops.cog import CoreOpsCog
from c1c_coreops.config import (
    build_command_variants,
    build_lookup_sequence,
    load_coreops_settings,
)


class DummyMember:
    def __init__(self, *, administrator: bool) -> None:
        self.roles: list[SimpleNamespace] = []
        self.guild_permissions = SimpleNamespace(administrator=administrator)


class DummyContext:
    def __init__(self, *, author: DummyMember) -> None:
        self.author = author
        self.guild = object()
        self._coreops_suppress_denials = True
        self._replies: list[tuple[tuple, dict]] = []

    async def reply(self, *args, **kwargs):
        self._replies.append((args, kwargs))


async def _run_checks(command: commands.Command, ctx: DummyContext) -> None:
    for check in command.checks:
        await check(ctx)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def patch_rbac_member(monkeypatch: pytest.MonkeyPatch) -> Iterable[None]:
    monkeypatch.setattr("c1c_coreops.rbac.discord.Member", DummyMember)
    monkeypatch.setattr("c1c_coreops.cog.discord.Member", DummyMember)
    yield


def test_multi_bot_alias_behavior(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TAG", "rec")
    monkeypatch.setenv("COREOPS_ENABLE_TAGGED_ALIASES", "1")
    monkeypatch.setenv("COREOPS_ENABLE_GENERIC_ALIASES", "0")
    monkeypatch.setenv(
        "COREOPS_ADMIN_BANG_ALLOWLIST",
        "env,reload,health,digest,checksheet,config,help,ping,refresh all",
    )

    async def runner() -> None:
        intents = discord.Intents.none()
        bot = commands.Bot(command_prefix="!", intents=intents)
        bot.remove_command("help")
        await bot.add_cog(CoreOpsCog(bot))

        try:
            env_command = bot.get_command("env")
            assert env_command is not None
            ops_env = bot.get_command("ops env")
            assert ops_env is not None

            non_admin_ctx = DummyContext(author=DummyMember(administrator=False))
            with pytest.raises(commands.CheckFailure):
                await _run_checks(ops_env, non_admin_ctx)
            with pytest.raises(commands.CheckFailure):
                await _run_checks(env_command, non_admin_ctx)

            admin_ctx = DummyContext(author=DummyMember(administrator=True))
            await _run_checks(ops_env, admin_ctx)
            await _run_checks(env_command, admin_ctx)

            settings = load_coreops_settings()
            variants = build_command_variants(settings, "env")
            assert "ops env" in variants
            assert variants[0] == "env"
        finally:
            await bot.close()

    asyncio.run(runner())


def test_generic_alias_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TAG", "rec")
    monkeypatch.setenv("COREOPS_ENABLE_TAGGED_ALIASES", "1")
    monkeypatch.setenv("COREOPS_ENABLE_GENERIC_ALIASES", "0")

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        bot.remove_command("help")
        await bot.add_cog(CoreOpsCog(bot))

        try:
            ops_digest = bot.get_command("ops digest")
            assert ops_digest is not None
            digest = bot.get_command("digest")
            assert digest is not None

            non_admin_ctx = DummyContext(author=DummyMember(administrator=False))
            with pytest.raises(commands.CheckFailure):
                await _run_checks(digest, non_admin_ctx)

            admin_ctx = DummyContext(author=DummyMember(administrator=True))
            await _run_checks(ops_digest, admin_ctx)
            await _run_checks(digest, admin_ctx)
        finally:
            await bot.close()

    asyncio.run(runner())


def test_allowlist_blocks_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TAG", "rec")
    monkeypatch.setenv("COREOPS_ENABLE_TAGGED_ALIASES", "1")
    monkeypatch.setenv("COREOPS_ENABLE_GENERIC_ALIASES", "0")
    monkeypatch.setenv(
        "COREOPS_ADMIN_BANG_ALLOWLIST",
        "env,reload,health,digest,checksheet,config,help,ping,refresh all",
    )

    settings = load_coreops_settings()
    assert "env" in settings.admin_bang_base_commands
    assert "foo" not in settings.admin_bang_base_commands


def test_reload_requires_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TAG", "rec")
    monkeypatch.setenv("COREOPS_ENABLE_TAGGED_ALIASES", "1")
    monkeypatch.setenv("COREOPS_ENABLE_GENERIC_ALIASES", "1")

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        bot.remove_command("help")
        await bot.add_cog(CoreOpsCog(bot))

        try:
            command = bot.get_command("reload")
            assert command is not None
            non_admin_ctx = DummyContext(author=DummyMember(administrator=False))
            with pytest.raises(commands.CheckFailure):
                await _run_checks(command, non_admin_ctx)
            admin_ctx = DummyContext(author=DummyMember(administrator=True))
            await _run_checks(command, admin_ctx)
        finally:
            await bot.close()

    asyncio.run(runner())


def test_build_lookup_sequence_prioritizes_base() -> None:
    candidates = build_lookup_sequence("digest", "--debug")
    assert candidates[0] == "digest"
    assert "digest --debug" in candidates


def test_build_lookup_sequence_includes_multi_word_command() -> None:
    candidates = build_lookup_sequence("refresh", "all")
    assert candidates[0] == "refresh"
    assert candidates[1] == "refresh all"
