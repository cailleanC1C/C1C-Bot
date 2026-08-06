import asyncio
import discord
from unittest.mock import AsyncMock
from urllib.parse import quote

from modules.ops import server_rules
from cogs.server_rules import ServerRulesCog


def legacy_marker(key):
    return f"{server_rules.LEGACY_MARKER_PREFIX}{key}\u2060\u2063"


def hidden_marker(key):
    return (
        f"[{server_rules.HIDDEN_MARKER_LABEL}]"
        f"({server_rules.HIDDEN_MARKER_PREFIX}{quote(key, safe='')})"
    )


class Msg:
    def __init__(self, id, content="", author_id=42):
        self.id = id
        self.content = content
        self.author = type("A", (), {"id": author_id})()
        self.deleted = False
        self.embeds = [discord.Embed(description="legacy payload")]
        self.channel = None
        self.edits = []

    async def edit(self, **kw):
        self.edits.append(kw)
        self.content = kw.get("content", self.content)
        if "embeds" in kw:
            self.embeds = kw["embeds"]
        if "embed" in kw:
            self.embeds = [kw["embed"]]

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
        msg.embeds = kw.get("embeds") or ([kw["embed"]] if "embed" in kw else [])
        msg.channel = self
        self.sent.append(msg)
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, mid):
        if mid not in self.messages:
            raise discord.NotFound(response=None, message="missing")
        self.messages[mid].channel = self
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
        assert [m.kw["embeds"][0].title for m in chan.sent] == ["Title A", "Title B"]
        assert chan.sent[0].kw["embeds"][0].thumbnail.url == "https://e.test/t.png"
        assert chan.sent[0].kw["content"] is None
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
        existing = Msg(555555555555555555, legacy_marker("keep"))
        chan.messages[555555555555555555] = existing
        disabled = Msg(777777777777777777, legacy_marker("old"))
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
        assert summary.created == 0
        assert summary.removed == 1
        assert existing.edits
        assert disabled.deleted
        assert ("B3", [["100"]]) not in writes["single"]
        assert ("B4", [[""]]) in writes["single"]

    asyncio.run(run())


def test_unrelated_messages_untouched_on_rebuild(monkeypatch):
    async def run():
        chan = Chan()
        unrelated = Msg(1, "hello")
        chan.messages[1] = unrelated
        old = Msg(222222222222222222, legacy_marker("rule"))
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
        assert summary.failed == 0
        assert summary.skipped == 1
        assert chan.sent == []

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


def test_supported_server_rules_faq_hex_colours_are_explicit_palette():
    from modules.common import embeds

    assert server_rules.parse_colour("#4472c4") == embeds.SERVER_RULES_FAQ_BLUE
    assert server_rules.parse_colour("#356854") == embeds.SERVER_RULES_FAQ_GREEN
    assert server_rules.parse_colour("#ffd666") == embeds.SERVER_RULES_FAQ_YELLOW
    assert server_rules.parse_colour("#607d8b") == embeds.SERVER_RULES_FAQ_SLATE
    assert server_rules.parse_colour("#4472C4") == embeds.SERVER_RULES_FAQ_BLUE
    assert server_rules.parse_colour("#00ff00") is None


def test_publish_fetches_known_old_id_and_never_deletes_forged_message(monkeypatch):
    async def run():
        chan = Chan()
        forged = Msg(333333333333333333, legacy_marker("rule"), author_id=99)
        old = Msg(222222222222222222, legacy_marker("rule"))
        chan.messages[forged.id] = forged
        chan.messages[old.id] = old
        fetched = []

        original_fetch = chan.fetch_message

        async def fetch_message(mid):
            fetched.append(mid)
            return await original_fetch(mid)

        chan.fetch_message = fetch_message
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
                    "#4472c4",
                    "",
                    "",
                    "",
                ]
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 1
        assert summary.removed == 1
        assert 222222222222222222 in fetched
        assert old.deleted
        assert not forged.deleted

    asyncio.run(run())


