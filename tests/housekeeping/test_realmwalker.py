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
        get_role=lambda role_id: role(10, "RealmWalker") if role_id == 10 else None
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
        return (
            "10"
            if key == realmwalker.ACCESS_ROLE_KEY
            else "1234567890123456789,2234567890123456789"
        )

    monkeypatch.setattr(realmwalker.recruitment, "get_config_value_async", get_config)
    config, error = asyncio.run(realmwalker.resolve_config())
    assert error is None
    assert config == realmwalker.RealmWalkerConfig(
        10, frozenset({1234567890123456789, 2234567890123456789})
    )
    assert [key for key, _ in calls] == [
        "REALMWALKER_ACCESS_ROLE_ID",
        "REALMWALKER_GAME_ROLE_IDS",
    ]


def test_config_resolution_parses_comma_separated_game_role_ids(monkeypatch):
    game_ids = (
        1448269393082454076,
        1447924607842652232,
        1447919520751681548,
        1298349996374229045,
        1447924492776116316,
        1447924956519469067,
        1447924705892892733,
    )

    async def get_config(key, default=None):
        if key == realmwalker.ACCESS_ROLE_KEY:
            return "1450000000000000000"
        return ",".join(str(role_id) for role_id in game_ids)

    monkeypatch.setattr(realmwalker.recruitment, "get_config_value_async", get_config)
    config, error = asyncio.run(realmwalker.resolve_config())

    assert error is None
    assert config is not None
    assert config.access_role_id == 1450000000000000000
    assert config.game_role_ids == frozenset(game_ids)
    assert len(config.game_role_ids) == 7
    assert (
        int("".join(str(role_id) for role_id in game_ids)) not in config.game_role_ids
    )


def test_config_resolution_tolerates_whitespace_and_newlines(monkeypatch):
    game_ids = (1234567890123456789, 2234567890123456789, 3234567890123456789)

    async def get_config(key, default=None):
        if key == realmwalker.ACCESS_ROLE_KEY:
            return "10"
        return f" {game_ids[0]}  {game_ids[1]}\n{game_ids[2]} "

    monkeypatch.setattr(realmwalker.recruitment, "get_config_value_async", get_config)
    config, error = asyncio.run(realmwalker.resolve_config())

    assert error is None
    assert config == realmwalker.RealmWalkerConfig(10, frozenset(game_ids))


def test_parse_role_ids_accepts_semicolons_mentions_and_deduplicates():
    first = 1234567890123456789
    second = 2234567890123456789

    parsed, invalid = realmwalker._parse_role_ids(
        f"{first};<@&{second}>;<@&{first}>"
    )

    assert parsed == {first, second}
    assert invalid == []


def test_parse_role_ids_splits_a_concatenated_digit_run():
    game_ids = (1234567890123456789, 2234567890123456789, 3234567890123456789)

    parsed, invalid = realmwalker._parse_role_ids("".join(map(str, game_ids)))

    assert parsed == set(game_ids)
    assert invalid == []


def test_parse_role_ids_marks_malformed_long_digit_run_invalid():
    malformed = "12345678901234567890"

    parsed, invalid = realmwalker._parse_role_ids(malformed)

    assert parsed == set()
    assert invalid == [malformed]


def test_parse_role_ids_marks_short_digit_run_invalid():
    parsed, invalid = realmwalker._parse_role_ids("12345")

    assert parsed == set()
    assert invalid == ["12345"]


def test_unresolved_game_role_ids_are_reported_individually():
    access_id = 1298349996374229045
    game_ids = frozenset({1448269393082454076, 1447924607842652232})
    guild = SimpleNamespace(
        get_role=lambda role_id: (
            role(access_id, "RealmWalker") if role_id == access_id else None
        )
    )

    resolved, error = realmwalker.resolve_guild_roles(
        guild, realmwalker.RealmWalkerConfig(access_id, game_ids)
    )

    assert resolved is None
    assert error is not None
    assert "`1448269393082454076`" in error
    assert "`1447924607842652232`" in error
    assert "`, `" in error
    assert "14482693930824540761447924607842652232" not in error


