from __future__ import annotations

import datetime as dt
import os
import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from cogs.housekeeping_achievement_collector import AchievementCollectorCog
from modules.housekeeping import achievement_collector as ac


def run(coro):
    return asyncio.run(coro)


class Role:
    def __init__(self, id): self.id = id

class Member:
    def __init__(self, id, name, roles=(), bot=False):
        self.id = id; self.display_name = name; self.roles = list(roles); self.bot = bot; self.mention = f"<@{id}>"

class Guild:
    id = 123
    def __init__(self, roles, members):
        self._roles = {r.id: r for r in roles}; self.members = members
    def get_role(self, rid): return self._roles.get(rid)

class Channel:
    def __init__(self): self.sent=[]
    async def send(self, **kwargs): self.sent.append(kwargs); return SimpleNamespace(id=42)

class Ctx:
    def __init__(self, guild, author, channel): self.guild=guild; self.author=author; self.channel=channel; self.sent=[]
    async def send(self, **kwargs): self.sent.append(kwargs); return SimpleNamespace(id=99)


def cfg(**overrides):
    base = dict(channel_id=555, default_limit=5, max_limit=10, min_count=1, schedule_rrule="FREQ=MONTHLY;BYDAY=MO;BYSETPOS=1", schedule_time_utc="09:30", roles_tab="Roles")
    base.update(overrides)
    return ac.AchievementCollectorConfig(**base)


def test_missing_achievements_sheet_id_fails(monkeypatch):
    monkeypatch.delenv("ACHIEVEMENTS_SHEET_ID", raising=False)
    with pytest.raises(ac.AchievementCollectorError, match="Missing ACHIEVEMENTS_SHEET_ID"):
        run(ac.load_active_role_ids(cfg()))


def test_missing_config_key_fails(monkeypatch):
    async def fake(key): return None if key == "achievement_collector_roles_tab" else "1"
    monkeypatch.setattr(ac.recruitment, "get_config_value_async", fake)
    with pytest.raises(ac.AchievementCollectorError, match="achievement_collector_roles_tab"):
        run(ac.resolve_config())


@pytest.mark.parametrize("key,value", [
    ("achievement_collector_default_limit", "x"),
    ("achievement_collector_max_limit", "x"),
    ("achievement_collector_min_count", "x"),
])
def test_invalid_integer_config_fails(monkeypatch, key, value):
    values = {
        "achievement_collector_channel_id":"1", "achievement_collector_default_limit":"5", "achievement_collector_max_limit":"10", "achievement_collector_min_count":"1",
        "achievement_collector_schedule_rrule":"FREQ=MONTHLY;BYDAY=MO;BYSETPOS=1", "achievement_collector_schedule_time_utc":"09:30", "achievement_collector_roles_tab":"Roles",
    }
    values[key] = value
    async def fake(k): return values[k]
    monkeypatch.setattr(ac.recruitment, "get_config_value_async", fake)
    with pytest.raises(ac.AchievementCollectorError, match=key): run(ac.resolve_config())


def test_dynamic_headers_active_true_blank_and_inactive_ignored(monkeypatch):
    monkeypatch.setenv("ACHIEVEMENTS_SHEET_ID", "sheet")
    async def fake_values(sheet_id, tab):
        return [["Other", "Active", "role_id"], ["", "TRUE", "101"], ["", "true", "102"], ["", "False", "103"], ["", "TRUE", ""], ["", "yes", "104"]]
    monkeypatch.setattr(ac.async_core, "afetch_values", fake_values)
    assert run(ac.load_active_role_ids(cfg())) == {101, 102}


def test_missing_headers_fail(monkeypatch):
    monkeypatch.setenv("ACHIEVEMENTS_SHEET_ID", "sheet")
    async def fake_values(sheet_id, tab): return [["role_id", "Enabled"]]
    monkeypatch.setattr(ac.async_core, "afetch_values", fake_values)
    with pytest.raises(ac.AchievementCollectorError, match="Active"):
        run(ac.load_active_role_ids(cfg()))


def test_stale_roles_bots_min_count_and_sorting(monkeypatch):
    async def fake_roles(config): return {1, 2, 999}
    monkeypatch.setattr(ac, "load_active_role_ids", fake_roles)
    r1, r2, stale = Role(1), Role(2), Role(999)
    guild = Guild([r1, r2], [Member(1,"Zed",[r1,r2]), Member(2,"Amy",[r1,r2]), Member(3,"Low",[r1]), Member(4,"Bot",[r1,r2], bot=True), Member(5,"Stale",[stale])])
    cache = run(ac.build_leaderboard(guild, cfg(min_count=2)))
    assert [(e.display_name, e.count, e.rank) for e in cache.entries] == [("Amy",2,1),("Zed",2,2)]
    assert 4 not in cache.counts