def test_publish_cleanup_failures_restore_ids_and_remove_replacement(monkeypatch):
    async def run():
        chan = Chan()
        old = Msg(222222222222222222, legacy_marker("rule"))
        chan.messages[old.id] = old

        async def delete_failure():
            raise discord.HTTPException(response=None, message="delete failed")

        old.delete = delete_failure
        writes = await fake_load(
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
                    "#356854",
                    "",
                    "",
                    "",
                ]
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 0
        assert summary.failed == 1
        assert writes["batch"] == [
            [{"range": "B2", "values": [["100"]]}],
            [{"range": "B2", "values": [[str(old.id)]]}],
        ]
        assert chan.sent[0].deleted

    asyncio.run(run())


def test_markerless_cleanup_failure_rolls_back_and_publish_retry_converges(
    monkeypatch,
):
    async def run():
        chan = Chan()
        old = Msg(222222222222222222, "")
        old.embeds = [discord.Embed(title="Previous", description="Clean")]
        chan.messages[old.id] = old
        rows = sheet(
            [
                "",
                str(old.id),
                "yes",
                "Replacement",
                "rule",
                "",
                "Current",
                "1",
                "blue",
                "",
                "",
                "rules",
            ]
        )
        first_writes = await fake_load(monkeypatch, rows, chan)
        original_delete = old.delete
        delete_attempts = 0

        async def fail_once():
            nonlocal delete_attempts
            delete_attempts += 1
            if delete_attempts == 1:
                raise discord.HTTPException(response=None, message="temporary")
            await original_delete()

        old.delete = fail_once
        first, _ = await server_rules.publish(Bot(chan))

        assert first.created == 0
        assert first.failed == 1
        assert not old.deleted
        assert chan.sent[0].deleted
        assert first_writes["batch"] == [
            [{"range": "B2", "values": [["100"]]}],
            [{"range": "B2", "values": [[str(old.id)]]}],
        ]

        second_writes = await fake_load(monkeypatch, rows, chan)
        second, _ = await server_rules.publish(Bot(chan))

        assert second.created == 1
        assert second.failed == 0
        assert old.deleted
        assert second_writes["batch"] == [[{"range": "B2", "values": [["101"]]}]]
        live_publication = [message for message in chan.sent if not message.deleted]
        assert len(live_publication) == 1
        assert live_publication[0].id == 101
        assert live_publication[0].content is None
        assert live_publication[0].embeds[0].description == "Replacement"

    asyncio.run(run())


def test_publish_history_scan_failure_reported_without_rollback(monkeypatch):
    async def run():
        chan = Chan()
        writes = await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    "",
                    "yes",
                    "D",
                    "rule",
                    "",
                    "T",
                    "1",
                    "#607d8b",
                    "",
                    "",
                    "",
                ]
            ),
            chan,
        )

        async def fail_iter(target, bot_id):
            raise discord.HTTPException(response=None, message="history failed")

        monkeypatch.setattr(server_rules, "_iter_feature_messages", fail_iter)
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 1
        assert summary.failed == 1
        assert writes["batch"] == [[{"range": "B2", "values": [["100"]]}]]
        assert not chan.sent[0].deleted

    asyncio.run(run())


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


def test_grouped_rows_publish_one_message_with_ordered_embeds_and_group_ids(
    monkeypatch,
):
    async def run():
        chan = Chan()
        previous = Msg(999999999999999999, legacy_marker("rule"))
        chan.messages[previous.id] = previous
        writes = await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    "",
                    "yes",
                    "One",
                    "rule",
                    "https://e.test/icon.png",
                    "T1",
                    "1",
                    "blue",
                    "",
                    "",
                    "rules",
                ],
                [
                    "",
                    "999999999999999999",
                    "yes",
                    "Two",
                    "rule",
                    "",
                    "T2",
                    "2",
                    "blue",
                    "",
                    "",
                    "rules",
                ],
                [
                    "",
                    "",
                    "yes",
                    "Three",
                    "rule",
                    "",
                    "T3",
                    "3",
                    "blue",
                    "Foot",
                    "",
                    "rules",
                ],
                ["", "", "yes", "FAQ", "faq_1", "", "Q", "4", "green", "", "", "faq"],
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 2
        assert len(chan.sent) == 2
        assert chan.sent[0].content is None
        assert [e.title for e in chan.sent[0].embeds] == ["T1", "T2", "T3"]
        assert chan.sent[0].embeds[0].thumbnail.url == "https://e.test/icon.png"
        assert not chan.sent[0].embeds[1].thumbnail.url
        assert not chan.sent[0].embeds[1].footer.text
        assert chan.sent[0].embeds[2].footer.text == "Foot"
        assert "serverrules:" not in (chan.sent[0].embeds[0].description or "")
        assert (
            server_rules._legacy_marker(chan.sent[0]).state
            is server_rules.LegacyMarkerState.NONE
        )
        assert writes["batch"] == [
            [
                {"range": "B2", "values": [["100"]]},
                {"range": "B3", "values": [[""]]},
                {"range": "B5", "values": [["101"]]},
            ]
        ]

    asyncio.run(run())


