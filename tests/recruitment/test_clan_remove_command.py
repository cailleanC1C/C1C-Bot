from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import asyncio
import pytest
from discord.ext import commands

from cogs import recruitment_clan_profile
from cogs.clanrole_management import ClanRoleManagementCog, ClanRoleRemoveView, ClanRoleTargetView, get_member_clan_roles, is_authorized_clan_role_manager, setup


class DummyRole:
    def __init__(self, rid: int, name: str):
        self.id = rid
        self.name = name
        self.mention = f"@{name}"


class DummyMember:
    def __init__(self, mid: int, roles: list[DummyRole], guild, admin: bool = False, *, display_name: str | None = None, name: str | None = None, global_name: str | None = None):
        self.id = mid
        self.roles = roles
        self.guild = guild
        self.mention = f"<@{mid}>"
        self.display_name = display_name or f"display-{mid}"
        self.name = name or f"user-{mid}"
        self.global_name = global_name
        self.guild_permissions = SimpleNamespace(administrator=admin)
        self.remove_roles = AsyncMock(side_effect=self._remove)
        self.add_roles = AsyncMock(side_effect=self._add)

    async def _remove(self, role, reason=None):
        if role in self.roles:
            self.roles.remove(role)

    async def _add(self, role, reason=None):
        if role not in self.roles:
            self.roles.append(role)


@pytest.fixture
def roles():
    return {
        "clan1": DummyRole(1, "ClanOne"),
        "clan2": DummyRole(2, "ClanTwo"),
        "raid": DummyRole(3, "Raid"),
        "wander": DummyRole(4, "Wandering Souls"),
        "mgr": DummyRole(10, "Manager"),
    }


def _ctx(author):
    return SimpleNamespace(author=author, guild=SimpleNamespace(id=1), reply=AsyncMock())


def test_authorized_by_admin(roles, monkeypatch):
    member = DummyMember(10, [roles["clan1"]], SimpleNamespace(), admin=True)
    assert is_authorized_clan_role_manager(member)


def test_authorized_by_clan_lead_ids(roles, monkeypatch):
    monkeypatch.setattr("cogs.clanrole_management.config.get_admin_role_ids", lambda: set())
    monkeypatch.setattr("cogs.clanrole_management.config.get_staff_role_ids", lambda: set())
    monkeypatch.setattr("cogs.clanrole_management.config.get_lead_role_ids", lambda: set())
    monkeypatch.setattr("cogs.clanrole_management.config.get_recruiter_role_ids", lambda: set())
    monkeypatch.setattr("cogs.clanrole_management.config.get_clan_lead_ids", lambda: {10})
    member = DummyMember(10, [roles["mgr"]], SimpleNamespace(), admin=False)
    assert is_authorized_clan_role_manager(member)


def test_get_member_clan_roles(roles):
    member = DummyMember(10, [roles["clan1"], roles["raid"]], SimpleNamespace())
    found = get_member_clan_roles(member, {1, 2})
    assert [r.id for r in found] == [1]


def test_cleanup_removes_raid_adds_wander(roles, monkeypatch):
    guild = SimpleNamespace(get_role=lambda rid: {3: roles["raid"], 4: roles["wander"]}.get(rid))
    member = DummyMember(42, [roles["clan1"], roles["raid"]], guild)
    author = DummyMember(1, [roles["mgr"]], guild)
    ctx = _ctx(author)
    cog = ClanRoleManagementCog()
    monkeypatch.setattr("cogs.clanrole_management.config.get_clan_role_ids", lambda: {1, 2})
    monkeypatch.setattr("cogs.clanrole_management.config.get_raid_role_id", lambda: 3)
    monkeypatch.setattr("cogs.clanrole_management.config.get_wandering_souls_role_id", lambda: 4)
    msg = asyncio.run(cog.apply_clan_removal_cleanup(ctx, member, roles["clan1"]))
    assert "Removed Raid" in msg and "Added Wandering Souls" in msg


def test_cleanup_keeps_raid_if_other_clan(roles, monkeypatch):
    guild = SimpleNamespace(get_role=lambda rid: {3: roles["raid"], 4: roles["wander"]}.get(rid))
    member = DummyMember(42, [roles["clan1"], roles["clan2"], roles["raid"]], guild)
    ctx = _ctx(DummyMember(1, [roles["mgr"]], guild))
    cog = ClanRoleManagementCog()
    monkeypatch.setattr("cogs.clanrole_management.config.get_clan_role_ids", lambda: {1, 2})
    monkeypatch.setattr("cogs.clanrole_management.config.get_raid_role_id", lambda: 3)
    monkeypatch.setattr("cogs.clanrole_management.config.get_wandering_souls_role_id", lambda: 4)
    msg = asyncio.run(cog.apply_clan_removal_cleanup(ctx, member, roles["clan1"]))
    assert "Raid kept because another clan role remains" in msg