def test_effective_limit_default_override_cap_and_invalid():
    c = cfg(default_limit=7, max_limit=10)
    assert ac.effective_limit(None, c) == 7
    assert ac.effective_limit(3, c) == 3
    assert ac.effective_limit(99, c) == 10
    with pytest.raises(ac.AchievementCollectorError): ac.effective_limit(0, c)


def test_leaderboard_and_rank_embeds_and_allowed_copy_branches():
    entries = tuple(ac.LeaderboardEntry(i, f"<@{i}>", f"U{i:02}", count, i) for i, count in enumerate([11,10,9,8,7,6,5,4,3,2,2], start=1))
    entries = entries + (ac.LeaderboardEntry(20, "<@20>", "One", 1, 12),)
    counts = {e.member_id:e.count for e in entries} | {21:0}
    cache = ac.LeaderboardCache(1, dt.datetime.now(dt.timezone.utc), entries, counts)
    emb = ac.leaderboard_embed(cache, 2)
    assert emb.title == "Achievement Collectors" and emb.description.startswith(ac.LEADERBOARD_INTRO)
    assert "1. <@1> - 11 achievements" in emb.description
    assert ac.rank_embed(Member(1,"",[]), cache).description == "<@1> is sitting at rank #1 with 11 achievements. Disgustingly shiny. Respectfully."
    assert "Dangerously shiny" in ac.rank_embed(Member(2,"",[]), cache).description
    assert "shiny danger zone" in ac.rank_embed(Member(4,"",[]), cache).description
    assert "hoard is growing" in ac.rank_embed(Member(11,"",[]), cache).description
    assert "collection has begun" in ac.rank_embed(Member(20,"",[]), cache).description
    assert "no counted achievements" in ac.rank_embed(Member(21,"",[]), cache).description



def test_rank_embed_handles_count_above_one_below_min_count_without_rank():
    cache = ac.LeaderboardCache(
        1,
        dt.datetime.now(dt.timezone.utc),
        (),
        {30: 2},
    )

    embed = ac.rank_embed(Member(30, "BelowMin", []), cache)

    assert embed.description == "<@30> has 2 achievements. The hoard is growing, but not enough to make the board yet."

def test_empty_leaderboard_embed():
    cache = ac.LeaderboardCache(1, dt.datetime.now(dt.timezone.utc), (), {})
    emb = ac.leaderboard_embed(cache, 10)
    assert emb.title == "Achievement Collectors"
    assert emb.description == "No achievement collectors found yet. Suspiciously unshiny."


def test_scheduler_config_parses_first_monday_before_due_time():
    nxt = ac.parse_schedule(
        "FREQ=MONTHLY;BYDAY=MO;BYSETPOS=1",
        "09:30",
        now=dt.datetime(2026, 2, 1, 12, 0, tzinfo=dt.timezone.utc),
    )
    assert nxt == dt.datetime(2026, 2, 2, 9, 30, tzinfo=dt.timezone.utc)


def test_scheduler_config_parses_first_monday_after_due_time():
    nxt = ac.parse_schedule(
        "FREQ=MONTHLY;BYDAY=MO;BYSETPOS=1",
        "09:30",
        now=dt.datetime(2026, 2, 2, 9, 31, tzinfo=dt.timezone.utc),
    )
    assert nxt == dt.datetime(2026, 3, 2, 9, 30, tzinfo=dt.timezone.utc)


def test_scheduler_config_invalid_rrule_and_time_fail():
    with pytest.raises(ac.AchievementCollectorError): ac.parse_schedule("not rrule", "09:30")
    with pytest.raises(ac.AchievementCollectorError): ac.parse_schedule("FREQ=MONTHLY", "25:99")


