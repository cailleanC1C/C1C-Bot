import asyncio
import discord
from unittest.mock import AsyncMock

from modules.ops import server_rules
from cogs.server_rules import ServerRulesCog


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


class Ws:
    def __init__(self, batch_writes):
        self.batch_writes = batch_writes

    def batch_update(self, payload):
        self.batch_writes.append(payload)
        return {"updated": len(payload)}


async def fake_load(monkeypatch, rows, chan):
    async def cfg(key, default=None, force=False):
        return {
            "SERVER_RULES_FAQ_TAB": "Rules",
            "SERVER_RULES_FAQ_CHANNEL_ID": str(chan.id),
        }.get(key, default)

    monkeypatch.setattr(server_rules.recruitment_sheet, "get_config_value_async", cfg)
    monkeypatch.setattr(server_rules, "_mirralith_sheet_id", lambda: "mirralith-sheet")
    monkeypatch.setattr(
        server_rules.sheets_core, "afetch_values", AsyncMock(return_value=rows)
    )
    monkeypatch.setattr(
        server_rules, "resolve_message_target", AsyncMock(return_value=chan)
    )
    writes = []
    batch_writes = []
    ws = Ws(batch_writes)
    monkeypatch.setattr(
        server_rules.sheets_core, "aget_worksheet", AsyncMock(return_value=ws)
    )

    async def write(ws, rng, vals, timeout=None):
        writes.append((rng, vals))

    async def acall(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(server_rules.async_adapter, "aworksheet_values_update", write)
    monkeypatch.setattr(server_rules.sheets_core, "acall_with_backoff", acall)
    return {"single": writes, "batch": batch_writes}


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
                    "community",
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
        assert writes["batch"] == [
            [{"range": "B3", "values": [["100"]]}, {"range": "B2", "values": [["101"]]}]
        ]
        assert writes["single"] == []

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
                ["", "", "true", "ok", "dup", "", "T", "1", "notacolor", "", "", ""],
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.failed >= 4
        assert chan.sent == []
        assert writes["single"] == []
        assert writes["batch"] == []

    asyncio.run(run())


def test_refresh_edits_recreates_and_deletes(monkeypatch):
    async def run():
        chan = Chan()
        existing = Msg(555555555555555555, server_rules.marker_for("keep"))
        chan.messages[555555555555555555] = existing
        disabled = Msg(777777777777777777, server_rules.marker_for("old"))
        chan.messages[777777777777777777] = disabled
        writes = await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    "555555555555555555",
                    "yes",
                    "Updated",
                    "keep",
                    "",
                    "Keep",
                    "1",
                    "community",
                    "",
                    "",
                    "",
                ],
                [
                    "",
                    "666666666666666666",
                    "on",
                    "New",
                    "missing",
                    "",
                    "Missing",
                    "2",
                    "community",
                    "",
                    "",
                    "",
                ],
                [
                    "",
                    "777777777777777777",
                    "no",
                    "Old",
                    "old",
                    "",
                    "Old",
                    "3",
                    "community",
                    "",
                    "",
                    "",
                ],
            ),
            chan,
        )
        original_fetch = server_rules._fetch

        async def fake_fetch(target, message_id):
            if message_id == "666666666666666666":
                return (
                    server_rules.FetchState.MISSING,
                    None,
                    "stored message was not found",
                )
            return await original_fetch(target, message_id)

        monkeypatch.setattr(server_rules, "_fetch", fake_fetch)
        summary, _ = await server_rules.refresh(Bot(chan))
        assert summary.refreshed == 1
        assert summary.created == 1
        assert summary.removed == 1
        assert existing.edits
        assert disabled.deleted
        assert ("B3", [["100"]]) in writes["single"]
        assert ("B4", [[""]]) in writes["single"]

    asyncio.run(run())


def test_unrelated_messages_untouched_on_rebuild(monkeypatch):
    async def run():
        chan = Chan()
        unrelated = Msg(1, "hello")
        chan.messages[1] = unrelated
        old = Msg(222222222222222222, server_rules.marker_for("rule"))
        chan.messages[222222222222222222] = old
        await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    "222222222222222222",
                    "yes",
                    "D",
                    "rule",
                    "",
                    "T",
                    "1",
                    "community",
                    "",
                    "",
                    "",
                ]
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 1
        assert old.deleted
        assert not unrelated.deleted

    asyncio.run(run())


def test_admin_usage_embed():
    async def run():
        cog = ServerRulesCog(Bot(Chan()))
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
    assert ServerRulesCog.serverrules.checks
    assert ServerRulesCog.publish.checks
    assert ServerRulesCog.refresh.checks


