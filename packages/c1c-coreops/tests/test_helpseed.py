import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from discord.ext import commands


def _ensure_src_on_path() -> None:
    root = Path(__file__).resolve().parents[3]
    src = root / "packages" / "c1c-coreops" / "src"
    import sys

    for item in (str(root), str(src)):
        if item not in sys.path:
            sys.path.insert(0, item)


_ensure_src_on_path()

import c1c_coreops.cog as coreops_cog  # noqa: E402
import c1c_coreops.rbac as coreops_rbac  # noqa: E402
from c1c_coreops.cog import (  # noqa: E402
    CoreOpsCog,
    _help_registry_command_key,
    _should_show,
)


class DummyMember:
    def __init__(self, *, administrator=False):
        self.guild_permissions = SimpleNamespace(administrator=administrator)
        self.roles = []
        self.id = 1 if administrator else 2


class DummyCtx:
    def __init__(self, *, administrator=True):
        self.replies = []
        self.guild = SimpleNamespace(id=1)
        self.author = DummyMember(administrator=administrator)
        self._coreops_suppress_denials = True

    async def reply(self, message, **kwargs):
        self.replies.append(str(message))


class FakeWorksheet:
    def __init__(self, rows):
        self.rows = [list(row) for row in rows]
        self.appended = []
        self.updated = []

    def get_all_values(self):
        return [list(row) for row in self.rows]

    def append_row(self, row, value_input_option="RAW"):
        self.appended.append((list(row), value_input_option))
        self.rows.append(list(row))

    def update(self, range_name, rows, value_input_option="RAW"):
        self.updated.append(
            (range_name, [list(row) for row in rows], value_input_option)
        )


HEADERS = list(coreops_cog.HELP_REGISTRY_HEADERS)


def _make_cog(bot):
    cog = CoreOpsCog.__new__(CoreOpsCog)
    cog.bot = bot
    cog._admin_bang_allowlist = set()
    return cog


def _command(name="sample", *, help_text="Detailed help", brief="Summary", extras=None):
    async def callback(ctx):
        return None

    cmd = commands.Command(
        callback, name=name, help=help_text, brief=brief, extras=extras or {}
    )
    return cmd


def _patch_sheet(monkeypatch, worksheet, tab="HelpCommands"):
    monkeypatch.setenv("RECRUITMENT_SHEET_ID", "sheet")
    monkeypatch.setattr(
        coreops_cog,
        "get_config_value_async",
        lambda *a, **k: asyncio.sleep(0, result=tab),
    )
    monkeypatch.setattr(
        coreops_cog,
        "aget_worksheet",
        lambda *a, **k: asyncio.sleep(0, result=worksheet),
    )
    monkeypatch.setattr(
        coreops_cog,
        "acall_with_backoff",
        lambda func, *a, **k: asyncio.sleep(0, result=func(*a, **k)),
    )


def test_helpseed_requires_config_key(monkeypatch):
    async def run():
        bot = SimpleNamespace(
            walk_commands=lambda: [
                _command(extras={"function_group": "general", "access_tier": "user"})
            ]
        )
        cog = _make_cog(bot)
        monkeypatch.setenv("RECRUITMENT_SHEET_ID", "sheet")
        monkeypatch.setattr(
            coreops_cog,
            "get_config_value_async",
            lambda *a, **k: asyncio.sleep(0, result=None),
        )
        with pytest.raises(RuntimeError, match="HELP_COMMANDS_TAB is required"):
            await cog._helpseed_impl(DummyCtx())

    asyncio.run(run())


def test_helpseed_missing_headers_fail(monkeypatch):
    async def run():
        bot = SimpleNamespace(walk_commands=lambda: [])
        cog = _make_cog(bot)
        worksheet = FakeWorksheet([["enabled", "bot_key"]])
        _patch_sheet(monkeypatch, worksheet)
        with pytest.raises(RuntimeError, match="missing required headers: command_key"):
            await cog._helpseed_impl(DummyCtx())

    asyncio.run(run())


def test_helpseed_existing_rows_preserve_manual_fields(monkeypatch):
    async def run():
        cmd = _command(extras={"function_group": "newcat", "access_tier": "admin"})
        bot = SimpleNamespace(walk_commands=lambda: [cmd])
        cog = _make_cog(bot)
        existing = [
            "TRUE",
            "woadkeeper",
            "sample",
            "!old",
            "!old",
            "manualcat",
            "staff",
            "manual summary",
            "manual details",
            "manual note",
            "9",
        ]
        worksheet = FakeWorksheet([HEADERS, existing])
        _patch_sheet(monkeypatch, worksheet)
        result = await cog._helpseed_impl(DummyCtx())
        assert result.updated == 1
        updated = worksheet.updated[0][1][0]
        assert updated[0] == "TRUE"
        assert updated[5] == "manualcat"
        assert updated[7] == "manual summary"
        assert updated[8] == "manual details"
        assert updated[9] == "manual note"
        assert updated[10] == "9"
        assert updated[6] == "admin"

    asyncio.run(run())


def test_helpseed_new_rows_append_disabled_blank_sort(monkeypatch):
    async def run():
        cmd = _command(extras={"function_group": "general", "access_tier": "user"})
        bot = SimpleNamespace(walk_commands=lambda: [cmd])
        cog = _make_cog(bot)
        worksheet = FakeWorksheet([HEADERS])
        _patch_sheet(monkeypatch, worksheet)
        result = await cog._helpseed_impl(DummyCtx())
        assert result.created == 1
        row = worksheet.appended[0][0]
        assert row[0] == "FALSE"
        assert row[10] == ""

    asyncio.run(run())


def test_helpseed_command_key_generation_grouped():
    assert _help_registry_command_key("ops ping") == "ops_ping"
    assert _help_registry_command_key("shards set") == "shards_set"


def test_helpseed_access_level_allowlist():
    cog = _make_cog(SimpleNamespace())
    assert (
        cog._build_help_seed_row(_command(extras={"access_tier": "staff"})).access_level
        == "staff"
    )
    assert (
        cog._build_help_seed_row(_command(extras={"access_tier": "owner"})).access_level
        == ""
    )


def test_helpseed_is_hidden_from_current_visible_help_overview():
    command = CoreOpsCog.ops_helpseed
    assert command.extras.get("hide_in_help") is True
    assert _should_show(command) is False


def test_visible_help_command_unchanged():
    assert CoreOpsCog.ops_help.callback.__name__ == "ops_help"
    assert getattr(CoreOpsCog, "ops_helpseed")


def test_helpseed_admin_check_allows_admin(monkeypatch):
    async def run():
        monkeypatch.setattr(coreops_rbac.discord, "Member", DummyMember)
        ctx = DummyCtx(administrator=True)
        for check in CoreOpsCog.ops_helpseed.checks:
            assert await check(ctx) is True

    asyncio.run(run())


def test_helpseed_admin_check_blocks_non_admin(monkeypatch):
    async def run():
        monkeypatch.setattr(coreops_rbac.discord, "Member", DummyMember)
        ctx = DummyCtx(administrator=False)
        failures = 0
        for check in CoreOpsCog.ops_helpseed.checks:
            try:
                await check(ctx)
            except commands.CheckFailure:
                failures += 1
        assert failures >= 1

    asyncio.run(run())
