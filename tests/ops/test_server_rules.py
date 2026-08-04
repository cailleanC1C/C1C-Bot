import asyncio
import discord
from unittest.mock import AsyncMock

from modules.ops import server_rules
from cogs.app_admin import AppAdmin


class Msg:
    def __init__(self, id, content="", author_id=42):
        self.id = id
        self.content = content
        self.author = type("A", (), {"id": author_id})()
        self.deleted = False
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)
        self.content = kw.get("content", self.content)

    async def delete(self):
        self.deleted = True


class Chan:
    def __init__(self, id=9):
        self.id = id
        self.guild = type("G", (), {"id": 1, "name": "Guild"})()
        self.sent = []
        self.messages = {}

    async def send(self, **kw):
        msg = Msg(100 + len(self.sent), kw.get("content", ""))
        msg.kw = kw
        self.sent.append(msg)
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, mid):
        if mid not in self.messages:
            raise discord.NotFound(response=None, message="missing")
        return self.messages[mid]

    def history(self, limit=500):
        async def gen():
            for m in list(self.messages.values()):
                yield m

        return gen()


class Bot:
    def __init__(self, chan):
        self.chan = chan
        self.user = type("U", (), {"id": 42})()

    def get_channel(self, id):
        return self.chan if id == self.chan.id else None


async def fake_load(monkeypatch, rows, chan):
    async def cfg(key, default=None, force=False):
        return {
            "SERVER_RULES_FAQ_TAB": "Rules",
            "SERVER_RULES_FAQ_CHANNEL_ID": str(chan.id),
        }.get(key, default)

    monkeypatch.setattr(server_rules.recruitment_sheet, "get_config_value_async", cfg)
    monkeypatch.setattr(
        server_rules.recruitment_sheet, "get_recruitment_sheet_id", lambda: "sheet"
    )
    monkeypatch.setattr(
        server_rules.sheets_core, "afetch_values", AsyncMock(return_value=rows)
    )
    monkeypatch.setattr(
        server_rules, "resolve_message_target", AsyncMock(return_value=chan)
    )
    writes = []
    monkeypatch.setattr(
        server_rules.sheets_core, "aget_worksheet", AsyncMock(return_value=object())
    )

    async def write(ws, rng, vals, timeout=None):
        writes.append((rng, vals))

    monkeypatch.setattr(server_rules.async_adapter, "aworksheet_values_update", write)
    return writes


def sheet(*body):
    return [
        [
            "review_notes",
            "message_id",
            "enabled",
            "description",
            "message_key",
            "thumbnail_url",
            "title",
            "order",
            "colour",
            "footer",
            "review_status",
            "section",
        ],
        *body,
    ]


def test_publish_sorts_and_writes_message_ids_header_order_independent(monkeypatch):
    async def run():
        chan = Chan()
        writes = await fake_load(
            monkeypatch,
            sheet(
                [
                    "human",
                    "",
                    "yes",
                    "Second",
                    "b",
                    "",
                    "Title B",
                    "2",
                    "#00ff00",
                    "foot",
                    "draft",
                    "faq",
                ],
                [
                    "human",
                    "",
                    "true",
                    "First",
                    "a",
                    "https://e.test/t.png",
                    "Title A",
                    "1",
                    "community",
                    "",
                    "ok",
                    "rules",
                ],
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 2
        assert [m.kw["embed"].title for m in chan.sent] == ["Title A", "Title B"]
        assert chan.sent[0].kw["embed"].thumbnail.url == "https://e.test/t.png"
        assert writes == [("B3", [["100"]]), ("B2", [["101"]])]

    asyncio.run(run())


def test_preflight_abort_no_mutations_on_invalid_rows(monkeypatch):
    async def run():
        chan = Chan()
        writes = await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    "",
                    "yes",
                    "",
                    "dup",
                    "ftp://bad",
                    "",
                    "x",
                    "notacolor",
                    "",
                    "",
                    "",
                ],
                ["", "", "true", "ok", "dup", "", "T", "1", "#fff", "", "", ""],
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.failed >= 4
        assert chan.sent == []
        assert writes == []

    asyncio.run(run())


def test_refresh_edits_recreates_and_deletes(monkeypatch):
    async def run():
        chan = Chan()
        existing = Msg(555, server_rules.marker_for("keep"))
        chan.messages[555] = existing
        disabled = Msg(777, server_rules.marker_for("old"))
        chan.messages[777] = disabled
        writes = await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    "555",
                    "yes",
                    "Updated",
                    "keep",
                    "",
                    "Keep",
                    "1",
                    "#00ff00",
                    "",
                    "",
                    "",
                ],
                [
                    "",
                    "666",
                    "on",
                    "New",
                    "missing",
                    "",
                    "Missing",
                    "2",
                    "#00ff00",
                    "",
                    "",
                    "",
                ],
                ["", "777", "no", "Old", "old", "", "Old", "3", "#00ff00", "", "", ""],
            ),
            chan,
        )
        summary, _ = await server_rules.refresh(Bot(chan))
        assert summary.refreshed == 1
        assert summary.created == 1
        assert summary.removed == 1
        assert existing.edits
        assert disabled.deleted
        assert ("B3", [["100"]]) in writes
        assert ("B4", [[""]]) in writes

    asyncio.run(run())


def test_unrelated_messages_untouched_on_rebuild(monkeypatch):
    async def run():
        chan = Chan()
        unrelated = Msg(1, "hello")
        chan.messages[1] = unrelated
        old = Msg(2, server_rules.marker_for("rule"))
        chan.messages[2] = old
        await fake_load(
            monkeypatch,
            sheet(["", "2", "yes", "D", "rule", "", "T", "1", "#00ff00", "", "", ""]),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 1
        assert old.deleted
        assert not unrelated.deleted

    asyncio.run(run())


def test_admin_usage_embed():
    async def run():
        cog = AppAdmin(Bot(Chan()))
        ctx = type("Ctx", (), {"reply": AsyncMock()})()
        await cog.serverrules.callback(cog, ctx)
        kwargs = ctx.reply.call_args.kwargs
        assert (
            kwargs["embed"].description
            == "Use `!serverrules publish` or `!serverrules refresh`."
        )
        assert "content" not in kwargs

    asyncio.run(run())


def test_no_scheduler_path_introduced():
    from pathlib import Path

    text = Path("modules/ops/server_rules.py").read_text()
    assert "scheduler" not in text.lower()


def test_serverrules_commands_are_admin_gated():
    assert AppAdmin.serverrules.checks
    assert AppAdmin.serverrules_publish.checks
    assert AppAdmin.serverrules_refresh.checks


def test_destination_resolution_supports_configured_target(monkeypatch):
    async def run():
        chan = Chan(id=123)
        resolver = AsyncMock(return_value=chan)
        await fake_load(
            monkeypatch,
            sheet(["", "", "true", "D", "k", "", "T", "1", "#00ff00", "", "", ""]),
            chan,
        )
        monkeypatch.setattr(server_rules, "resolve_message_target", resolver)
        await server_rules.preflight(Bot(chan))
        resolver.assert_awaited_once()
        assert resolver.await_args.args[1] == 123

    asyncio.run(run())


def test_enabled_value_parser_accepts_common_sheet_values():
    assert server_rules.parse_enabled("yes") is True
    assert server_rules.parse_enabled("ON") is True
    assert server_rules.parse_enabled("0") is False
    assert server_rules.parse_enabled("maybe") is None
