import asyncio
from types import SimpleNamespace

import discord

from modules.housekeeping import realmwalker, role_audit
from cogs.housekeeping_realmwalker import MEMBER_LOAD_ERROR, RealmWalkerAuditCog


class Member(SimpleNamespace):
    @property
    def mention(self):
        return f"<@{self.id}>"


def role(role_id, name):
    return SimpleNamespace(id=role_id, name=name)


def member(member_id, roles, *, bot=False):
    return Member(
        id=member_id,
        name=f"user{member_id}",
        display_name=f"User {member_id}",
        roles=roles,
        bot=bot,
    )


def test_scan_flags_only_humans_with_games_and_missing_access_once():
    access = role(10, "RealmWalker")
    game_a = role(20, "ArcRaiders")
    game_b = role(21, "WoW")
    other = role(30, "Other")
    config = realmwalker.RealmWalkerConfig(10, frozenset({20, 21}))
    flagged = member(1, [game_a, game_b])
    result = realmwalker.scan_members(
        [
            flagged,
            member(2, [game_a, access]),
            member(3, [access]),
            member(4, [other]),
            member(5, [game_a], bot=True),
        ],
        config,
    )
    assert result.checked == 4
    assert len(result.issues) == 1
    assert result.issues[0].member is flagged
    assert [item.name for item in result.issues[0].matched_game_roles] == [
        "ArcRaiders",
        "WoW",
    ]


def test_fix_adds_access_without_removing_games_and_continues_after_failures(
    monkeypatch,
):
    access = role(10, "RealmWalker")
    game = role(20, "WoW")
    good = member(1, [game])
    forbidden = member(2, [game])
    http_error = member(3, [game])

    async def add_good(added, **_kwargs):
        good.roles.append(added)

    async def add_forbidden(*_args, **_kwargs):
        raise discord.Forbidden(
            SimpleNamespace(status=403, reason="Forbidden"), "hierarchy"
        )

    async def add_http(*_args, **_kwargs):
        raise discord.HTTPException(
            SimpleNamespace(status=500, reason="Error"), "failure"
        )

    good.add_roles = add_good
    forbidden.add_roles = add_forbidden
    http_error.add_roles = add_http
    issues = realmwalker.scan_members(
        [good, forbidden, http_error],
        realmwalker.RealmWalkerConfig(10, frozenset({20})),
    ).issues
    result = asyncio.run(realmwalker.fix_issues(issues, access))
    assert result.fixed[0].member is good
    assert len(result.failures) == 2
    assert game in good.roles and access in good.roles


def test_daily_section_reports_mismatch_and_fix_hint_without_mutation():
    game = role(20, "WoW")
    affected = member(1, [game])
    issue = realmwalker.RealmWalkerIssue(affected, (game,))
    summary = role_audit.AuditResult(checked=1, realmwalker_issues=[issue])
    embed = role_audit._render_report(
        summary=summary, raid_role_name="Raid", wanderer_role_name="Wandering Souls"
    )
    text = embed.description or ""
    assert "Missing RealmWalker access" in text
    assert "<@1> – User 1 – game roles: WoW" in text
    assert "!audit realmwalker fix" in text
    assert not hasattr(affected, "add_roles")


def test_daily_config_warning_renders_without_crashing():
    summary = role_audit.AuditResult(
        checked=1, realmwalker_warning="Config is invalid."
    )
    embed = role_audit._render_report(
        summary=summary, raid_role_name="Raid", wanderer_role_name="Wandering Souls"
    )
    assert "RealmWalker audit warning" in (embed.description or "")


def test_manual_fix_aborts_with_embed_when_full_member_load_fails(monkeypatch):
    cached = member(1, [role(20, "WoW")])
    add_called = False

    async def add_roles(*_args, **_kwargs):
        nonlocal add_called
        add_called = True

    cached.add_roles = add_roles

    async def failing_members():
        raise RuntimeError("member fetch failed")
        yield  # pragma: no cover

    guild = SimpleNamespace(
        members=[cached],
        fetch_members=lambda **_kwargs: failing_members(),
        get_role=lambda _role_id: role(10, "RealmWalker"),
    )
    sent = []

    async def send(**kwargs):
        sent.append(kwargs)

    ctx = SimpleNamespace(guild=guild, send=send)

    async def config():
        return realmwalker.RealmWalkerConfig(10, frozenset({20})), None

    monkeypatch.setattr(realmwalker, "resolve_config", config)
    cog = RealmWalkerAuditCog(SimpleNamespace())
    asyncio.run(RealmWalkerAuditCog.audit_realmwalker.callback(cog, ctx, "fix"))

    assert add_called is False
    assert len(sent) == 1
    assert MEMBER_LOAD_ERROR in (sent[0]["embed"].description or "")
    assert "No roles were changed" in (sent[0]["embed"].description or "")


def test_manual_report_aborts_instead_of_claiming_clean_on_member_load_failure(
    monkeypatch,
):
    async def failing_members():
        raise RuntimeError("member fetch failed")
        yield  # pragma: no cover

    guild = SimpleNamespace(
        members=[],
        fetch_members=lambda **_kwargs: failing_members(),
        get_role=lambda role_id: role(
            role_id, "RealmWalker" if role_id == 10 else "WoW"
        ),
    )
    sent = []

    async def send(**kwargs):
        sent.append(kwargs)

    async def config():
        return realmwalker.RealmWalkerConfig(10, frozenset({20})), None

    monkeypatch.setattr(realmwalker, "resolve_config", config)
    cog = RealmWalkerAuditCog(SimpleNamespace())
    ctx = SimpleNamespace(guild=guild, send=send)
    asyncio.run(RealmWalkerAuditCog.audit_realmwalker.callback(cog, ctx, ""))

    description = sent[0]["embed"].description or ""
    assert MEMBER_LOAD_ERROR in description
    assert "No RealmWalker access issues found" not in description