def test_preview_publish_rank_routing_embeds_allowed_mentions_and_cache(monkeypatch):
    r=Role(1); guild=Guild([r],[Member(1,"A",[r]), Member(2,"B",[])])
    current=Channel(); publish_ch=Channel(); bot=SimpleNamespace(get_channel=lambda cid: publish_ch, fetch_channel=None, wait_until_ready=lambda: None, is_closed=lambda: True)
    cog = AchievementCollectorCog(bot)
    cog._scheduler.start = lambda: None
    async def fake_config(): return cfg(channel_id=555, default_limit=5, max_limit=5, min_count=1)
    builds = {"n":0}
    async def fake_build(g, c):
        builds["n"] += 1
        return ac.LeaderboardCache(g.id, dt.datetime.now(dt.timezone.utc), (ac.LeaderboardEntry(1,"<@1>","A",1,1),), {1:1,2:0})
    monkeypatch.setattr("cogs.housekeeping_achievement_collector.resolve_config", fake_config)
    monkeypatch.setattr("cogs.housekeeping_achievement_collector.build_leaderboard", fake_build)
    ctx=Ctx(guild,guild.members[0],current)
    run(cog.preview.callback(cog, ctx, None))
    assert ctx.sent[-1]["embed"].title == "Achievement Collectors Preview"
    assert str(ctx.sent[-1]["allowed_mentions"]) == str(ac.discord.AllowedMentions.none())
    run(cog.publish.callback(cog, ctx, 99))
    assert publish_ch.sent[-1]["embed"].title == "Achievement Collectors"
    run(cog.rank.callback(cog, ctx, None))
    run(cog.rank.callback(cog, ctx, guild.members[1]))
    assert "<@1>" in ctx.sent[-2]["embed"].description
    assert "<@2>" in ctx.sent[-1]["embed"].description
    assert builds["n"] == 2  # preview + publish; rank reused publish cache


def test_command_access_gates():
    assert any(getattr(chk, "__qualname__", "").endswith("admin_only.<locals>.predicate") for chk in AchievementCollectorCog.preview.checks)
    assert any(getattr(chk, "__qualname__", "").endswith("admin_only.<locals>.predicate") for chk in AchievementCollectorCog.publish.checks)
    assert not any(getattr(chk, "__qualname__", "").endswith("admin_only.<locals>.predicate") for chk in AchievementCollectorCog.rank.checks)


def _raise_permission_error_with_traceback():
    raise PermissionError()


def test_report_collector_failure_logs_passed_permission_error_explicit_exc_info(monkeypatch):
    monkeypatch.setenv("ACHIEVEMENTS_SHEET_ID", "secret-sheet-id")
    guild = Guild([], [Member(1, "Actor", [])])
    guild.id = 456
    channel = Channel()
    channel.id = 789
    ctx = Ctx(guild, guild.members[0], channel)
    ctx.author.id = 1234
    cog = AchievementCollectorCog(SimpleNamespace())
    ops_logs = []

    async def fake_send_log_message(message):
        ops_logs.append(message)

    monkeypatch.setattr("cogs.housekeeping_achievement_collector.runtime_helpers.send_log_message", fake_send_log_message)

    try:
        _raise_permission_error_with_traceback()
    except PermissionError as exc:
        passed_exc = exc

    with patch("cogs.housekeeping_achievement_collector.log.error") as log_error:
        run(cog._report_collector_failure(ctx, "preview", passed_exc, limit=25))

    log_error.assert_called_once()
    _, kwargs = log_error.call_args
    assert kwargs["exc_info"] == (type(passed_exc), passed_exc, passed_exc.__traceback__)
    assert kwargs["exc_info"][0] is PermissionError
    assert kwargs["exc_info"][1] is passed_exc
    assert kwargs["exc_info"][2] is passed_exc.__traceback__
    assert kwargs["extra"] == {
        "achievement_collector_command": "preview",
        "guild_id": 456,
        "channel_id": 789,
        "actor_id": 1234,
        "provided_limit": 25,
        "target_member_id": None,
        "exception_type": "PermissionError",
    }
    assert ops_logs
    ops_message = ops_logs[-1]
    assert "feature=achievement collector" in ops_message
    assert "command=preview" in ops_message
    assert "exception_type=PermissionError" in ops_message
    assert "exception=-" in ops_message
    assert "secret-sheet-id" not in ops_message
    assert "ACHIEVEMENTS_SHEET_ID" not in ops_message
    assert "sheet contents" not in ops_message
    assert "message content" not in ops_message
    assert "credentials" not in ops_message
    assert "token" not in ops_message