def test_group_limits_rejected_before_mutation(monkeypatch):
    async def run():
        chan = Chan()
        body = [
            ["", "", "yes", "D", "rule", "", f"T{i}", str(i), "blue", "", "", "rules"]
            for i in range(1, 12)
        ]
        writes = await fake_load(monkeypatch, sheet(*body), chan)
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.failed == 1
        assert chan.sent == []
        assert writes["batch"] == []
        body = [
            [
                "",
                "",
                "yes",
                "x" * 1000,
                "long",
                "",
                f"T{i}",
                str(i),
                "blue",
                "",
                "",
                "rules",
            ]
            for i in range(1, 8)
        ]
        writes = await fake_load(monkeypatch, sheet(*body), chan)
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.failed == 1
        assert chan.sent == []
        assert writes["batch"] == []

    asyncio.run(run())


def test_faq_topic_metadata_does_not_combine_or_render(monkeypatch):
    async def run():
        chan = Chan()
        rows = actual_sheet(
            [
                "faq_a",
                "faq",
                "1",
                "TRUE",
                "Q1",
                "A1",
                "blue",
                "https://e.test/topic.png",
                "",
                "",
                "",
                "",
                "topic",
                "Topic",
            ],
            [
                "faq_b",
                "faq",
                "2",
                "TRUE",
                "Q2",
                "A2",
                "blue",
                "",
                "",
                "",
                "",
                "",
                "topic",
                "Topic",
            ],
            [
                "faq_c",
                "faq",
                "3",
                "TRUE",
                "Q3",
                "A3",
                "blue",
                "",
                "Topic foot",
                "",
                "",
                "",
                "topic",
                "Topic",
            ],
        )
        rows[0].extend(["topic_key", "topic_title"])
        writes = await fake_load(monkeypatch, rows, chan)
        _tab, _header, parsed, errors = await server_rules.load_rows()
        assert not errors
        assert [r.topic_key for r in parsed] == ["topic", "topic", "topic"]
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 3
        assert len(chan.sent) == 3
        assert all(len(m.embeds) == 1 for m in chan.sent)
        assert "Topic" not in "".join(
            (m.content or "") + (m.embeds[0].description or "") for m in chan.sent
        )
        assert chan.sent[0].embeds[0].thumbnail.url == "https://e.test/topic.png"
        assert not chan.sent[1].embeds[0].thumbnail.url
        assert not chan.sent[1].embeds[0].footer.text
        assert chan.sent[2].embeds[0].footer.text == "Topic foot"
        assert writes["batch"] == [
            [
                {"range": "L2", "values": [["100"]]},
                {"range": "L3", "values": [["101"]]},
                {"range": "L4", "values": [["102"]]},
            ]
        ]

    asyncio.run(run())