def test_manual_audit_aborts_when_game_role_does_not_resolve(monkeypatch):
    roles = {10: role(10, "RealmWalker")}
    guild = SimpleNamespace(get_role=roles.get)
    sent = []

    async def send(**kwargs):
        sent.append(kwargs)

    async def config():
        return realmwalker.RealmWalkerConfig(10, frozenset({20, 21})), None

    monkeypatch.setattr(realmwalker, "resolve_config", config)
    ctx = SimpleNamespace(guild=guild, send=send)
    cog = RealmWalkerAuditCog(SimpleNamespace())
    asyncio.run(RealmWalkerAuditCog.audit_realmwalker.callback(cog, ctx, ""))

    description = sent[0]["embed"].description or ""
    assert "`20`, `21`" in description
    assert "No RealmWalker access issues found" not in description


def test_manual_fix_does_not_mutate_when_role_config_is_invalid(monkeypatch):
    affected = member(1, [role(20, "WoW")])
    add_called = False

    async def add_roles(*_args, **_kwargs):
        nonlocal add_called
        add_called = True

    affected.add_roles = add_roles
    guild = SimpleNamespace(
        get_role=lambda role_id: role(10, "RealmWalker")
        if role_id == 10
        else None
    )
    sent = []

    async def send(**kwargs):
        sent.append(kwargs)

    async def config():
        return realmwalker.RealmWalkerConfig(10, frozenset({20})), None

    monkeypatch.setattr(realmwalker, "resolve_config", config)
    ctx = SimpleNamespace(guild=guild, send=send)
    cog = RealmWalkerAuditCog(SimpleNamespace())
    asyncio.run(RealmWalkerAuditCog.audit_realmwalker.callback(cog, ctx, "fix"))

    assert add_called is False
    assert "No roles were changed" in (sent[0]["embed"].description or "")


def test_clean_manual_result_includes_resolved_role_summary(monkeypatch):
    roles = {
        10: role(10, "RealmWalker"),
        20: role(20, "WoW"),
        21: role(21, "Guild Wars 2"),
    }

    async def fetched_members():
        yield member(1, [roles[10], roles[20]])

    guild = SimpleNamespace(
        get_role=roles.get, fetch_members=lambda **_kwargs: fetched_members()
    )
    sent = []

    async def send(**kwargs):
        sent.append(kwargs)

    async def config():
        return realmwalker.RealmWalkerConfig(10, frozenset({20, 21})), None

    monkeypatch.setattr(realmwalker, "resolve_config", config)
    ctx = SimpleNamespace(guild=guild, send=send)
    cog = RealmWalkerAuditCog(SimpleNamespace())
    asyncio.run(RealmWalkerAuditCog.audit_realmwalker.callback(cog, ctx, ""))

    embed = sent[0]["embed"]
    assert "No RealmWalker access issues found" in (embed.description or "")
    summary = embed.fields[0].value
    assert "RealmWalker (`10`)" in summary
    assert "WoW (`20`)" in summary
    assert "Guild Wars 2 (`21`)" in summary
    assert sent[0]["allowed_mentions"].everyone is False
    assert sent[0]["allowed_mentions"].users is False
    assert sent[0]["allowed_mentions"].roles is False


def test_config_resolution_still_uses_recruitment_config_helper(monkeypatch):
    calls = []

    async def get_config(key, default=None):
        calls.append((key, default))
        return "10" if key == realmwalker.ACCESS_ROLE_KEY else "20,21"

    monkeypatch.setattr(realmwalker.recruitment, "get_config_value_async", get_config)
    config, error = asyncio.run(realmwalker.resolve_config())
    assert error is None
    assert config == realmwalker.RealmWalkerConfig(10, frozenset({20, 21}))
    assert [key for key, _ in calls] == [
        "REALMWALKER_ACCESS_ROLE_ID",
        "REALMWALKER_GAME_ROLE_IDS",
    ]


def test_daily_realmwalker_scan_warns_instead_of_using_partial_cache(monkeypatch):
    roles = {
        role_id: role(role_id, name)
        for role_id, name in (
            (1, "Raid"),
            (2, "Wandering Souls"),
            (3, "Visitor"),
            (10, "RealmWalker"),
            (20, "WoW"),
        )
    }
    cached = member(1, [roles[20]])

    async def failing_members():
        raise RuntimeError("member fetch failed")
        yield  # pragma: no cover

    guild = SimpleNamespace(
        id=100,
        members=[cached],
        roles=list(roles.values()),
        fetch_members=lambda **_kwargs: failing_members(),
        get_role=lambda role_id: roles.get(role_id),
    )
    cached.guild = guild

    async def no_tickets(*_args, **_kwargs):
        return []

    monkeypatch.setattr(role_audit, "fetch_ticket_threads", no_tickets)
    result = asyncio.run(
        role_audit._audit_guild(
            SimpleNamespace(),
            guild,
            raid_role_id=1,
            wanderer_role_id=2,
            visitor_role_id=3,
            clan_role_ids={99},
            raid_role_name="Raid",
            wanderer_role_name="Wandering Souls",
            realmwalker_config=realmwalker.RealmWalkerConfig(10, frozenset({20})),
        )
    )

    assert result is not None
    assert result.realmwalker_issues == []
    assert "full member list could not be loaded" in (result.realmwalker_warning or "")