@pytest.mark.parametrize("command_name,limit,target_member_id", [
    ("preview", 25, None),
    ("publish", 26, None),
    ("rank", None, 2),
])
def test_collector_permission_error_empty_message_ops_notification_and_embed(monkeypatch, command_name, limit, target_member_id):
    monkeypatch.setenv("ACHIEVEMENTS_SHEET_ID", "secret-sheet-id")
    guild = Guild([], [Member(1, "Actor", []), Member(2, "Target", [])])
    guild.id = 456
    channel = Channel()
    channel.id = 789
    ctx = Ctx(guild, guild.members[0], channel)
    ctx.author.id = 1234
    cog = AchievementCollectorCog(SimpleNamespace(get_channel=lambda cid: None, fetch_channel=None))
    ops_logs = []

    async def fake_send_log_message(message):
        ops_logs.append(message)

    async def boom(*_args, **_kwargs):
        raise PermissionError()

    monkeypatch.setattr("cogs.housekeeping_achievement_collector.runtime_helpers.send_log_message", fake_send_log_message)
    if command_name in {"preview", "publish"}:
        monkeypatch.setattr(cog, "_rebuild", boom)
    else:
        monkeypatch.setattr(cog, "_get_or_build_cache", boom)

    target = guild.members[1] if target_member_id is not None else None
    with patch("cogs.housekeeping_achievement_collector.log.error") as log_error:
        if command_name == "preview":
            run(cog.preview.callback(cog, ctx, limit))
        elif command_name == "publish":
            run(cog.publish.callback(cog, ctx, limit))
        else:
            run(cog.rank.callback(cog, ctx, target))

    _, kwargs = log_error.call_args
    exc_info = kwargs["exc_info"]
    assert exc_info is not None
    assert exc_info[0] is PermissionError
    assert isinstance(exc_info[1], PermissionError)
    assert exc_info[2] is exc_info[1].__traceback__
    assert ctx.sent[-1]["embed"].title == "Achievement Collector"
    ops_message = ops_logs[-1]
    assert "feature=achievement collector" in ops_message
    assert f"command={command_name}" in ops_message
    assert "exception_type=PermissionError" in ops_message
    assert "exception=-" in ops_message
    assert "secret-sheet-id" not in ops_message
    assert "ACHIEVEMENTS_SHEET_ID" not in ops_message
    assert "sheet contents" not in ops_message
    assert "message content" not in ops_message
    assert "credentials" not in ops_message
    assert "token" not in ops_message

@pytest.mark.parametrize("command_name,limit,target_member_id", [
    ("preview", 7, None),
    ("publish", 8, None),
    ("rank", None, 2),
])
def test_collector_unexpected_exception_logs_traceback_and_ops_notification(monkeypatch, caplog, command_name, limit, target_member_id):
    monkeypatch.setenv("ACHIEVEMENTS_SHEET_ID", "secret-sheet-id")
    r = Role(1)
    guild = Guild([r], [Member(1, "Actor", [r]), Member(2, "Target", [])])
    guild.id = 456
    current = Channel()
    current.id = 789
    bot = SimpleNamespace(get_channel=lambda cid: None, fetch_channel=None, wait_until_ready=lambda: None, is_closed=lambda: True)
    cog = AchievementCollectorCog(bot)
    ops_logs = []

    async def fake_send_log_message(message):
        ops_logs.append(message)

    async def boom(*_args, **_kwargs):
        raise RuntimeError("boom secret-sheet-id sheet contents: shiny row message content: !achievementcollector preview")

    monkeypatch.setattr("cogs.housekeeping_achievement_collector.runtime_helpers.send_log_message", fake_send_log_message)
    if command_name in {"preview", "publish"}:
        monkeypatch.setattr(cog, "_rebuild", boom)
    else:
        monkeypatch.setattr(cog, "_get_or_build_cache", boom)

    ctx = Ctx(guild, guild.members[0], current)
    ctx.author.id = 1234
    target = guild.members[1] if target_member_id is not None else None

    with caplog.at_level("ERROR", logger="c1c.housekeeping.achievement_collector.cog"):
        if command_name == "preview":
            run(cog.preview.callback(cog, ctx, limit))
        elif command_name == "publish":
            run(cog.publish.callback(cog, ctx, limit))
        else:
            run(cog.rank.callback(cog, ctx, target))

    record = next(rec for rec in caplog.records if rec.message == f"achievement collector {command_name} failed")
    assert record.exc_info is not None
    assert record.exc_info[0] is RuntimeError
    assert ctx.sent[-1]["embed"].title == "Achievement Collector"
    assert ops_logs
    ops_message = ops_logs[-1]
    assert "feature=achievement collector" in ops_message
    assert f"command={command_name}" in ops_message
    assert "guild_id=456" in ops_message
    assert "channel_id=789" in ops_message
    assert "actor_id=1234" in ops_message
    assert "exception_type=RuntimeError" in ops_message
    if limit is not None:
        assert f"limit={limit}" in ops_message
    if target_member_id is not None:
        assert f"target_member_id={target_member_id}" in ops_message
    assert "secret-sheet-id" not in ops_message
    assert "shiny row" not in ops_message
    assert "!achievementcollector preview" not in ops_message
