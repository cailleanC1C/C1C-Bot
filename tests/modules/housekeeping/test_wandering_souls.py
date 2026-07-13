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
    assert "Name: Included" in ctx.sent[0]["embed"].description


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


def test_unknown_message_stats_are_rendered_honestly():
    joined = dt.datetime(2026, 1, 2, 3, 4, tzinfo=dt.timezone.utc)
    result = ws.InvestigationResult(total_wandering=1, excluded=0, entries=(ws.InvestigationEntry(Member(1, "Mystery", [], joined)),))

    embed = ws.build_investigation_embeds(result)[0]

    assert "Last activity" not in embed.description
    assert "Last message: none found in scan window" in embed.description
    assert "Messages in scan window: 0" in embed.description
    assert embed.footer.text == ws.ACTIVITY_FOOTER

class Message:
    def __init__(self, author_id, created_at):
        self.author = SimpleNamespace(id=author_id)
        self.created_at = created_at


class Channel:
    def __init__(self, channel_id, name, messages=(), error=None):
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.messages = list(messages)
        self.error = error
        self.calls = []

    async def _history(self, *, limit=None, after=None, oldest_first=False):
        self.calls.append({"limit": limit, "after": after, "oldest_first": oldest_first})
        if self.error is not None:
            raise self.error
        for message in self.messages:
            yield message

    def history(self, *, limit=None, after=None, oldest_first=False):
        return self._history(limit=limit, after=after, oldest_first=oldest_first)


def test_default_scan_window_is_90_days():
    assert ws.parse_scan_days(None) == (90, None)
    result = ws.collect_wandering_souls(Guild([Role(10), Role(20)], [Member(1, "A", [Role(10)])]), 10, 20)
    assert result.scan_days == 90


def test_invalid_day_argument_returns_embed_error():
    days, error = ws.parse_scan_days("soon")
    embed = ws.build_error_embed(error)

    assert days is None
    assert embed.title == "Wandering Souls Investigation Error"
    assert "!wanderingsouls investigate <days>" in embed.description


def test_day_argument_is_clamped_to_max_180():
    assert ws.parse_scan_days("999") == (180, None)


def test_entries_include_profile_mention_and_id_and_last_message_wording():
    result = ws.InvestigationResult(total_wandering=1, excluded=0, entries=(ws.InvestigationEntry(Member(123, "Mystery", [])),))
    description = ws.build_investigation_embeds(result)[0].description

    assert "Profile: <@123>" in description
    assert "ID: 123" in description
    assert "Last activity" not in description
    assert "Last message:" in description
    assert "Messages in scan window:" in description


def test_only_final_candidate_member_ids_are_counted():
    now = dt.datetime(2026, 7, 13, tzinfo=dt.timezone.utc)
    wandering = Role(10)
    exclude = Role(20)
    included = Member(1, "Included", [wandering])
    other = Member(2, "Other", [])
    guild = Guild([wandering, exclude], [included, other])
    channel = Channel(100, "general", [Message(1, now), Message(2, now)])
    guild.text_channels = [channel]

    result = ws.collect_wandering_souls(guild, wandering.id, exclude.id)
    scanned = run(ws.scan_recent_messages(guild, result, now=now))

    assert len(scanned.entries) == 1
    assert scanned.entries[0].member.id == 1
    assert scanned.entries[0].scanned_message_count == 1


def test_excluded_role_members_are_not_scanned_or_counted():
    now = dt.datetime(2026, 7, 13, tzinfo=dt.timezone.utc)
    wandering = Role(10)
    exclude = Role(20)
    included = Member(1, "Included", [wandering])
    excluded = Member(2, "Excluded", [wandering, exclude])
    guild = Guild([wandering, exclude], [included, excluded])
    channel = Channel(100, "general", [Message(1, now), Message(2, now)])
    guild.text_channels = [channel]

    result = ws.collect_wandering_souls(guild, wandering.id, exclude.id)
    scanned = run(ws.scan_recent_messages(guild, result, now=now))

    assert scanned.excluded == 1
    assert [entry.member.id for entry in scanned.entries] == [1]
    assert scanned.entries[0].scanned_message_count == 1


def test_forbidden_channel_history_is_skipped_and_reported(monkeypatch):
    now = dt.datetime(2026, 7, 13, tzinfo=dt.timezone.utc)
    monkeypatch.setattr(ws.discord, "Forbidden", RuntimeError)
    guild = Guild([Role(10), Role(20)], [Member(1, "Included", [Role(10)])])
    guild.text_channels = [Channel(100, "private", error=RuntimeError("no access"))]
    result = ws.collect_wandering_souls(guild, 10, 20)

    scanned = run(ws.scan_recent_messages(guild, result, now=now))
    description = ws.build_investigation_embeds(scanned)[0].description

    assert scanned.scan_warning_count == 1
    assert "Scan warning count: 1 channel(s) could not be read" in description


def test_scan_populates_last_message_count_and_channel():
    now = dt.datetime(2026, 7, 13, 12, tzinfo=dt.timezone.utc)
    older = now - dt.timedelta(days=2)
    member = Member(1, "Included", [Role(10)])
    guild = Guild([Role(10), Role(20)], [member])
    channel = Channel(100, "general", [Message(1, older), Message(1, now)])
    guild.text_channels = [channel]

    result = ws.collect_wandering_souls(guild, 10, 20, scan_days=90)
    scanned = run(ws.scan_recent_messages(guild, result, now=now))
    description = ws.build_investigation_embeds(scanned)[0].description

    assert "Last message: 2026-07-13 12:00 UTC" in description
    assert "Messages in scan window: 2" in description
    assert "Last seen channel: <#100>" in description
