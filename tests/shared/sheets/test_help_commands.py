from __future__ import annotations

import asyncio
import datetime as dt

from shared.sheets import help_commands
from shared.sheets.cache_service import cache


HEADERS = list(help_commands.REQUIRED_HEADERS)


def _row(**overrides: object) -> list[object]:
    values = {
        "enabled": "TRUE",
        "bot_key": "woadkeeper",
        "command_key": "clan",
        "command": "!clan",
        "usage": "!clan <tag>",
        "category": "Recruitment",
        "access_level": "user",
        "summary": "Clan details",
        "details": "Shows clan details.",
        "sort_order": "20",
    }
    values.update(overrides)
    return [values[name] for name in HEADERS]


def test_parser_resolves_reordered_headers_hides_disabled_and_sorts() -> None:
    reordered = list(reversed(HEADERS))
    source = [
        reordered,
        list(reversed(_row(command="!late", command_key="late", sort_order="30"))),
        list(
            reversed(
                _row(command="!off", command_key="off", enabled="FALSE", sort_order="1")
            )
        ),
        list(reversed(_row(command="!early", command_key="early", sort_order="2"))),
        list(
            reversed(
                _row(command="!fallback", command_key="fallback", sort_order="bad")
            )
        ),
    ]

    rows = help_commands.parse_rows(source)

    assert [row.command for row in rows] == ["!early", "!late", "!fallback"]


def test_lookup_normalizes_leading_bang_and_access_filtering() -> None:
    rows = help_commands.parse_rows(
        [
            HEADERS,
            _row(),
            _row(command="!secret", command_key="secret", access_level="admin"),
        ]
    )

    assert help_commands.find_row(rows, "clan") == help_commands.find_row(rows, "!clan")
    assert [
        row.command
        for row in help_commands.visible_rows(rows, staff=False, admin=False)
    ] == ["!clan"]
    assert [
        row.command for row in help_commands.visible_rows(rows, staff=True, admin=False)
    ] == ["!clan"]
    assert len(help_commands.visible_rows(rows, staff=True, admin=True)) == 2


def test_recruiter_only_visibility_uses_separate_access() -> None:
    rows = help_commands.parse_rows(
        [
            HEADERS,
            _row(command="!user", command_key="user"),
            _row(command="!recruit", command_key="recruit", access_level="recruiter"),
            _row(command="!staff", command_key="staff", access_level="staff"),
        ]
    )

    visible = help_commands.visible_rows(rows, staff=False, recruiter=True, admin=False)

    assert [row.command for row in visible] == ["!user", "!recruit"]


def test_grouping_splits_access_before_dynamic_category_and_numeric_order() -> None:
    rows = help_commands.parse_rows(
        [
            HEADERS,
            _row(
                command="!staff-late",
                command_key="staff_late",
                category="Zulu",
                access_level="staff",
                sort_order="20",
            ),
            _row(command="!user", command_key="user", category="Zulu", sort_order="99"),
            _row(
                command="!staff-early",
                command_key="staff_early",
                category="Zulu",
                access_level="staff",
                sort_order="2",
            ),
            _row(
                command="!staff-alpha",
                command_key="staff_alpha",
                category="Alpha",
                access_level="staff",
                sort_order="50",
            ),
        ]
    )

    grouped = help_commands.group_rows(rows)

    assert [(access, category) for access, category, _ in grouped] == [
        ("user", "Zulu"),
        ("staff", "Alpha"),
        ("staff", "Zulu"),
    ]
    assert [row.command for row in grouped[-1][2]] == ["!staff-early", "!staff-late"]


def test_loader_uses_config_selected_tab_and_one_values_read(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class Worksheet:
        def get_all_values(self):
            calls.append(("read", "once"))
            return [HEADERS, _row()]

    async def config(key, default):
        assert key == "HELP_COMMANDS_TAB"
        return "ConfiguredHelpTab"

    async def worksheet(sheet_id, tab):
        calls.append((sheet_id, tab))
        return Worksheet()

    async def direct_call(func, *args, **kwargs):
        return func(*args, **kwargs)

    sheet_id_calls = 0

    def sheet_id():
        nonlocal sheet_id_calls
        sheet_id_calls += 1
        return "sheet-id"

    monkeypatch.delenv("RECRUITMENT_SHEET_ID", raising=False)
    monkeypatch.setattr(help_commands.recruitment, "get_recruitment_sheet_id", sheet_id)
    monkeypatch.setattr(help_commands.recruitment, "get_config_value_async", config)
    monkeypatch.setattr(help_commands, "aget_worksheet", worksheet)
    monkeypatch.setattr(help_commands, "acall_with_backoff", direct_call)

    rows = asyncio.run(help_commands._load())

    assert rows[0].command == "!clan"
    assert sheet_id_calls == 1
    assert calls == [("sheet-id", "ConfiguredHelpTab"), ("read", "once")]


def test_cache_ttl_prevents_reads_and_failed_refresh_keeps_stale(monkeypatch) -> None:
    cache._buckets.pop(help_commands.BUCKET_NAME, None)
    reads = 0

    async def loader():
        nonlocal reads
        reads += 1
        if reads > 1:
            raise RuntimeError("temporary outage")
        return help_commands.parse_rows([HEADERS, _row()])

    monkeypatch.setattr(help_commands, "_load", loader)
    first = asyncio.run(help_commands.get_rows())
    second = asyncio.run(help_commands.get_rows())
    assert first == second
    assert reads == 1

    bucket = cache.get_bucket(help_commands.BUCKET_NAME)
    assert bucket is not None
    bucket.last_refresh = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=2)
    asyncio.run(cache.refresh_now(help_commands.BUCKET_NAME, actor="test"))
    assert bucket.value == first
    assert bucket.last_result == "fail"


def test_manual_helpcommands_bucket_refresh_reloads_immediately(monkeypatch) -> None:
    cache._buckets.pop(help_commands.BUCKET_NAME, None)
    reads = 0

    async def loader():
        nonlocal reads
        reads += 1
        return help_commands.parse_rows([HEADERS, _row(summary=f"version {reads}")])

    monkeypatch.setattr(help_commands, "_load", loader)
    first = asyncio.run(help_commands.get_rows())
    asyncio.run(cache.refresh_now("helpcommands", actor="admin"))
    bucket = cache.get_bucket("helpcommands")

    assert first is not None and first[0].summary == "version 1"
    assert bucket is not None and bucket.value[0].summary == "version 2"
    assert reads == 2
