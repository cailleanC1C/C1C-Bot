from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from modules.housekeeping import guides_help_index as ghi


class AsyncIter:
    def __init__(self, items):
        self.items = list(items)

    def __aiter__(self):
        self._it = iter(self.items)
        return self

    async def __anext__(self):
        try:
            return next(self._it)
        except StopIteration:
            raise StopAsyncIteration


class Message:
    def __init__(self, id, content="old", pinned=False, pin_fails=False):
        self.id = id
        self.content = content
        self.pinned = pinned
        self.pin_fails = pin_fails
        self.deleted = False
        self.edited = []

    async def edit(self, *, content):
        self.content = content
        self.edited.append(content)

    async def delete(self):
        self.deleted = True

    async def pin(self):
        if self.pin_fails:
            raise ghi.discord.HTTPException(response=None, message="nope")
        self.pinned = True


class Target:
    def __init__(self, messages=None):
        self.messages = {m.id: m for m in (messages or [])}
        self.sent = []
        self.id = 60
        self.guild = SimpleNamespace(name="guild")

    async def fetch_message(self, mid):
        if mid not in self.messages:
            raise ghi.discord.NotFound(response=None, message="missing")
        return self.messages[mid]

    async def send(self, content):
        m = Message(1000 + len(self.sent), content)
        self.sent.append(m)
        return m


class Bot:
    def __init__(self, channels):
        self.channels = channels

    async def wait_until_ready(self):
        pass

    def get_channel(self, cid):
        return self.channels.get(cid)

    async def fetch_channel(self, cid):
        return self.channels.get(cid)


def tag(id, name, position=0):
    return SimpleNamespace(id=id, name=name, position=position)


def thread(id, name, tags, created=0):
    return SimpleNamespace(
        id=id,
        name=name,
        applied_tags=tags,
        mention=f"<#T{id}>",
        created_at=datetime.fromtimestamp(created, timezone.utc),
    )


def forum(id, name, tags, threads=(), pos=0, archived=()):
    return SimpleNamespace(
        id=id,
        name=name,
        available_tags=list(tags),
        threads=list(threads),
        position=pos,
        archived_threads=lambda limit=None: AsyncIter(archived),
    )


async def _noop(*args, **kwargs):
    pass


async def _ret(v):
    return v


def test_discover_forum_channels_from_category_and_blacklist():
    f1 = forum(1, "f1", [])
    f2 = forum(2, "f2", [])
    cat = SimpleNamespace(channels=[f2, SimpleNamespace(id=3), f1])
    assert [f.id for f in ghi.discover_forum_channels(cat, blacklist={"2"})] == [1]


def test_build_ignores_untagged_and_groups_tagged_posts():
    a = tag(10, "Missions", 0)
    f = forum(1, "forum", [a], [thread(11, "x", []), thread(12, "y", [a])])
    messages, posts, groups = ghi.build_index_messages([f], {1: f.threads})
    assert posts == 1 and groups == 1
    assert "## Missions" in messages[0] and "<#T12>" in messages[0]
    assert "<#T11>" not in messages[0]


def test_build_multi_tag_thread_appears_under_each_group():
    a = tag(10, "Missions", 0)
    b = tag(11, "Faction Wars", 1)
    f = forum(1, "forum", [a, b], [thread(12, "y", [a, b])])
    body = "\n".join(ghi.build_index_messages([f], {1: f.threads})[0])
    assert "## Missions" in body and "## Faction Wars" in body
    assert body.count("<#T12>") == 2


def test_tag_groups_follow_available_tags_order_without_position():
    a = SimpleNamespace(id=10, name="Missions")
    b = SimpleNamespace(id=11, name="Faction Wars")
    f = forum(1, "forum", [b, a], [thread(12, "mission", [a]), thread(13, "war", [b])])

    body = "\n".join(ghi.build_index_messages([f], {1: f.threads})[0])

    assert body.index("## Faction Wars") < body.index("## Missions")


def test_tag_and_post_blacklists_hide_content():
    a = tag(10, "Missions", 0)
    b = tag(11, "Faction Wars", 1)
    f = forum(1, "forum", [a, b], [thread(12, "y", [a]), thread(13, "z", [b])])
    messages, posts, groups = ghi.build_index_messages(
        [f], {1: f.threads}, tag_blacklist={"Faction Wars"}, post_blacklist={"12"}
    )
    body = "\n".join(messages)
    assert posts == 0 and groups == 0
    assert "Missions" not in body and "Faction Wars" not in body