def test_legacy_marker_validation_and_clean_refresh(monkeypatch):
    async def run():
        chan = Chan()
        legacy = Msg(555555555555555555, legacy_marker("keep"))
        chan.messages[legacy.id] = legacy
        await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    str(legacy.id),
                    "yes",
                    "Updated",
                    "keep",
                    "",
                    "Keep",
                    "1",
                    "blue",
                    "",
                    "",
                    "rules",
                ]
            ),
            chan,
        )
        summary, _ = await server_rules.refresh(Bot(chan))
        assert summary.refreshed == 1
        assert legacy.content is None
        assert (
            server_rules._legacy_marker(legacy).state
            is server_rules.LegacyMarkerState.NONE
        )
        assert legacy.embeds[0].description == "Updated"
        forged = Msg(1, author_id=99)
        forged.embeds = legacy.embeds
        assert (
            server_rules._legacy_marker(forged).state
            is server_rules.LegacyMarkerState.NONE
        )
        malformed = Msg(2, author_id=42)
        e = discord.Embed(description="[\u2063](https://c1c.invalid/serverrules/%ZZ)")
        malformed.embeds = [e]
        assert (
            server_rules._legacy_marker(malformed).state
            is server_rules.LegacyMarkerState.MALFORMED
        )
        ordinary = Msg(3, "hello", 42)
        assert (
            server_rules._legacy_marker(ordinary).state
            is server_rules.LegacyMarkerState.NONE
        )
        assert server_rules.is_feature_message(Msg(4, legacy_marker("old")), {"old"})

    asyncio.run(run())


def test_hidden_marker_migrates_then_markerless_stored_id_refreshes(monkeypatch):
    async def run():
        chan = Chan()
        message_id = 555555555555555555
        existing = Msg(message_id, "")
        existing.embeds = [discord.Embed(description=f"Old{hidden_marker('keep')}")]
        chan.messages[message_id] = existing
        rows = sheet(
            [
                "",
                str(message_id),
                "yes",
                "Clean Sheet description",
                "keep",
                "",
                "Keep",
                "1",
                "blue",
                "Sheet footer",
                "",
                "rules",
            ]
        )
        await fake_load(monkeypatch, rows, chan)

        first, _ = await server_rules.refresh(Bot(chan))
        second, _ = await server_rules.refresh(Bot(chan))

        assert first.refreshed == second.refreshed == 1
        assert len(existing.edits) == 2
        assert existing.content is None
        assert existing.embeds[0].description == "Clean Sheet description"
        assert existing.embeds[0].footer.text == "Sheet footer"
        assert "serverrules:" not in existing.embeds[0].description
        assert "c1c.invalid" not in existing.embeds[0].description

    asyncio.run(run())


def test_stored_message_verifier_rejects_untrusted_identity_and_payload():
    target = Chan(9)
    stored_id = "555555555555555555"

    def valid_message():
        message = Msg(int(stored_id), "")
        message.channel = target
        message.embeds = [discord.Embed(description="valid")]
        return message

    def accepted(message):
        return server_rules._is_valid_stored_message(
            message,
            stored_message_id=stored_id,
            target=target,
            bot_id=42,
            permitted_legacy_keys={"keep"},
        )

    assert accepted(valid_message())
    wrong_id = valid_message()
    wrong_id.id += 1
    assert not accepted(wrong_id)
    wrong_channel = valid_message()
    wrong_channel.channel = Chan(10)
    assert not accepted(wrong_channel)
    wrong_author = valid_message()
    wrong_author.author.id = 99
    assert not accepted(wrong_author)
    empty = valid_message()
    empty.embeds = []
    assert not accepted(empty)
    invalid = valid_message()
    invalid.embeds[0].description = "x" * (server_rules.MAX_DESCRIPTION + 1)
    assert not accepted(invalid)
    visible_content = valid_message()
    visible_content.content = "not management data"
    assert not accepted(visible_content)
    wrong_key = valid_message()
    wrong_key.content = legacy_marker("wrong")
    assert not accepted(wrong_key)
    malformed = valid_message()
    malformed.embeds[0].description = (
        f"[{server_rules.HIDDEN_MARKER_LABEL}]"
        f"({server_rules.HIDDEN_MARKER_PREFIX}%ZZ)"
    )
    assert not accepted(malformed)


