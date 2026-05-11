from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import asyncio
import pytest

from cogs.clanrole_management import ClanRoleManagementCog, ClanRoleRemoveView, get_member_clan_roles, is_authorized_clan_role_manager


class DummyRole:
    def __init__(self, rid: int, name: str):
        self.id = rid
        self.name = name
        self.mention = f"@{name}"


class DummyMember:
    def __init__(self, mid: int, roles: list[DummyRole], guild, admin: bool = False):
        self.id = mid
        self.roles = roles
        self.guild = guild
        self.mention = f"<@{mid}>"
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
