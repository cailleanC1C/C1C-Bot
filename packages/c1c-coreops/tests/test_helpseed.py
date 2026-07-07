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
        self.appended_batches = []
        self.updated = []
        self.updated_batches = []
        self.get_all_values_calls = 0

    def get_all_values(self):
        self.get_all_values_calls += 1
        return [list(row) for row in self.rows]

    def append_row(self, row, value_input_option="RAW"):
        self.appended.append((list(row), value_input_option))
        self.rows.append(list(row))

    def append_rows(self, rows, value_input_option="RAW"):
        rows = [list(row) for row in rows]
        self.appended_batches.append((rows, value_input_option))
        self.rows.extend(rows)

    def update(self, range_name, rows, value_input_option="RAW"):
        self.updated.append(
            (range_name, [list(row) for row in rows], value_input_option)
        )

    def batch_update(self, updates, value_input_option="RAW"):
        self.updated_batches.append((updates, value_input_option))


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

    async def fake_get_config_value_async(*args, **kwargs):
        assert kwargs.get("force") is not True
        return tab

    monkeypatch.setattr(
        coreops_cog,
        "get_config_value_async",
        fake_get_config_value_async,
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
        assert worksheet.get_all_values_calls == 1
        assert worksheet.updated == []
        updated = worksheet.updated_batches[0][0][0]["values"][0]
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
        assert worksheet.get_all_values_calls == 1
        assert worksheet.appended == []
        row = worksheet.appended_batches[0][0][0]
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


def test_helpseed_batches_multiple_created_rows(monkeypatch):
    async def run():
        bot = SimpleNamespace(
            walk_commands=lambda: [
                _command("alpha", extras={"function_group": "general", "access_tier": "user"}),
                _command("beta", extras={"function_group": "general", "access_tier": "user"}),
            ]
        )
        cog = _make_cog(bot)
        worksheet = FakeWorksheet([HEADERS])
        _patch_sheet(monkeypatch, worksheet)
        result = await cog._helpseed_impl(DummyCtx())
        assert result.created == 2
        assert worksheet.appended == []
        assert len(worksheet.appended_batches) == 1
        assert len(worksheet.appended_batches[0][0]) == 2

    asyncio.run(run())


def test_bare_helpseed_is_hidden_admin_maintenance_alias():
    command = CoreOpsCog.helpseed
    assert command.hidden is True
    assert command.extras.get("hide_in_help") is True
    assert command.extras.get("access_tier") == "hidden"
    assert command.extras.get("help_flags") == ["hidden", "maintenance"]
    assert _should_show(command) is False


def test_bare_helpseed_delegates_to_same_command_impl(monkeypatch):
    async def run():
        bot = SimpleNamespace(walk_commands=lambda: [])
        cog = _make_cog(bot)
        ctx = DummyCtx()
        calls = []

        async def fake_impl(received_ctx):
            calls.append(received_ctx)
            await received_ctx.reply("seeded")

        monkeypatch.setattr(cog, "_helpseed_impl", fake_impl)
        await CoreOpsCog.helpseed.callback(cog, ctx)
        assert calls == [ctx]
        assert ctx.replies == ["seeded"]

    asyncio.run(run())


def test_ops_helpseed_delegates_to_same_command_impl(monkeypatch):
    async def run():
        bot = SimpleNamespace(walk_commands=lambda: [])
        cog = _make_cog(bot)
        ctx = DummyCtx()
        calls = []

        async def fake_impl(received_ctx):
            calls.append(received_ctx)

        monkeypatch.setattr(cog, "_helpseed_impl", fake_impl)
        await CoreOpsCog.ops_helpseed.callback(cog, ctx)
        assert calls == [ctx]

    asyncio.run(run())


def test_bare_helpseed_admin_check_blocks_non_admin(monkeypatch):
    async def run():
        monkeypatch.setattr(coreops_rbac.discord, "Member", DummyMember)
        ctx = DummyCtx(administrator=False)
        failures = 0
        for check in CoreOpsCog.helpseed.checks:
            try:
                await check(ctx)
            except commands.CheckFailure:
                failures += 1
        assert failures >= 1

    asyncio.run(run())


def test_bare_helpseed_is_not_exported_into_helpcommands(monkeypatch):
    async def run():
        bot = SimpleNamespace(walk_commands=lambda: [CoreOpsCog.helpseed])
        cog = _make_cog(bot)
        worksheet = FakeWorksheet([HEADERS])
        _patch_sheet(monkeypatch, worksheet)
        result = await cog._helpseed_impl(DummyCtx())
        assert result.created == 0
        assert result.skipped == 1
        assert worksheet.appended_batches == []

    asyncio.run(run())


def test_helpseed_rate_limit_reply_is_friendly(monkeypatch):
    async def run():
        bot = SimpleNamespace(walk_commands=lambda: [])
        cog = _make_cog(bot)
        ctx = DummyCtx()

        async def fail(_ctx):
            raise RuntimeError('429 RESOURCE_EXHAUSTED ReadRequestsPerMinutePerUser traceback details')

        monkeypatch.setattr(cog, "_helpseed_impl", fail)
        await CoreOpsCog.ops_helpseed.callback(cog, ctx)
        assert ctx.replies == [
            "⚠️ Help registry seed hit Google Sheets rate limits. Wait a minute and try again."
        ]

    asyncio.run(run())


def test_helpseed_missing_append_rows_fails_without_append_row_fallback(monkeypatch):
    async def run():
        class NoAppendRowsWorksheet(FakeWorksheet):
            append_rows = None

        cmd = _command(extras={"function_group": "general", "access_tier": "user"})
        bot = SimpleNamespace(walk_commands=lambda: [cmd])
        cog = _make_cog(bot)
        worksheet = NoAppendRowsWorksheet([HEADERS])
        _patch_sheet(monkeypatch, worksheet)
        with pytest.raises(RuntimeError, match="requires worksheet.append_rows"):
            await cog._helpseed_impl(DummyCtx())
        assert worksheet.appended == []

    asyncio.run(run())


def test_helpseed_missing_batch_update_fails_without_update_fallback(monkeypatch):
    async def run():
        class NoBatchUpdateWorksheet(FakeWorksheet):
            batch_update = None

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
        worksheet = NoBatchUpdateWorksheet([HEADERS, existing])
        _patch_sheet(monkeypatch, worksheet)
        with pytest.raises(RuntimeError, match="requires worksheet.batch_update"):
            await cog._helpseed_impl(DummyCtx())
        assert worksheet.updated == []

    asyncio.run(run())


def test_helpseed_fills_blank_curated_fields_from_metadata(monkeypatch):
    async def run():
        cmd = _command(
            extras={"function_group": "newcat", "access_tier": "admin"},
            brief="Generated summary",
            help_text="Generated details",
        )
        bot = SimpleNamespace(walk_commands=lambda: [cmd])
        cog = _make_cog(bot)
        existing = [
            "TRUE",
            "woadkeeper",
            "sample",
            "!old",
            "!old",
            "",
            "staff",
            "",
            "",
            "manual note",
            "7",
        ]
        worksheet = FakeWorksheet([HEADERS, existing])
        _patch_sheet(monkeypatch, worksheet)
        result = await cog._helpseed_impl(DummyCtx())
        assert result.updated == 1
        updated = worksheet.updated_batches[0][0][0]["values"][0]
        assert updated[5] == "newcat"
        assert updated[7] == "Generated summary"
        assert updated[8] == "Generated details"
        assert updated[9] == "manual note"
        assert updated[10] == "7"

    asyncio.run(run())


def test_touched_woadkeeper_command_metadata_is_useful() -> None:
    from cogs.app_admin import AppAdmin
    from cogs.clanrole_management import ClanRoleManagementCog
    from cogs.housekeeping_mirralith import MirralithOverviewCog
    from cogs.recruitment_clan_ads import ClanAdsCog
    from cogs.recruitment_member import RecruitmentMember
    from cogs.recruitment_reporting import RecruitmentReporting
    from cogs.recruitment_welcome import WelcomeBridge
    from modules.community.fusion.cog import FusionCog
    from modules.community.leagues.cog import LeaguesCog
    from modules.community.reaction_roles import ReactionRolesCog
    from modules.coreops.cmd_cfg import ConfigCmd
    from modules.housekeeping.cleanup import CleanupCog
    from modules.ops.permissions_ui import PermissionsUICog

    commands_by_name = {
        "cfg": ConfigCmd.cfg_cmd,
        "fusion": FusionCog.fusion,
        "titan": FusionCog.titan,
        "clanrole": ClanRoleManagementCog.clanrole,
        "clanads": ClanAdsCog.clanads,
        "next": AppAdmin.next_jobs,
        "servermap": AppAdmin.servermap,
        "whoweare": AppAdmin.whoweare,
        "report": RecruitmentReporting.report_group,
        "roleaudit": RecruitmentReporting.roleaudit,
        "cleanup": CleanupCog.cleanup,
        "welcome-refresh": WelcomeBridge.welcome_refresh,
        "mirralith": MirralithOverviewCog.mirralith_group,
        "leagues": LeaguesCog.leagues,
        "reactrole": ReactionRolesCog.reactrole_cmd,
        "perm": PermissionsUICog.perm,
        "clansearch": RecruitmentMember.clansearch,
        "ops health": CoreOpsCog.ops_health,
        "ops checksheet": CoreOpsCog.ops_checksheet,
        "ops config": CoreOpsCog.ops_config,
        "ops refresh": CoreOpsCog.ops_refresh,
        "ops refresh all": CoreOpsCog.ops_refresh_all,
        "ops reload": CoreOpsCog.ops_reload,
    }
    required_terms = {
        "reactrole": ["<key>", "mapping key"],
        "ops refresh": ["bucket", "registered"],
        "ops reload": ["--reboot", "Unknown flags"],
        "next": ["component", "scheduled jobs"],
        "roleaudit": ["preview", "apply CONFIRM"],
        "cleanup": ["run", "writeback"],
        "report": ["recruiters", "all"],
        "clansearch": ["takes no arguments", "!clan <tag or name>"],
    }

    cog = _make_cog(SimpleNamespace())
    for name, command in commands_by_name.items():
        row = cog._build_help_seed_row(command)
        details = row.details
        assert row.summary, name
        assert details and len(details) >= 40, name
        assert row.usage.startswith("!"), name
        for term in required_terms.get(name, []):
            assert term in f"{row.usage} {row.summary} {details}", name