def test_destination_resolution_supports_configured_target(monkeypatch):
    async def run():
        chan = Chan(id=123)
        resolver = AsyncMock(return_value=chan)
        await fake_load(
            monkeypatch,
            sheet(["", "", "true", "D", "k", "", "T", "1", "community", "", "", ""]),
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


def test_rules_tab_reads_mirralith_and_not_recruitment_accessor(monkeypatch):
    async def run():
        chan = Chan()

        async def cfg(key, default=None, force=False):
            return {
                "SERVER_RULES_FAQ_TAB": "Rules",
                "SERVER_RULES_FAQ_CHANNEL_ID": str(chan.id),
            }.get(key, default)

        monkeypatch.setattr(
            server_rules.recruitment_sheet, "get_config_value_async", cfg
        )
        monkeypatch.setattr(
            server_rules.recruitment_sheet,
            "get_recruitment_sheet_id",
            lambda: (_ for _ in ()).throw(AssertionError("recruitment accessor used")),
        )
        monkeypatch.setattr(
            server_rules, "_mirralith_sheet_id", lambda: "mirralith-sheet"
        )
        calls = []

        async def read(sheet_id, tab):
            calls.append((sheet_id, tab))
            return sheet(
                ["", "", "true", "D", "k", "", "T", "1", "community", "", "", ""]
            )

        monkeypatch.setattr(server_rules.sheets_core, "afetch_values", read)
        monkeypatch.setattr(
            server_rules, "resolve_message_target", AsyncMock(return_value=chan)
        )
        await server_rules.preflight(Bot(chan))
        assert calls == [("mirralith-sheet", "Rules")]

    asyncio.run(run())


def test_invalid_colour_duplicate_nonfinite_order_and_snowflake_rejected(monkeypatch):
    async def run():
        chan = Chan()
        writes = await fake_load(
            monkeypatch,
            sheet(
                ["", "123", "true", "D", "badid", "", "T", "nan", "purple", "", "", ""],
                ["", "", "true", "D", "one", "", "T", "1", "community", "", "", ""],
                ["", "", "true", "D", "two", "", "T", "1", "community", "", "", ""],
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.failed >= 4
        assert chan.sent == []
        assert writes["single"] == []
        assert writes["batch"] == []

    asyncio.run(run())


def test_feature_disabled_stops_without_mutations(monkeypatch):
    async def run():
        chan = Chan()
        cog = ServerRulesCog(Bot(chan))
        ctx = type(
            "Ctx",
            (),
            {"reply": AsyncMock(), "author": type("A", (), {"id": 5})(), "guild": None},
        )()
        monkeypatch.setattr(
            "cogs.server_rules.feature_flags.is_enabled", lambda key: False
        )
        monkeypatch.setattr(
            "cogs.server_rules.runtime_helpers.send_log_message", AsyncMock()
        )
        monkeypatch.setattr(
            server_rules, "publish", AsyncMock(side_effect=AssertionError("mutated"))
        )
        await cog.publish.callback(cog, ctx)
        assert ctx.reply.call_args.kwargs["embed"].title == "Server rules disabled"
        assert chan.sent == []

    asyncio.run(run())


def test_publish_send_failure_deletes_new_and_keeps_sheet_ids(monkeypatch):
    async def run():
        chan = Chan()
        writes = await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    "111111111111111111",
                    "true",
                    "D",
                    "a",
                    "",
                    "A",
                    "1",
                    "community",
                    "",
                    "",
                    "",
                ],
                [
                    "",
                    "222222222222222222",
                    "true",
                    "D",
                    "b",
                    "",
                    "B",
                    "2",
                    "community",
                    "",
                    "",
                    "",
                ],
            ),
            chan,
        )
        original_send = chan.send

        async def send(**kw):
            if chan.sent:
                raise discord.HTTPException(response=None, message="boom")
            return await original_send(**kw)

        chan.send = send
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.failed == 1
        assert chan.sent[0].deleted
        assert writes["single"] == []
        assert writes["batch"] == []

    asyncio.run(run())


def test_publish_sheet_failure_restores_ids_and_deletes_new(monkeypatch):
    async def run():
        chan = Chan()
        await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    "111111111111111111",
                    "true",
                    "D",
                    "a",
                    "",
                    "A",
                    "1",
                    "community",
                    "",
                    "",
                    "",
                ]
            ),
            chan,
        )
        batch_calls = []

        async def acall(func, payload, **kwargs):
            batch_calls.append(payload)
            if payload != [{"range": "B2", "values": [["111111111111111111"]]}]:
                raise RuntimeError("sheet down")
            return {"restored": True}

        monkeypatch.setattr(server_rules.sheets_core, "acall_with_backoff", acall)
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.failed == 1
        assert chan.sent[0].deleted
        assert [{"range": "B2", "values": [["111111111111111111"]]}] in batch_calls

    asyncio.run(run())