def test_cleanup_missing_raid_existing_wander(roles, monkeypatch):
    guild = SimpleNamespace(get_role=lambda rid: {3: roles["raid"], 4: roles["wander"]}.get(rid))
    member = DummyMember(42, [roles["clan1"], roles["wander"]], guild)
    ctx = _ctx(DummyMember(1, [roles["mgr"]], guild))
    cog = ClanRoleManagementCog()
    monkeypatch.setattr("cogs.clanrole_management.config.get_clan_role_ids", lambda: {1, 2})
    monkeypatch.setattr("cogs.clanrole_management.config.get_raid_role_id", lambda: 3)
    monkeypatch.setattr("cogs.clanrole_management.config.get_wandering_souls_role_id", lambda: 4)
    msg = asyncio.run(cog.apply_clan_removal_cleanup(ctx, member, roles["clan1"]))
    assert "Raid was already absent" in msg and "already present" in msg


def test_multiple_roles_view_restricts_invoker(roles):
    cog = ClanRoleManagementCog()
    guild = SimpleNamespace()
    author = DummyMember(1, [roles["mgr"]], guild)
    target = DummyMember(2, [roles["clan1"], roles["clan2"]], guild)
    view = ClanRoleRemoveView(cog, _ctx(author), target, [roles["clan1"], roles["clan2"]])
    interaction = SimpleNamespace(user=SimpleNamespace(id=99), response=SimpleNamespace(send_message=AsyncMock()))
    ok = asyncio.run(view.interaction_check(interaction))
    assert ok is False


def test_target_view_restricts_invoker(roles):
    cog = ClanRoleManagementCog()
    guild = SimpleNamespace()
    author = DummyMember(1, [roles["mgr"]], guild)
    target = DummyMember(2, [roles["clan1"]], guild)
    view = ClanRoleTargetView(cog, _ctx(author), [target])
    interaction = SimpleNamespace(user=SimpleNamespace(id=99), response=SimpleNamespace(send_message=AsyncMock()))
    ok = asyncio.run(view.interaction_check(interaction))
    assert ok is False


def test_resolve_member_query_exact_name_preferred_over_startswith(roles):
    guild = SimpleNamespace(
        members=[
            DummyMember(1, [], None, display_name="Smurfette", name="smurfette"),
            DummyMember(2, [], None, display_name="Smurf", name="smurf"),
        ],
        get_member=lambda mid: None,
        fetch_member=AsyncMock(),
    )
    for member in guild.members:
        member.guild = guild
    ctx = SimpleNamespace(guild=guild)
    found = asyncio.run(ClanRoleManagementCog().resolve_member_query(ctx, "smurf"))
    assert [m.id for m in found] == [2]


def test_resolve_member_query_exact_username(roles):
    target = DummyMember(2, [], None, display_name="Foo", name="Caillean")
    guild = SimpleNamespace(members=[target], get_member=lambda mid: None, fetch_member=AsyncMock())
    target.guild = guild
    found = asyncio.run(ClanRoleManagementCog().resolve_member_query(SimpleNamespace(guild=guild), "caillean"))
    assert [m.id for m in found] == [2]


def test_resolve_member_query_exact_global_name(roles):
    target = DummyMember(2, [], None, display_name="Foo", name="bar", global_name="Smurf")
    guild = SimpleNamespace(members=[target], get_member=lambda mid: None, fetch_member=AsyncMock())
    target.guild = guild
    found = asyncio.run(ClanRoleManagementCog().resolve_member_query(SimpleNamespace(guild=guild), "smurf"))
    assert [m.id for m in found] == [2]


def test_resolve_member_query_startswith_unique(roles):
    target = DummyMember(2, [], None, display_name="Smurfy", name="bar")
    guild = SimpleNamespace(members=[target], get_member=lambda mid: None, fetch_member=AsyncMock())
    target.guild = guild
    found = asyncio.run(ClanRoleManagementCog().resolve_member_query(SimpleNamespace(guild=guild), "smu"))
    assert [m.id for m in found] == [2]


