import asyncio
from types import SimpleNamespace

import discord

from modules.housekeeping import realmwalker, role_audit


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