def test_output_splits_when_threshold_exceeded():
    a = tag(10, "Missions", 0)
    b = tag(11, "Faction Wars", 1)
    f = forum(1, "forum", [a, b], [thread(12, "long", [a]), thread(13, "long2", [b])])
    messages, _, _ = ghi.build_index_messages([f], {1: f.threads}, threshold=90)
    assert len(messages) > 1


def test_archived_tagged_posts_are_indexed():
    a = tag(10, "Missions", 0)
    archived = thread(14, "archived", [a])
    f = forum(1, "forum", [a], [], archived=[archived])

    async def run():
        threads, _ = await ghi._collect_forum_threads(f)
        messages, posts, groups = ghi.build_index_messages([f], {1: threads})
        assert posts == 1 and groups == 1
        assert "<#T14>" in messages[0]

    asyncio.run(run())


def test_scheduler_interval():
    now = datetime(2026, 7, 18, tzinfo=timezone.utc)
    assert not ghi.should_refresh(now - timedelta(hours=12), 1, now=now)
    assert ghi.should_refresh(now - timedelta(days=2), 1, now=now)


def test_disabled_skips(monkeypatch):
    async def run():
        monkeypatch.setattr(ghi.feature_flags, "is_enabled", lambda key: False)
        monkeypatch.setattr(
            ghi.runtime_helpers, "send_log_message", lambda msg: _noop()
        )
        result = await ghi.refresh_guides_help_index(Bot({}), force=True)
        assert result.status == "disabled" and result.reason == "feature_disabled"

    asyncio.run(run())


def test_missing_source_and_target_config(monkeypatch):
    async def run():
        monkeypatch.setattr(ghi.feature_flags, "is_enabled", lambda key: True)

        async def cfg(key, default=None):
            return None

        monkeypatch.setattr(ghi, "_config", cfg)
        assert (
            await ghi.refresh_guides_help_index(Bot({}), force=True)
        ).reason == "missing_source_category"

        async def cfg2(key, default=None):
            return "1" if key == ghi.CONFIG_SOURCE_CATEGORY_ID else None

        monkeypatch.setattr(ghi, "_config", cfg2)
        assert (
            await ghi.refresh_guides_help_index(
                Bot({1: SimpleNamespace(id=1, channels=[])}), force=True
            )
        ).reason == "missing_target_channel"

    asyncio.run(run())


def test_refresh_edits_reuses_deletes_and_pin_nonfatal(monkeypatch):
    async def run():
        a = tag(10, "Missions", 0)
        f = forum(1, "forum", [a], [thread(12, "y", [a])])
        target = Target([Message(201, "old", pin_fails=True), Message(202, "stale")])
        bot = Bot({50: SimpleNamespace(id=50, channels=[f]), 60: target})
        monkeypatch.setattr(ghi.feature_flags, "is_enabled", lambda key: True)

        async def cfg(key, default=None):
            return {
                ghi.CONFIG_SOURCE_CATEGORY_ID: "50",
                ghi.CONFIG_TARGET_CHANNEL_ID: "60",
                ghi.CONFIG_REFRESH_DAYS: "1",
            }.get(key, default)

        monkeypatch.setattr(ghi, "_config", cfg)
        monkeypatch.setattr(
            ghi.runtime_helpers,
            "resolve_configured_text_channel",
            lambda *a, **k: _ret((target, None)),
        )
        monkeypatch.setattr(
            ghi.runtime_helpers, "send_log_message", lambda msg: _noop()
        )
        monkeypatch.setattr(
            ghi.server_map_state,
            "fetch_state",
            lambda: _ret(
                {
                    ghi.STATE_MESSAGE_PREFIX + "1": "201",
                    ghi.STATE_MESSAGE_PREFIX + "2": "202",
                }
            ),
        )
        saved = {}

        async def save(entries):
            saved.update(entries)

        monkeypatch.setattr(ghi.server_map_state, "update_state", save)
        result = await ghi.refresh_guides_help_index(bot, force=True, actor="command")
        assert result.status == "ok"
        assert target.messages[201].edited and target.messages[202].deleted
        assert saved[ghi.STATE_MESSAGE_PREFIX + "1"] == "201"
        assert saved[ghi.STATE_MESSAGE_PREFIX + "2"] == ""

    asyncio.run(run())