def test_resolve_member_query_mention_and_id(roles):
    target = DummyMember(42, [], None)
    guild = SimpleNamespace(
        members=[target],
        get_member=lambda mid: target if mid == 42 else None,
        fetch_member=AsyncMock(return_value=None),
    )
    target.guild = guild
    cog = ClanRoleManagementCog()
    assert [m.id for m in asyncio.run(cog.resolve_member_query(SimpleNamespace(guild=guild), "<@42>"))] == [42]
    assert [m.id for m in asyncio.run(cog.resolve_member_query(SimpleNamespace(guild=guild), "42"))] == [42]


def test_clanrole_remove_no_match_warning(roles, monkeypatch):
    guild = SimpleNamespace(members=[], get_member=lambda _: None, fetch_member=AsyncMock())
    author = DummyMember(1, [roles["mgr"]], guild)
    ctx = SimpleNamespace(author=author, guild=guild, reply=AsyncMock())
    monkeypatch.setattr("cogs.clanrole_management.is_authorized_clan_role_manager", lambda _: True)
    asyncio.run(ClanRoleManagementCog().clanrole_remove.callback(ClanRoleManagementCog(), ctx, member_query="nobody"))
    assert "No matching member found" in ctx.reply.await_args.kwargs["content"] if "content" in ctx.reply.await_args.kwargs else ctx.reply.await_args.args[0]


def test_multiple_matches_shows_target_dropdown_and_no_mutation(roles, monkeypatch):
    guild = SimpleNamespace(get_member=lambda _: None, fetch_member=AsyncMock())
    t1 = DummyMember(2, [roles["clan1"]], guild, display_name="smurf-one", name="smurfone")
    t2 = DummyMember(3, [roles["clan1"]], guild, display_name="smurf-two", name="smurftwo")
    guild.members = [t1, t2]
    author = DummyMember(1, [roles["mgr"]], guild)
    ctx = SimpleNamespace(author=author, guild=guild, reply=AsyncMock())
    monkeypatch.setattr("cogs.clanrole_management.is_authorized_clan_role_manager", lambda _: True)
    cog = ClanRoleManagementCog()
    asyncio.run(cog.clanrole_remove.callback(cog, ctx, member_query="smurf"))
    assert ctx.reply.await_count == 1
    assert t1.remove_roles.await_count == 0 and t2.remove_roles.await_count == 0


def test_existing_multiclan_dropdown_after_text_resolution(roles, monkeypatch):
    guild = SimpleNamespace(get_member=lambda _: None, fetch_member=AsyncMock())
    target = DummyMember(2, [roles["clan1"], roles["clan2"]], guild, display_name="Caillean", name="caillean")
    guild.members = [target]
    author = DummyMember(1, [roles["mgr"]], guild)
    ctx = SimpleNamespace(author=author, guild=guild, reply=AsyncMock())
    monkeypatch.setattr("cogs.clanrole_management.is_authorized_clan_role_manager", lambda _: True)
    monkeypatch.setattr("cogs.clanrole_management.config.get_clan_role_ids", lambda: {1, 2})
    cog = ClanRoleManagementCog()
    asyncio.run(cog.clanrole_remove.callback(cog, ctx, member_query="Caillean"))
    assert ctx.reply.await_count == 1
    assert target.remove_roles.await_count == 0


def test_target_selection_with_multiclan_edits_interaction_without_ctx_reply(roles, monkeypatch):
    guild = SimpleNamespace()
    author = DummyMember(1, [roles["mgr"]], guild)
    target = DummyMember(2, [roles["clan1"], roles["clan2"]], guild, display_name="Caillean", name="caillean")
    guild.get_member = lambda mid: target if mid == 2 else None
    guild.members = [target]
    ctx = SimpleNamespace(author=author, guild=guild, reply=AsyncMock())
    monkeypatch.setattr("cogs.clanrole_management.config.get_clan_role_ids", lambda: {1, 2})
    cog = ClanRoleManagementCog()
    view = ClanRoleTargetView(cog, ctx, [target])
    interaction = SimpleNamespace(message=SimpleNamespace(id=101), response=SimpleNamespace(edit_message=AsyncMock()))
    asyncio.run(view.handle_selection(interaction, 2))
    assert ctx.reply.await_count == 0
    assert interaction.response.edit_message.await_count == 1