def test_refresh_group_edits_once_and_conflicting_ids_fail(monkeypatch):
    async def run():
        chan = Chan()
        existing = Msg(555555555555555555, legacy_marker("rule"))
        chan.messages[existing.id] = existing
        await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    str(existing.id),
                    "yes",
                    "One",
                    "rule",
                    "",
                    "T1",
                    "1",
                    "blue",
                    "",
                    "",
                    "rules",
                ],
                [
                    "",
                    str(existing.id),
                    "yes",
                    "Two",
                    "rule",
                    "",
                    "T2",
                    "2",
                    "blue",
                    "",
                    "",
                    "rules",
                ],
            ),
            chan,
        )
        summary, _ = await server_rules.refresh(Bot(chan))
        assert summary.refreshed == 1
        assert len(existing.edits) == 1
        assert len(existing.edits[0]["embeds"]) == 2
        writes = await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    "555555555555555555",
                    "yes",
                    "One",
                    "bad",
                    "",
                    "T1",
                    "1",
                    "blue",
                    "",
                    "",
                    "rules",
                ],
                [
                    "",
                    "666666666666666666",
                    "yes",
                    "Two",
                    "bad",
                    "",
                    "T2",
                    "2",
                    "blue",
                    "",
                    "",
                    "rules",
                ],
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.failed == 1
        assert writes["batch"] == []

    asyncio.run(run())


def test_clean_description_overflow_rejected_before_side_effects(monkeypatch):
    async def run():
        chan = Chan()
        writes = await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    "",
                    "yes",
                    "x" * (server_rules.MAX_DESCRIPTION + 1),
                    "near",
                    "",
                    "",
                    "1",
                    "blue",
                    "",
                    "",
                    "rules",
                ]
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.failed == 1
        assert chan.sent == []
        assert writes["batch"] == []

    asyncio.run(run())


def test_clean_combined_total_overflow_rejected_before_side_effects(
    monkeypatch,
):
    async def run():
        chan = Chan()
        writes = await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    "",
                    "yes",
                    "a" * 3000,
                    "combo",
                    "",
                    "",
                    "1",
                    "blue",
                    "",
                    "",
                    "rules",
                ],
                [
                    "",
                    "",
                    "yes",
                    "b" * (server_rules.MAX_TOTAL - 3000 + 1),
                    "combo",
                    "",
                    "",
                    "2",
                    "blue",
                    "",
                    "",
                    "rules",
                ],
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.failed == 1
        assert chan.sent == []
        assert writes["batch"] == []

    asyncio.run(run())


def test_valid_4096_character_clean_payload_publishes(monkeypatch):
    async def run():
        chan = Chan()
        writes = await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    "",
                    "yes",
                    "x" * server_rules.MAX_DESCRIPTION,
                    "ok",
                    "",
                    "",
                    "1",
                    "blue",
                    "",
                    "",
                    "rules",
                ]
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 1
        assert len(chan.sent[0].embeds[0].description) == server_rules.MAX_DESCRIPTION
        assert writes["batch"] == [[{"range": "B2", "values": [["100"]]}]]

    asyncio.run(run())


def test_interleaved_topic_keys_rejected_before_side_effects(monkeypatch):
    async def run():
        chan = Chan()
        rows = actual_sheet(
            [
                "faq_a1",
                "faq",
                "1",
                "TRUE",
                "A1",
                "D",
                "blue",
                "",
                "",
                "",
                "",
                "",
                "topic_a",
                "A",
            ],
            [
                "faq_b",
                "faq",
                "2",
                "TRUE",
                "B",
                "D",
                "blue",
                "",
                "",
                "",
                "",
                "",
                "topic_b",
                "B",
            ],
            [
                "faq_a2",
                "faq",
                "3",
                "TRUE",
                "A2",
                "D",
                "blue",
                "",
                "",
                "",
                "",
                "",
                "topic_a",
                "A",
            ],
        )
        rows[0].extend(["topic_key", "topic_title"])
        writes = await fake_load(monkeypatch, rows, chan)
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.failed == 1
        assert chan.sent == []
        assert writes["batch"] == []

    asyncio.run(run())