def test_refresh_temp_fetch_failure_does_not_duplicate(monkeypatch):
    async def run():
        chan = Chan()
        await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    "111111111111111111",
                    "true",
                    "D",
                    "a",
                    "",
                    "A",
                    "1",
                    "community",
                    "",
                    "",
                    "",
                ]
            ),
            chan,
        )

        async def fetch(mid):
            raise discord.Forbidden(response=None, message="no")

        chan.fetch_message = fetch
        summary, _ = await server_rules.refresh(Bot(chan))
        assert summary.failed == 1
        assert chan.sent == []

    asyncio.run(run())


def test_refresh_replacement_write_failure_cleans_replacement(monkeypatch):
    async def run():
        chan = Chan()
        await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    "111111111111111111",
                    "true",
                    "D",
                    "a",
                    "",
                    "A",
                    "1",
                    "community",
                    "",
                    "",
                    "",
                ]
            ),
            chan,
        )

        async def write(ws, rng, vals, timeout=None):
            raise RuntimeError("sheet down")

        monkeypatch.setattr(
            server_rules.async_adapter, "aworksheet_values_update", write
        )

        async def missing(target, message_id):
            return server_rules.FetchState.MISSING, None, "stored message was not found"

        monkeypatch.setattr(server_rules, "_fetch", missing)
        summary, _ = await server_rules.refresh(Bot(chan))
        assert summary.failed == 1
        assert chan.sent[0].deleted

    asyncio.run(run())


def actual_sheet(*body):
    return [
        [
            "message_key",
            "section",
            "order",
            "enabled",
            "title",
            "description",
            "colour",
            "thumbnail_url",
            "footer",
            "review_status",
            "review_notes",
            "message_id",
        ],
        *body,
    ]


def test_trailing_false_placeholder_rows_are_ignored_with_actual_header_order(
    monkeypatch,
):
    async def run():
        chan = Chan()
        writes = await fake_load(
            monkeypatch,
            actual_sheet(
                [
                    "rules",
                    "rules",
                    "1",
                    "TRUE",
                    "Rules",
                    "Read <#123>",
                    "blue",
                    "",
                    "",
                    "approved",
                    "",
                    "",
                ],
                ["", "", "", "FALSE", "", "", "", "", "", "", "", ""],
                ["", "", "", "FALSE", "", "", "", "", "", "", "", ""],
                ["", "", "", "FALSE", "", "", "", "", "", "", "", ""],
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 1
        assert len(chan.sent) == 1
        assert writes["batch"] == [[{"range": "L2", "values": [["100"]]}]]

    asyncio.run(run())


def test_non_empty_malformed_false_row_is_rejected(monkeypatch):
    async def run():
        chan = Chan()
        writes = await fake_load(
            monkeypatch,
            actual_sheet(
                [
                    "rules",
                    "rules",
                    "1",
                    "TRUE",
                    "Rules",
                    "Read",
                    "blue",
                    "",
                    "",
                    "approved",
                    "",
                    "",
                ],
                [
                    "",
                    "faq",
                    "",
                    "FALSE",
                    "",
                    "Has data but no key",
                    "blue",
                    "",
                    "",
                    "",
                    "",
                    "",
                ],
                ["", "", "", "FALSE", "", "", "", "", "", "", "", ""],
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.failed >= 1
        assert "row 3" in summary.failures
        assert chan.sent == []
        assert writes["batch"] == []

    asyncio.run(run())


def test_supported_colour_names_and_hex_rejection():
    assert server_rules.parse_colour(" #4472C4 ") is not None
    assert server_rules.parse_colour("#356854") is not None
    assert server_rules.parse_colour("#FFD666") is not None
    assert server_rules.parse_colour("#607D8B") is not None
    assert server_rules.parse_colour("community") == server_rules.get_embed_colour(
        "community"
    )
    assert server_rules.parse_colour("blue") == server_rules.get_embed_colour(
        "community"
    )
    assert server_rules.parse_colour("c1c_blue") == server_rules.get_embed_colour(
        "community"
    )
    assert server_rules.parse_colour("recruitment") == server_rules.get_embed_colour(
        "recruitment"
    )
    assert server_rules.parse_colour("green") == server_rules.get_embed_colour(
        "recruitment"
    )
    assert server_rules.parse_colour("admin") == server_rules.get_embed_colour("admin")
    assert server_rules.parse_colour("#00ff00") is None
    assert server_rules.parse_colour("purple") is None


def test_feature_enabled_invokes_publish(monkeypatch):
    async def run():
        chan = Chan()
        cog = ServerRulesCog(Bot(chan))
        ctx = type(
            "Ctx",
            (),
            {"reply": AsyncMock(), "author": type("A", (), {"id": 5})(), "guild": None},
        )()
        monkeypatch.setattr(
            "cogs.server_rules.feature_flags.is_enabled", lambda key: True
        )
        monkeypatch.setattr(
            "cogs.server_rules.runtime_helpers.send_log_message", AsyncMock()
        )
        called = AsyncMock(return_value=(server_rules.Summary(created=1), chan))
        monkeypatch.setattr(server_rules, "publish", called)
        await cog.publish.callback(cog, ctx)
        called.assert_awaited_once()
        assert ctx.reply.call_args.kwargs["embed"].title == "Server rules publish"

    asyncio.run(run())


def test_rebuild_deletes_known_old_managed_message_by_stored_id_without_history(
    monkeypatch,
):
    async def run():
        chan = Chan()
        old = Msg(333333333333333333, server_rules.marker_for("old"))
        chan.messages[333333333333333333] = old
        await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    "333333333333333333",
                    "true",
                    "D",
                    "old",
                    "",
                    "Old",
                    "1",
                    "community",
                    "",
                    "",
                    "",
                ]
            ),
            chan,
        )

        def no_history(limit=500):
            async def gen():
                if False:
                    yield None

            return gen()

        chan.history = no_history
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 1
        assert old.deleted

    asyncio.run(run())


def test_rebuild_does_not_delete_unrelated_stored_message(monkeypatch):
    async def run():
        chan = Chan()
        unrelated = Msg(444444444444444444, "not managed")
        chan.messages[444444444444444444] = unrelated
        await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    "444444444444444444",
                    "true",
                    "D",
                    "rule",
                    "",
                    "Rule",
                    "1",
                    "community",
                    "",
                    "",
                    "",
                ]
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 1
        assert summary.failed == 1
        assert not unrelated.deleted

    asyncio.run(run())