def test_message_lifecycle_failures_return_clear_reasons(monkeypatch):
    async def run():
        a = tag(10, "Missions", 0)
        f = forum(1, "forum", [a], [thread(12, "y", [a])])
        bot = Bot({50: SimpleNamespace(id=50, channels=[f])})
        monkeypatch.setattr(ghi.feature_flags, "is_enabled", lambda key: True)

        async def cfg(key, default=None):
            return {
                ghi.CONFIG_SOURCE_CATEGORY_ID: "50",
                ghi.CONFIG_TARGET_CHANNEL_ID: "60",
                ghi.CONFIG_REFRESH_DAYS: "1",
            }.get(key, default)

        monkeypatch.setattr(ghi, "_config", cfg)
        monkeypatch.setattr(
            ghi.runtime_helpers, "send_log_message", lambda msg: _noop()
        )
        monkeypatch.setattr(ghi.server_map_state, "fetch_state", lambda: _ret({}))
        monkeypatch.setattr(
            ghi.server_map_state, "update_state", lambda entries: _noop()
        )

        class SendFailTarget(Target):
            async def send(self, content):
                raise ghi.discord.HTTPException(
                    response=SimpleNamespace(status=500, reason="boom"),
                    message="boom",
                )

        send_fail = SendFailTarget([])
        monkeypatch.setattr(
            ghi.runtime_helpers,
            "resolve_configured_text_channel",
            lambda *a, **k: _ret((send_fail, None)),
        )
        assert (
            await ghi.refresh_guides_help_index(bot, force=True)
        ).reason == "message_send_failed"

        class EditFailMessage(Message):
            async def edit(self, *, content):
                raise ghi.discord.HTTPException(
                    response=SimpleNamespace(status=500, reason="boom"),
                    message="boom",
                )

        edit_fail = Target([EditFailMessage(201, "old")])
        monkeypatch.setattr(
            ghi.runtime_helpers,
            "resolve_configured_text_channel",
            lambda *a, **k: _ret((edit_fail, None)),
        )
        monkeypatch.setattr(
            ghi.server_map_state,
            "fetch_state",
            lambda: _ret({ghi.STATE_MESSAGE_PREFIX + "1": "201"}),
        )
        assert (
            await ghi.refresh_guides_help_index(bot, force=True)
        ).reason == "message_edit_failed"

        target = Target([])
        monkeypatch.setattr(
            ghi.runtime_helpers,
            "resolve_configured_text_channel",
            lambda *a, **k: _ret((target, None)),
        )
        monkeypatch.setattr(ghi.server_map_state, "fetch_state", lambda: _ret({}))

        async def state_fail(entries):
            raise RuntimeError("sheet unavailable")

        monkeypatch.setattr(ghi.server_map_state, "update_state", state_fail)
        assert (
            await ghi.refresh_guides_help_index(bot, force=True)
        ).reason == "state_update_failed"

    asyncio.run(run())


def test_manual_force_bypasses_interval(monkeypatch):
    async def run():
        a = tag(10, "Missions", 0)
        f = forum(1, "forum", [a], [thread(12, "y", [a])])
        target = Target([])
        bot = Bot({50: SimpleNamespace(id=50, channels=[f]), 60: target})
        monkeypatch.setattr(ghi.feature_flags, "is_enabled", lambda key: True)

        async def cfg(key, default=None):
            return {
                ghi.CONFIG_SOURCE_CATEGORY_ID: "50",
                ghi.CONFIG_TARGET_CHANNEL_ID: "60",
                ghi.CONFIG_REFRESH_DAYS: "99",
            }.get(key, default)

        monkeypatch.setattr(ghi, "_config", cfg)
        monkeypatch.setattr(
            ghi.runtime_helpers,
            "resolve_configured_text_channel",
            lambda *a, **k: _ret((target, None)),
        )
        monkeypatch.setattr(
            ghi.runtime_helpers, "send_log_message", lambda msg: _noop()
        )
        monkeypatch.setattr(
            ghi.server_map_state,
            "fetch_state",
            lambda: _ret({ghi.STATE_LAST_RUN_AT: ghi._now_iso()}),
        )
        monkeypatch.setattr(
            ghi.server_map_state, "update_state", lambda entries: _noop()
        )
        assert (
            await ghi.refresh_guides_help_index(bot, force=False)
        ).reason == "interval_not_elapsed"
        assert (await ghi.refresh_guides_help_index(bot, force=True)).status == "ok"

    asyncio.run(run())