def test_concatenated_impossible_value_is_unresolved_while_valid_role_continues():
    access_id = 10
    valid_game_id = 20
    joined_id = 14482693930824540761447924607842652232
    roles = {
        access_id: role(access_id, "RealmWalker"),
        valid_game_id: role(valid_game_id, "WoW"),
    }

    resolved, warning = realmwalker.resolve_guild_roles(
        SimpleNamespace(get_role=roles.get),
        realmwalker.RealmWalkerConfig(access_id, frozenset({valid_game_id, joined_id})),
    )

    assert resolved is not None
    assert [item.id for item in resolved.game_roles] == [valid_game_id]
    assert warning is not None
    assert f"`{joined_id}`" in warning


def test_partial_unresolved_warning_lists_ids_and_audit_uses_resolved_roles():
    access = role(10, "RealmWalker")
    game = role(20, "WoW")
    unresolved_ids = (30, 40)
    guild = SimpleNamespace(get_role={10: access, 20: game}.get)

    resolved, warning = realmwalker.resolve_guild_roles(
        guild,
        realmwalker.RealmWalkerConfig(10, frozenset({20, *unresolved_ids})),
    )
    result = realmwalker.scan_members(
        [member(1, [game])],
        realmwalker.RealmWalkerConfig(10, frozenset({20, *unresolved_ids})),
    )

    assert resolved is not None
    assert len(result.issues) == 1
    assert warning is not None
    assert "`30`, `40`" in warning
    assert "`3040`" not in warning


def test_access_role_config_rejects_a_role_id_list(monkeypatch):
    async def get_config(key, default=None):
        return "10,11" if key == realmwalker.ACCESS_ROLE_KEY else "20,21"

    monkeypatch.setattr(realmwalker.recruitment, "get_config_value_async", get_config)
    config, error = asyncio.run(realmwalker.resolve_config())

    assert config is None
    assert error is not None
    assert "REALMWALKER_ACCESS_ROLE_ID is missing or invalid" in error


def test_manual_fix_uses_valid_ids_and_warns_for_invalid_parsed_config(
    monkeypatch,
):
    access_id = 1234567890123456789
    game_id = 2234567890123456789
    second_game_id = 3234567890123456789
    add_called = False

    async def add_roles(*_args, **_kwargs):
        nonlocal add_called
        add_called = True

    affected = member(1, [role(game_id, "WoW")])
    affected.add_roles = add_roles

    async def get_config(key, default=None):
        if key == realmwalker.ACCESS_ROLE_KEY:
            return str(access_id)
        return f"{game_id},12345,{second_game_id}"

    monkeypatch.setattr(realmwalker.recruitment, "get_config_value_async", get_config)
    roles = {
        access_id: role(access_id, "RealmWalker"),
        game_id: affected.roles[0],
        second_game_id: role(second_game_id, "GW2"),
    }

    async def fetched_members():
        yield affected

    guild = SimpleNamespace(
        get_role=roles.get, fetch_members=lambda **_kwargs: fetched_members()
    )
    sent = []

    async def send(**kwargs):
        sent.append(kwargs)

    cog = RealmWalkerAuditCog(SimpleNamespace())
    ctx = SimpleNamespace(guild=guild, send=send)
    asyncio.run(RealmWalkerAuditCog.audit_realmwalker.callback(cog, ctx, "fix"))

    assert add_called is True
    assert "contains invalid values: `12345`" in (
        sent[0]["embed"].description or ""
    )


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


def test_daily_audit_combines_config_and_unresolved_role_warnings(monkeypatch):
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
    affected = member(1, [roles[20]])

    async def fetched_members():
        yield affected

    guild = SimpleNamespace(
        id=100,
        roles=list(roles.values()),
        fetch_members=lambda **_kwargs: fetched_members(),
        get_role=roles.get,
    )
    affected.guild = guild

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
            realmwalker_config=realmwalker.RealmWalkerConfig(10, frozenset({20, 30})),
            realmwalker_warning=(
                "REALMWALKER_GAME_ROLE_IDS contains invalid values: `bad-id`"
            ),
        )
    )

    assert result is not None
    assert len(result.realmwalker_issues) == 1
    assert "invalid values: `bad-id`" in (result.realmwalker_warning or "")
    assert "could not be resolved in this guild: `30`" in (
        result.realmwalker_warning or ""
    )
