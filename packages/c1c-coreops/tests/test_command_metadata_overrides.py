import asyncio
from pathlib import Path
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

from c1c_coreops.cog import CoreOpsCog  # noqa: E402  pylint: disable=wrong-import-position
from modules.ops.permissions_ui import PermissionsUICog  # noqa: E402  pylint: disable=wrong-import-position
from cogs.recruitment_clan_profile import ClanProfileCog  # noqa: E402  pylint: disable=wrong-import-position
from cogs.recruitment_member import RecruitmentMember  # noqa: E402  pylint: disable=wrong-import-position
from cogs.recruitment_recruiter import RecruiterPanelCog  # noqa: E402  pylint: disable=wrong-import-position
from cogs.recruitment_welcome import WelcomeBridge  # noqa: E402  pylint: disable=wrong-import-position
from cogs.app_admin import AppAdmin  # noqa: E402  pylint: disable=wrong-import-position


@pytest.fixture(autouse=True)
def patch_rbac(monkeypatch: pytest.MonkeyPatch) -> Iterable[None]:
    monkeypatch.setattr("c1c_coreops.rbac.get_admin_role_ids", lambda: set())
    monkeypatch.setattr("c1c_coreops.rbac.get_staff_role_ids", lambda: set())
    yield


def test_operational_metadata_exposed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COREOPS_ENABLE_GENERIC_ALIASES", "1")

    async def runner() -> None:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        await bot.add_cog(CoreOpsCog(bot))
        await bot.add_cog(PermissionsUICog(bot))
        await bot.add_cog(RecruiterPanelCog(bot))
        await bot.add_cog(WelcomeBridge(bot))
        await bot.add_cog(RecruitmentMember(bot))
        await bot.add_cog(ClanProfileCog(bot))
        await bot.add_cog(AppAdmin(bot))

        try:
            operational_pairs = [
                ("env", "operational"),
                ("ops env", "operational"),
                ("health", "operational"),
                ("ops health", "operational"),
                ("digest", "operational"),
                ("ops digest", "operational"),
                ("config", "operational"),
                ("ops config", "operational"),
                ("checksheet", "operational"),
                ("ops checksheet", "operational"),
                ("ping", "operational"),
                ("ops ping", "operational"),
                ("reload", "operational"),
                ("ops reload", "operational"),
            ]

            for qualified_name, expected_group in operational_pairs:
                command = bot.get_command(qualified_name)
                assert command is not None, qualified_name
                actual_group = getattr(command, "function_group", None)
                assert actual_group == expected_group, (
                    f"{qualified_name} function_group={actual_group!r} extras={getattr(command, 'extras', None)!r}"
                )

            assert getattr(bot.get_command("ops refresh clansinfo"), "access_tier", None) == "staff"
            assert getattr(bot.get_command("ops config"), "access_tier", None) == "admin"
            assert getattr(bot.get_command("ops checksheet"), "access_tier", None) == "admin"
            assert getattr(bot.get_command("ops refresh"), "access_tier", None) == "admin"
            assert getattr(bot.get_command("ops refresh all"), "access_tier", None) == "admin"
        finally:
            await bot.close()

    asyncio.run(runner())