def test_blank_rule_topic_metadata_remains_valid(monkeypatch):
    async def run():
        chan = Chan()
        rows = actual_sheet(
            [
                "rule",
                "rules",
                "1",
                "TRUE",
                "Rule",
                "D",
                "blue",
                "",
                "",
                "",
                "",
                "",
                "",
                "",
            ],
            [
                "faq",
                "faq",
                "2",
                "TRUE",
                "FAQ",
                "D",
                "blue",
                "",
                "",
                "",
                "",
                "",
                "topic",
                "Topic",
            ],
        )
        rows[0].extend(["topic_key", "topic_title"])
        summary_target = await fake_load(monkeypatch, rows, chan)
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 2
        assert summary_target["batch"] == [
            [
                {"range": "L2", "values": [["100"]]},
                {"range": "L3", "values": [["101"]]},
            ]
        ]

    asyncio.run(run())


def test_cleanup_accepts_legacy_faq_topic_marker_for_stored_question_row(monkeypatch):
    async def run():
        chan = Chan()
        old = Msg(222222222222222222, legacy_marker("topic_legacy"))
        chan.messages[old.id] = old
        rows = actual_sheet(
            [
                "faq_question",
                "faq",
                "1",
                "TRUE",
                "Q",
                "D",
                "blue",
                "",
                "",
                "",
                "",
                str(old.id),
                "topic_legacy",
                "Topic",
            ],
        )
        rows[0].extend(["topic_key", "topic_title"])
        await fake_load(monkeypatch, rows, chan)
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 1
        assert summary.removed == 1
        assert old.deleted
        assert not chan.sent[0].deleted

    asyncio.run(run())


def test_failed_direct_cleanup_verification_does_not_hide_orphan(monkeypatch):
    async def run():
        chan = Chan()
        orphan = Msg(222222222222222222, legacy_marker("old_cluster"))
        wrong_author = Msg(
            333333333333333333, legacy_marker("old_cluster"), author_id=99
        )
        malformed = Msg(444444444444444444, "")
        malformed.embeds = [
            discord.Embed(description="[\u2063](https://c1c.invalid/serverrules/%ZZ)")
        ]
        unrelated = Msg(555555555555555555, "hello")
        chan.messages[orphan.id] = orphan
        chan.messages[wrong_author.id] = wrong_author
        chan.messages[malformed.id] = malformed
        chan.messages[unrelated.id] = unrelated
        await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    str(orphan.id),
                    "yes",
                    "New",
                    "new_question",
                    "",
                    "Q",
                    "1",
                    "blue",
                    "",
                    "",
                    "faq",
                ]
            ),
            chan,
        )
        summary, _ = await server_rules.publish(Bot(chan))
        assert summary.created == 1
        assert summary.removed == 1
        assert orphan.deleted
        assert not chan.sent[0].deleted
        assert not wrong_author.deleted
        assert not malformed.deleted
        assert not unrelated.deleted

    asyncio.run(run())


def test_refresh_failed_group_does_not_stop_other_group(monkeypatch):
    async def run():
        chan = Chan()
        bad = Msg(111111111111111111, legacy_marker("bad"))
        good = Msg(222222222222222222, legacy_marker("good"))
        chan.messages[bad.id] = bad
        chan.messages[good.id] = good

        async def bad_edit(**kw):
            raise discord.HTTPException(response=None, message="boom")

        bad.edit = bad_edit
        await fake_load(
            monkeypatch,
            sheet(
                [
                    "",
                    str(bad.id),
                    "yes",
                    "Bad",
                    "bad",
                    "",
                    "Bad",
                    "1",
                    "blue",
                    "",
                    "",
                    "rules",
                ],
                [
                    "",
                    str(good.id),
                    "yes",
                    "Good",
                    "good",
                    "",
                    "Good",
                    "2",
                    "blue",
                    "",
                    "",
                    "rules",
                ],
            ),
            chan,
        )
        summary, _ = await server_rules.refresh(Bot(chan))
        assert summary.failed == 1
        assert summary.refreshed == 1
        assert good.content is None
        assert (
            server_rules._legacy_marker(good).state
            is server_rules.LegacyMarkerState.NONE
        )

    asyncio.run(run())