def test_rebuild_history_failure_reports_cleanup_without_rollback(monkeypatch):
    async def run():
        chan = Chan()
        writes = await fake_load(
            monkeypatch,
            sheet(
                ["", "", "true", "D", "rule", "", "Rule", "1", "community", "", "", ""]
            ),
            chan,
        )

        def broken_history(limit=500):
            async def gen():
                raise RuntimeError("history down")
                yield None

            return gen()

        chan.history = broken_history
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 1
        assert summary.failed == 1
        assert "cleanup" in summary.failures
        assert writes["batch"] == [[{"range": "B2", "values": [["100"]]}]]
        assert chan.sent[0].deleted is False

    asyncio.run(run())


def test_rebuild_old_cleanup_delete_failure_keeps_new_ids(monkeypatch):
    async def run():
        chan = Chan()
        old = Msg(555555555555555555, server_rules.marker_for("rule"))

        async def fail_delete():
            raise discord.Forbidden(response=None, message="no")

        old.delete = fail_delete
        chan.messages[555555555555555555] = old
        writes = await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    "555555555555555555",
                    "true",
                    "D",
                    "rule",
                    "",
                    "Rule",
                    "1",
                    "community",
                    "",
                    "",
                    "",
                ]
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 1
        assert summary.failed == 1
        assert writes["batch"] == [[{"range": "B2", "values": [["100"]]}]]
        assert chan.sent[0].deleted is False

    asyncio.run(run())


def test_orphan_cleanup_only_deletes_managed_messages(monkeypatch):
    async def run():
        chan = Chan()
        orphan = Msg(666666666666666666, server_rules.marker_for("orphan"))
        unrelated = Msg(777777777777777777, "not managed")
        chan.messages[666666666666666666] = orphan
        chan.messages[777777777777777777] = unrelated
        await fake_load(
            monkeypatch,
            sheet(
                ["", "", "true", "D", "rule", "", "Rule", "1", "community", "", "", ""]
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 1
        assert orphan.deleted
        assert not unrelated.deleted

    asyncio.run(run())


def test_orphan_cleanup_leaves_forged_marker_from_other_author(monkeypatch):
    async def run():
        chan = Chan()
        forged = Msg(
            888888888888888888, server_rules.marker_for("forged"), author_id=999
        )
        ordinary = Msg(999999999999999999, "ordinary")
        chan.messages[888888888888888888] = forged
        chan.messages[999999999999999999] = ordinary
        await fake_load(
            monkeypatch,
            sheet(
                ["", "", "true", "D", "rule", "", "Rule", "1", "community", "", "", ""]
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 1
        assert not forged.deleted
        assert not ordinary.deleted

    asyncio.run(run())