def test_target_selection_multiclan_sets_remove_view_message_reference(roles, monkeypatch):
    guild = SimpleNamespace()
    author = DummyMember(1, [roles["mgr"]], guild)
    target = DummyMember(2, [roles["clan1"], roles["clan2"]], guild, display_name="Caillean", name="caillean")
    guild.get_member = lambda mid: target if mid == 2 else None
    guild.members = [target]
    ctx = SimpleNamespace(author=author, guild=guild, reply=AsyncMock())
    monkeypatch.setattr("cogs.clanrole_management.config.get_clan_role_ids", lambda: {1, 2})
    cog = ClanRoleManagementCog()
    view = ClanRoleTargetView(cog, ctx, [target])
    interaction_message = SimpleNamespace(id=999)
    interaction = SimpleNamespace(message=interaction_message, response=SimpleNamespace(edit_message=AsyncMock()))
    asyncio.run(view.handle_selection(interaction, 2))
    passed_view = interaction.response.edit_message.await_args.kwargs["view"]
    assert isinstance(passed_view, ClanRoleRemoveView)
    assert passed_view.message is interaction_message


def test_remaining_clan_detection_excludes_removed_role_without_cache_refresh(roles, monkeypatch):
    guild = SimpleNamespace(get_role=lambda rid: {3: roles["raid"], 4: roles["wander"]}.get(rid))
    member = DummyMember(42, [roles["clan1"], roles["raid"]], guild)
    # Simulate stale cache: remove_roles does not mutate member.roles
    member.remove_roles = AsyncMock(return_value=None)
    ctx = _ctx(DummyMember(1, [roles["mgr"]], guild))
    cog = ClanRoleManagementCog()
    monkeypatch.setattr("cogs.clanrole_management.config.get_clan_role_ids", lambda: {1, 2})
    monkeypatch.setattr("cogs.clanrole_management.config.get_raid_role_id", lambda: 3)
    monkeypatch.setattr("cogs.clanrole_management.config.get_wandering_souls_role_id", lambda: 4)
    msg = asyncio.run(cog.apply_clan_removal_cleanup(ctx, member, roles["clan1"]))
    assert "Raid kept because another clan role remains" not in msg


def test_view_timeout_handles_missing_message(roles):
    view = ClanRoleRemoveView(ClanRoleManagementCog(), _ctx(DummyMember(1, [roles["mgr"]], SimpleNamespace())), DummyMember(2, [roles["clan1"]], SimpleNamespace()), [roles["clan1"]])
    view.message = SimpleNamespace(edit=AsyncMock(side_effect=RuntimeError("gone")))
    asyncio.run(view.on_timeout())


def test_handle_selection_stops_view(roles):
    cog = ClanRoleManagementCog()
    cog.apply_clan_removal_cleanup = AsyncMock(return_value="ok")
    author = DummyMember(1, [roles["mgr"]], SimpleNamespace())
    target = DummyMember(2, [roles["clan1"]], SimpleNamespace())
    view = ClanRoleRemoveView(cog, _ctx(author), target, [roles["clan1"]])
    interaction = SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock()))
    called = {"stop": False}
    original_stop = view.stop
    def _stop():
        called["stop"] = True
        original_stop()
    view.stop = _stop
    asyncio.run(view.handle_selection(interaction, 1))
    assert called["stop"] is True


def test_missing_configured_roles_reported(roles, monkeypatch):
    guild = SimpleNamespace(get_role=lambda rid: None)
    member = DummyMember(42, [roles["clan1"]], guild)
    ctx = _ctx(DummyMember(1, [roles["mgr"]], guild))
    cog = ClanRoleManagementCog()
    monkeypatch.setattr("cogs.clanrole_management.config.get_clan_role_ids", lambda: {1, 2})
    monkeypatch.setattr("cogs.clanrole_management.config.get_raid_role_id", lambda: 3)
    monkeypatch.setattr("cogs.clanrole_management.config.get_wandering_souls_role_id", lambda: 4)
    msg = asyncio.run(cog.apply_clan_removal_cleanup(ctx, member, roles["clan1"]))
    assert "Configured Raid role could not be found" in msg
    assert "Configured Wandering Souls role could not be found" in msg


def test_setup_registers_clanrole_commands():
    async def _run():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        try:
            await setup(bot)
            command = bot.get_command("clanrole")
            assert command is not None
            assert command.name == "clanrole"
            assert command.get_command("remove") is not None
        finally:
            await bot.close()

    asyncio.run(_run())


def test_clan_command_unchanged_after_clanrole_setup():
    async def _run():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        try:
            await recruitment_clan_profile.setup(bot)
            await setup(bot)
            assert bot.get_command("clan") is not None
            assert bot.get_command("clanrole") is not None
        finally:
            await bot.close()

    asyncio.run(_run())
