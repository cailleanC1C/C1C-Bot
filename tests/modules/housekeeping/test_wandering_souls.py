from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace

from cogs.housekeeping_wandering_souls import WanderingSoulsCog
from modules.housekeeping import wandering_souls as ws


def run(coro):
    return asyncio.run(coro)


class Role:
    def __init__(self, role_id):
        self.id = role_id


class Member:
    def __init__(self, member_id, name, roles=(), joined_at=None):
        self.id = member_id
        self.display_name = name
        self.roles = list(roles)
        self.joined_at = joined_at
        self.mention = f"<@{member_id}>"

    async def add_roles(self, *args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("read-only command must not mutate roles")

    async def remove_roles(self, *args, **kwargs):  # pragma: no cover - must not be called
        raise AssertionError("read-only command must not mutate roles")


class Guild:
    def __init__(self, roles, members, *, chunked=True, chunk_error=None):
        self._roles = {role.id: role for role in roles}
        self.members = list(members)
        self.chunked = chunked
        self.chunk_error = chunk_error
        self.chunk_calls = []

    def get_role(self, role_id):
        return self._roles.get(role_id)

    async def chunk(self, *, cache=False):
        self.chunk_calls.append({"cache": cache})
        if self.chunk_error is not None:
            raise self.chunk_error
        self.chunked = True
        return self.members


class Ctx:
    def __init__(self, guild):
        self.guild = guild
        self.invoked_subcommand = None
        self.sent = []

    async def send(self, **kwargs):
        self.sent.append(kwargs)
        return SimpleNamespace(id=len(self.sent))


def test_missing_wandering_souls_role_id_returns_admin_error(monkeypatch):
    exclude = Role(20)
    guild = Guild([exclude], [])
    monkeypatch.setattr(ws.config, "get_wandering_souls_role_id", lambda: None)
    monkeypatch.setattr(ws.config, "get_wandering_souls_exclude_role_id", lambda: exclude.id)

    _wandering, _exclude, error = ws.resolve_investigation_roles(guild)

    assert error == "Missing or invalid WANDERING_SOULS_ROLE_ID. Set it to a numeric Discord role ID."
    assert ws.build_error_embed(error).title == "Wandering Souls Investigation Error"


def test_missing_wandering_souls_exclude_role_id_returns_admin_error(monkeypatch):
    wandering = Role(10)
    guild = Guild([wandering], [])
    monkeypatch.setattr(ws.config, "get_wandering_souls_role_id", lambda: wandering.id)
    monkeypatch.setattr(ws.config, "get_wandering_souls_exclude_role_id", lambda: None)

    _wandering, _exclude, error = ws.resolve_investigation_roles(guild)

    assert error == "Missing or invalid WANDERING_SOULS_EXCLUDE_ROLE_ID. Set it to a numeric Discord role ID."
    assert "WANDERING_SOULS_EXCLUDE_ROLE_ID" in ws.build_error_embed(error).description


def test_members_with_wandering_souls_role_are_included():
    wandering = Role(10)
    exclude = Role(20)
    member = Member(1, "Included", [wandering])
    guild = Guild([wandering, exclude], [member, Member(2, "Other", [])])

    result = ws.collect_wandering_souls(guild, wandering.id, exclude.id)

    assert result.total_wandering == 1
    assert [entry.member.display_name for entry in result.entries] == ["Included"]


def test_members_with_both_wandering_souls_and_exclusion_role_are_excluded():
    wandering = Role(10)
    exclude = Role(20)
    guild = Guild([wandering, exclude], [Member(1, "Excluded", [wandering, exclude]), Member(2, "Included", [wandering])])

    result = ws.collect_wandering_souls(guild, wandering.id, exclude.id)

    assert result.total_wandering == 2
    assert result.excluded == 1
    assert [entry.member.display_name for entry in result.entries] == ["Included"]


def test_bare_wandering_souls_group_response_is_embed():
    ctx = Ctx(Guild([], []))
    cog = WanderingSoulsCog(SimpleNamespace())

    run(cog.wandering_souls_group.callback(cog, ctx))

    assert ctx.sent
    embed = ctx.sent[0]["embed"]
    assert embed.title == "Wandering Souls Diagnostics"
    assert embed.description == "Use `!wanderingsouls investigate` to list current Wandering Souls members."


def test_command_chunks_guild_before_collecting_when_cache_is_incomplete(monkeypatch):
    wandering = Role(10)
    exclude = Role(20)
    guild = Guild([wandering, exclude], [Member(1, "Included", [wandering])], chunked=False)
    monkeypatch.setattr(ws.config, "get_wandering_souls_role_id", lambda: wandering.id)
    monkeypatch.setattr(ws.config, "get_wandering_souls_exclude_role_id", lambda: exclude.id)
    ctx = Ctx(guild)
    cog = WanderingSoulsCog(SimpleNamespace())

    run(cog.investigate.callback(cog, ctx))

    assert guild.chunk_calls == [{"cache": True}]
    assert ctx.sent[0]["embed"].title == "Wandering Souls Investigation"
    assert "Player: Included" in ctx.sent[0]["embed"].description


def test_command_returns_error_embed_when_chunking_fails(monkeypatch):
    wandering = Role(10)
    exclude = Role(20)
    guild = Guild([wandering, exclude], [Member(1, "Partial", [wandering])], chunked=False, chunk_error=RuntimeError("discord unavailable"))
    monkeypatch.setattr(ws.config, "get_wandering_souls_role_id", lambda: wandering.id)
    monkeypatch.setattr(ws.config, "get_wandering_souls_exclude_role_id", lambda: exclude.id)
    ctx = Ctx(guild)
    cog = WanderingSoulsCog(SimpleNamespace())

    run(cog.investigate.callback(cog, ctx))

    assert guild.chunk_calls == [{"cache": True}]
    assert len(ctx.sent) == 1
    embed = ctx.sent[0]["embed"]
    assert embed.title == "Wandering Souls Investigation Error"
    assert "full member list could not be loaded" in embed.description
    assert "Partial" not in embed.description


def test_command_is_read_only_and_does_not_call_role_mutation_methods(monkeypatch):
    wandering = Role(10)
    exclude = Role(20)
    guild = Guild([wandering, exclude], [Member(1, "Included", [wandering])])
    monkeypatch.setattr(ws.config, "get_wandering_souls_role_id", lambda: wandering.id)
    monkeypatch.setattr(ws.config, "get_wandering_souls_exclude_role_id", lambda: exclude.id)
    ctx = Ctx(guild)
    cog = WanderingSoulsCog(SimpleNamespace())

    run(cog.investigate.callback(cog, ctx))

    assert ctx.sent
    assert ctx.sent[0]["embed"].title == "Wandering Souls Investigation"


def test_long_results_are_paginated_safely():
    entries = tuple(ws.InvestigationEntry(Member(i, f"Member {i:03} with a fairly long display name", [])) for i in range(120))
    result = ws.InvestigationResult(total_wandering=120, excluded=0, entries=entries)

    embeds = ws.build_investigation_embeds(result)

    assert len(embeds) > 1
    assert all(len(embed.description) <= ws.MAX_EMBED_DESCRIPTION for embed in embeds)
    assert embeds[0].title.startswith("Wandering Souls Investigation (1/")


def test_unknown_activity_and_message_stats_are_rendered_honestly():
    joined = dt.datetime(2026, 1, 2, 3, 4, tzinfo=dt.timezone.utc)
    result = ws.InvestigationResult(total_wandering=1, excluded=0, entries=(ws.InvestigationEntry(Member(1, "Mystery", [], joined)),))

    embed = ws.build_investigation_embeds(result)[0]

    assert "Last activity: unknown" in embed.description
    assert "Messages: unknown" in embed.description
    assert embed.footer.text == ws.ACTIVITY_FOOTER
