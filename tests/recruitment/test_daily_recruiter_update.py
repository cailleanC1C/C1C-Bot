import asyncio
import pytest
from discord import Embed
from unittest.mock import AsyncMock, MagicMock

from modules.recruitment.reporting import daily_recruiter_update as dru
from cogs.recruitment_reporting import RecruitmentReporting


def _sample_rows():
    return [
        [
            "H1_Headline",
            "H2_Headline",
            "Key",
            "open_spots",
            "inactives",
            "reserved_spots",
        ],
        ["General Overview", "", "", "", "", ""],
        ["", "", "Ops Summary", "3", "1", "0"],
        ["", "", "Ops Idle", "0", "0", "0"],
        ["Per Bracket", "", "", "", "", ""],
        ["", "", "Elite End Game", "2", "0", "1"],
        ["", "", "Mid Game", "0", "0", "0"],
        ["Bracket Details", "", "", "", "", ""],
        ["", "Elite End Game", "", "", "", ""],
        ["", "", "Clan Alpha", "5", "0", "1"],
        ["", "", "Elders", "0", "0", "0"],
        ["", "", "Cambions", "0", "0", "0"],
        ["", "", "", "", "", ""],
        ["", "Mid Game", "", "", "", ""],
        ["", "", "Clan Delta", "2", "2", "0"],
    ]


def _field_text(embeds):
    return "\n".join(field.value for embed in embeds for field in embed.fields)


def test_build_embeds_from_rows_filters_and_groups():
    rows = _sample_rows()
    headers = dru._headers_map(rows[0])
    summary_embed, details_embed = dru._build_embeds_from_rows(rows, headers)

    assert isinstance(summary_embed, Embed)
    assert isinstance(details_embed, Embed)

    assert summary_embed.title == "Summary Open Spots"
    assert details_embed.title == "Bracket Details"

    assert len(summary_embed.fields) == 3

    general_field = summary_embed.fields[0]
    assert general_field.name == "General Overview"
    assert "🔹 **Ops Summary:** open 3" in general_field.value
    assert "Ops Idle" not in general_field.value

    divider_field = summary_embed.fields[1]
    assert divider_field.name.strip() == ""
    assert divider_field.value in {"﹘﹘﹘", "▫▪▫▪▫▪▫"}

    per_bracket = summary_embed.fields[2]
    assert per_bracket.name == "Per Bracket"
    assert "🔹 **Elite End Game:** open 2 | inactives 0 | reserved 1" in per_bracket.value
    assert "🔹 **Mid Game:** open 0 | inactives 0 | reserved 0" in per_bracket.value

    assert len(details_embed.fields) == 2

    elite_end_game = details_embed.fields[0]
    assert elite_end_game.name == "Elite End Game"
    assert elite_end_game.inline is False
    assert "🔹 **Clan Alpha:** open 5 | inactives 0 | reserved 1" in elite_end_game.value
    assert "🔹 **Elders:** open 0 | inactives 0 | reserved 0" not in elite_end_game.value
    assert "🔹 **Cambions:** open 0 | inactives 0 | reserved 0" not in elite_end_game.value
    assert details_embed.footer.text == dru.DETAILS_FILTER_FOOTER
    assert summary_embed.footer.text != dru.DETAILS_FILTER_FOOTER

    mid_game = details_embed.fields[1]
    assert mid_game.name == "Mid Game"
    assert mid_game.inline is False
    assert "🔹 **Clan Delta:** open 2 | inactives 2 | reserved 0" in mid_game.value


def test_bracket_details_hide_zero_rows_and_empty_brackets():
    rows = [
        [
            "H1_Headline",
            "H2_Headline",
            "Key",
            "open_spots",
            "inactives",
            "reserved_spots",
        ],
        ["Bracket Details", "", "", "", "", ""],
        ["", "First Sheet Bracket", "", "", "", ""],
        ["", "", "Island Arena", "0", "0", "0"],
        ["", "", "Active Clan", "4", "1", "2"],
        ["", "Second Sheet Bracket", "", "", "", ""],
        ["", "", "Vindicators", "0", "0", "0"],
        ["", "", "Warlords", "0", "0", "0"],
        ["", "", "Eff-It", "0", "0", "0"],
    ]
    headers = dru._headers_map(rows[0])

    sections = dru._extract_report_sections(rows, headers)
    details_embed = dru._build_details_embed(sections)

    assert [field.name for field in details_embed.fields] == ["First Sheet Bracket"]
    first_value = details_embed.fields[0].value
    assert "🔹 **Island Arena:** open 0 | inactives 0 | reserved 0" not in first_value
    assert "🔹 **Active Clan:** open 4 | inactives 1 | reserved 2" in first_value
    assert "Second Sheet Bracket" not in [field.name for field in details_embed.fields]


def test_live_headers_drive_sections_brackets_and_clan_names_without_grouping():
    rows = [
        [
            "reserved_spots",
            "H2_Headline",
            "inactives",
            "Key",
            "H1_Headline",
            "open_spots",
        ],
        ["", "", "", "", "Bracket Details", ""],
        ["", "Configured H2 Bracket", "", "", "", ""],
        ["0", "", "0", "Key Clan Zero", "", "0"],
        ["3", "", "1", "Key Clan Active", "", "2"],
    ]
    headers = dru._headers_map(rows[0])

    assert "grouping" not in headers
    sections = dru._extract_report_sections(rows, headers)
    details_embed = dru._build_details_embed(sections)

    assert len(details_embed.fields) == 1
    assert details_embed.fields[0].name == "Configured H2 Bracket"
    value = details_embed.fields[0].value
    assert "🔹 **Key Clan Zero:** open 0 | inactives 0 | reserved 0" not in value
    assert "🔹 **Key Clan Active:** open 2 | inactives 1 | reserved 3" in value


def test_statistics_header_normalization_accepts_existing_sheet_headers():
    rows = _sample_rows()
    rows[0] = [
        "  H1_Headline  ",
        "h2   headline",
        "KEY",
        "open_spots",
        " inactives ",
        "reserved   spots",
    ]
    headers = dru._headers_map(rows[0])

    sections = dru._extract_report_sections(
        rows,
        headers,
        dru._report_fetch_context(tab_name=dru.DEFAULT_REPORTS_TAB_NAME, rows=rows, data_source="test"),
    )

    assert sections.general_lines
    assert "Ops Summary" in sections.general_lines[0]


def test_missing_headers_include_diagnostics():
    rows = [["not", "the", "schema"], ["General Overview"]]
    headers = dru._headers_map(rows[0])
    context = dru._report_fetch_context(tab_name=dru.DEFAULT_REPORTS_TAB_NAME, rows=rows, data_source="test")

    with pytest.raises(dru.ReportSchemaError) as exc_info:
        dru._extract_report_sections(rows, headers, context)

    exc = exc_info.value
    assert exc.required == dru._REPORT_REQUIRED_HEADERS
    assert exc.actual_first_row == tuple(rows[0])
    assert exc.context.config_key == "REPORTS_TAB"
    assert exc.context.tab_name == dru.DEFAULT_REPORTS_TAB_NAME
    assert exc.context.row_count == 2


def test_fetch_timeout_uses_cached_valid_rows(monkeypatch):
    rows = _sample_rows()
    headers = dru._headers_map(rows[0])

    async def timeout_fetch(tab_name):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(dru, "_REPORT_ROWS_CACHE", rows)
    monkeypatch.setattr(dru, "_REPORT_HEADERS_CACHE", headers)
    monkeypatch.setattr(dru, "_REPORT_CONTEXT_CACHE", None)
    monkeypatch.setattr(dru, "get_recruitment_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(dru, "get_reports_tab_name_async", AsyncMock(return_value=dru.DEFAULT_REPORTS_TAB_NAME))
    monkeypatch.setattr(dru, "afetch_reports_tab", timeout_fetch)

    fetched_rows, fetched_headers = asyncio.run(dru._fetch_report_rows())

    assert fetched_rows == rows
    assert fetched_headers == headers
    assert dru._REPORT_CONTEXT_CACHE is not None
    assert dru._REPORT_CONTEXT_CACHE.data_source == "cache"
    assert dru._REPORT_CONTEXT_CACHE.underlying_exception_type == "TimeoutError"


def test_fetch_timeout_without_cache_returns_actionable_error(monkeypatch):
    async def timeout_fetch(tab_name):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(dru, "_REPORT_ROWS_CACHE", None)
    monkeypatch.setattr(dru, "_REPORT_HEADERS_CACHE", {})
    monkeypatch.setattr(dru, "_REPORT_CONTEXT_CACHE", None)
    monkeypatch.setattr(dru, "get_recruitment_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(dru, "get_reports_tab_name_async", AsyncMock(return_value=dru.DEFAULT_REPORTS_TAB_NAME))
    monkeypatch.setattr(dru, "afetch_reports_tab", timeout_fetch)

    with pytest.raises(dru.ReportFetchError) as exc_info:
        asyncio.run(dru._fetch_report_rows())

    assert "Google Sheets/cache fetch timed out" in str(exc_info.value)
    assert exc_info.value.context.underlying_exception_type == "TimeoutError"


def test_fetch_report_rows_uses_async_reports_tab_lookup_in_async_context(monkeypatch):
    rows = _sample_rows()

    async def fake_fetch(tab_name):
        assert tab_name == "Configured Stats"
        return rows

    def forbidden_sync_reports_tab(*_args, **_kwargs):
        raise AssertionError("sync report tab lookup must not be used from async report path")

    monkeypatch.setattr(dru, "_REPORT_ROWS_CACHE", None)
    monkeypatch.setattr(dru, "_REPORT_HEADERS_CACHE", {})
    monkeypatch.setattr(dru, "_REPORT_CONTEXT_CACHE", None)
    monkeypatch.setattr(dru, "get_recruitment_sheet_id", lambda: "sheet-id")
    monkeypatch.setattr(dru, "get_reports_tab_name", forbidden_sync_reports_tab, raising=False)
    monkeypatch.setattr(dru, "get_reports_tab_name_async", AsyncMock(return_value="Configured Stats"))
    monkeypatch.setattr(dru, "afetch_reports_tab", fake_fetch)

    fetched_rows, fetched_headers = asyncio.run(dru._fetch_report_rows())

    assert fetched_rows == rows
    assert fetched_headers == dru._headers_map(rows[0])
    dru.get_reports_tab_name_async.assert_awaited_once_with(dru.DEFAULT_REPORTS_TAB_NAME)
    assert dru._REPORT_CONTEXT_CACHE is not None
    assert dru._REPORT_CONTEXT_CACHE.tab_name == "Configured Stats"

def test_bracket_details_keep_each_mixed_non_zero_case():
    rows = [
        [
            "H1_Headline",
            "H2_Headline",
            "Key",
            "open_spots",
            "inactives",
            "reserved_spots",
        ],
        ["Bracket Details", "", "", "", "", ""],
        ["", "Mixed Bracket", "", "", "", ""],
        ["", "", "Only Inactives", "0", "3", "0"],
        ["", "", "Only Reserved", "0", "0", "2"],
        ["", "", "Only Open", "1", "0", "0"],
        ["", "", "All Zero", "0", "0", "0"],
    ]
    headers = dru._headers_map(rows[0])

    sections = dru._extract_report_sections(rows, headers)
    details_embed = dru._build_details_embed(sections)

    assert len(details_embed.fields) == 1
    value = details_embed.fields[0].value
    assert "🔹 **Only Inactives:** open 0 | inactives 3 | reserved 0" in value
    assert "🔹 **Only Reserved:** open 0 | inactives 0 | reserved 2" in value
    assert "🔹 **Only Open:** open 1 | inactives 0 | reserved 0" in value
    assert "🔹 **All Zero:** open 0 | inactives 0 | reserved 0" not in value


def test_open_spots_pager_switches_pages(monkeypatch):
    rows = _sample_rows()
    headers = dru._headers_map(rows[0])
    sections = dru._extract_report_sections(rows, headers)

    async def fake_load_sections():
        return sections

    monkeypatch.setattr(dru, "_load_report_sections", fake_load_sections)

    async def runner():
        pager = dru.OpenSpotsPager(sections)

        interaction_details = MagicMock()
        interaction_details.response = AsyncMock()
        interaction_details.response.edit_message = AsyncMock()
        interaction_details.response.defer = AsyncMock()

        await pager.set_details(interaction_details)

        interaction_summary = MagicMock()
        interaction_summary.response = AsyncMock()
        interaction_summary.response.edit_message = AsyncMock()
        interaction_summary.response.defer = AsyncMock()

        await pager.set_summary(interaction_summary)

        return pager, interaction_details, interaction_summary

    pager, interaction_details, interaction_summary = asyncio.run(runner())

    assert pager.current_page == "summary"
    assert pager.summary_button.disabled is True
    assert pager.details_button.disabled is False

    args, kwargs = interaction_details.response.edit_message.await_args
    assert kwargs["embeds"][0].title == "Bracket Details"

    args, kwargs = interaction_summary.response.edit_message.await_args
    assert kwargs["embeds"][0].title == "Summary Open Spots"


def test_open_spots_pager_is_persistent_and_registered_once(monkeypatch):
    add_view_calls = []

    class DummyBot:
        def add_view(self, view):
            add_view_calls.append(view)

    monkeypatch.setattr(dru, "_PERSISTENT_VIEW_REGISTERED", False)
    bot = DummyBot()
    dru.register_persistent_views(bot)
    dru.register_persistent_views(bot)

    assert len(add_view_calls) == 1
    view = add_view_calls[0]
    assert isinstance(view, dru.OpenSpotsPager)
    assert view.timeout is None
    custom_ids = {item.custom_id for item in view.children}
    assert custom_ids == {"open_spots_summary", "open_spots_details"}


def test_open_spots_pager_registration_skips_bot_marked_registered(monkeypatch):
    add_view_calls = []

    class DummyBot:
        _c1c_open_spots_pager_registered = True

        def add_view(self, view):
            add_view_calls.append(view)

    monkeypatch.setattr(dru, "_PERSISTENT_VIEW_REGISTERED", False)
    dru.register_persistent_views(DummyBot())

    assert add_view_calls == []


def test_open_spots_pager_registration_tolerates_duplicate_value_error(monkeypatch):
    class DummyBot:
        def __init__(self):
            self.marked = False

        def add_view(self, view):
            raise ValueError("duplicate custom_id open_spots_summary already registered")

    monkeypatch.setattr(dru, "_PERSISTENT_VIEW_REGISTERED", False)
    bot = DummyBot()
    dru.register_persistent_views(bot)

    assert getattr(bot, "_c1c_open_spots_pager_registered") is True


def test_post_daily_recruiter_update_sends_pager(monkeypatch):
    rows = _sample_rows()
    headers = dru._headers_map(rows[0])

    async def fake_fetch():
        return rows, headers

    class DummyChannel:
        def __init__(self):
            self.sent = []
            self.guild = None

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    channel = DummyChannel()

    bot = MagicMock()
    bot.get_channel.return_value = channel
    bot.fetch_channel = AsyncMock()
    bot.wait_until_ready = AsyncMock()

    monkeypatch.setattr(dru, "_fetch_report_rows", fake_fetch)
    monkeypatch.setattr(dru, "get_report_destination_id", lambda: 123)
    monkeypatch.setattr(dru, "_role_mentions", lambda: ("<@&1>", "<@&2>"))
    monkeypatch.setattr(dru.discord, "TextChannel", DummyChannel)

    ok, error = asyncio.run(dru.post_daily_recruiter_update(bot))

    assert ok is True
    assert error == "-"
    assert channel.sent
    sent_kwargs = channel.sent[0]
    assert len(sent_kwargs["embeds"]) == 1
    assert isinstance(sent_kwargs["view"], dru.OpenSpotsPager)
    assert sent_kwargs["content"].startswith("# Update ")
    assert "<@&1>" in sent_kwargs["content"]
    assert "<@&2>" in sent_kwargs["content"]
    assert sent_kwargs["embeds"][0].title == "Summary Open Spots"


def test_post_daily_recruiter_update_logs_missing_headers(monkeypatch, caplog):
    rows = [["bad", "schema"], ["General Overview", ""]]
    headers = dru._headers_map(rows[0])

    async def fake_fetch():
        dru._REPORT_CONTEXT_CACHE = dru._report_fetch_context(
            tab_name=dru.DEFAULT_REPORTS_TAB_NAME, rows=rows, data_source="test"
        )
        return rows, headers

    class DummyChannel:
        guild = None

        async def send(self, **kwargs):
            raise AssertionError("send should not be called")

    bot = MagicMock()
    bot.get_channel.return_value = DummyChannel()
    bot.fetch_channel = AsyncMock()
    bot.wait_until_ready = AsyncMock()

    monkeypatch.setattr(dru, "_fetch_report_rows", fake_fetch)
    monkeypatch.setattr(dru, "get_report_destination_id", lambda: 123)
    monkeypatch.setattr(dru.discord, "TextChannel", DummyChannel)

    with caplog.at_level("WARNING", logger="c1c.recruitment.reporting.daily"):
        ok, error = asyncio.run(dru.post_daily_recruiter_update(bot))

    assert ok is False
    assert "missing required header" in error
    assert "summary/bracket section" in caplog.text
    assert "required=" in caplog.text
    assert "config_key=REPORTS_TAB" in caplog.text


def test_run_full_recruiter_reports_posts_open_tickets_after_summary_failure(monkeypatch):
    async def fake_report(bot):
        return False, "summary failed"

    async def fake_audit(bot, *, actor, dry_run):
        return True, "-"

    async def fake_tickets(bot):
        return True, "-"

    log_calls = []

    async def fake_log_event(**kwargs):
        log_calls.append(kwargs)

    monkeypatch.setattr(dru, "post_daily_recruiter_update", fake_report)
    monkeypatch.setattr(dru, "run_role_and_visitor_audit", fake_audit)
    monkeypatch.setattr(dru, "send_currently_open_tickets_report", fake_tickets)
    monkeypatch.setattr(dru, "resolve_audit_destination", lambda: (123, "REPORT_RECRUITERS_DEST_ID"))
    monkeypatch.setattr(dru, "_log_event", fake_log_event)

    results = asyncio.run(dru.run_full_recruiter_reports(MagicMock(), actor="scheduled"))

    assert results["report"] == (False, "summary failed")
    assert results["open_tickets"] == (True, "-")
    assert any(call.get("note") == "open-tickets" and call["result"] == "ok" for call in log_calls)
    assert any(call.get("note") is None and call["result"] == "fail" for call in log_calls)


def test_run_full_recruiter_reports_posts_normal_report_and_open_tickets(monkeypatch):
    async def fake_report(bot):
        return True, "-"

    async def fake_audit(bot, *, actor, dry_run):
        return True, "-"

    async def fake_tickets(bot):
        return True, "-"

    log_calls = []

    async def fake_log_event(**kwargs):
        log_calls.append(kwargs)

    monkeypatch.setattr(dru, "post_daily_recruiter_update", fake_report)
    monkeypatch.setattr(dru, "run_role_and_visitor_audit", fake_audit)
    monkeypatch.setattr(dru, "send_currently_open_tickets_report", fake_tickets)
    monkeypatch.setattr(dru, "resolve_audit_destination", lambda: (123, "REPORT_RECRUITERS_DEST_ID"))
    monkeypatch.setattr(dru, "_log_event", fake_log_event)

    results = asyncio.run(dru.run_full_recruiter_reports(MagicMock(), actor="scheduled"))

    assert results["report"] == (True, "-")
    assert results["open_tickets"] == (True, "-")
    assert any(call.get("note") is None and call["result"] == "ok" for call in log_calls)
    assert any(call.get("note") == "open-tickets" and call["result"] == "ok" for call in log_calls)


def test_summary_embed_splits_long_field_values_without_dropping_lines():
    rows = _sample_rows()
    rows[2:2] = [["", "", f"Long Clan {idx}", "1", "0", "0"] for idx in range(40)]
    headers = dru._headers_map(rows[0])

    sections = dru._extract_report_sections(rows, headers)
    summary_embeds = dru._build_summary_embeds(sections)
    details_embeds = dru._build_details_embeds(sections)

    dru._validate_summary_section(sections, summary_embeds, details_embeds)
    combined = _field_text(summary_embeds)
    for idx in range(40):
        assert f"Long Clan {idx}" in combined
    assert all(
        len(field.value) <= dru.DISCORD_FIELD_VALUE_LIMIT
        for embed in summary_embeds
        for field in embed.fields
    )


def test_details_embed_continues_into_additional_embeds_without_dropping_fields():
    rows = [
        [
            "H1_Headline",
            "H2_Headline",
            "Key",
            "open_spots",
            "inactives",
            "reserved_spots",
        ],
        ["General Overview", "", "", "", "", ""],
        ["", "", "Ops Summary", "3", "1", "0"],
        ["Bracket Details", "", "", "", "", ""],
    ]
    for idx in range(dru.DISCORD_EMBED_FIELD_LIMIT + 5):
        rows.extend(
            [
                ["", f"Bracket {idx}", "", "", "", ""],
                ["", "", f"Clan {idx}", "1", "0", "0"],
            ]
        )
    headers = dru._headers_map(rows[0])

    sections = dru._extract_report_sections(rows, headers)
    details_embeds = dru._build_details_embeds(sections)

    assert len(details_embeds) > 1
    combined = _field_text(details_embeds)
    for idx in range(dru.DISCORD_EMBED_FIELD_LIMIT + 5):
        assert f"Clan {idx}" in combined
    assert all(len(embed.fields) <= dru.DISCORD_EMBED_FIELD_LIMIT for embed in details_embeds)


def test_single_extremely_long_line_is_split_without_ellipsis_or_data_loss():
    long_label = "Clan " + ("A" * (dru.DISCORD_FIELD_VALUE_LIMIT * 2 + 100))
    sections = dru.ReportSections(
        general_lines=[f"🔹 **{long_label}:** open 1 | inactives 0 | reserved 0"],
        per_bracket_lines=[],
        detail_blocks=[],
    )

    summary_embeds = dru._build_summary_embeds(sections)
    combined = _field_text(summary_embeds)

    assert "…" not in combined
    assert long_label in combined.replace("\n", "")
    assert all(
        len(field.value) <= dru.DISCORD_FIELD_VALUE_LIMIT
        for embed in summary_embeds
        for field in embed.fields
    )


def test_report_exceeding_embed_total_length_splits_safely():
    lines = [
        f"🔹 **Clan {idx:03d}:** open 1 | inactives 0 | reserved 0 " + ("x" * 180)
        for idx in range(45)
    ]
    sections = dru.ReportSections(
        general_lines=lines,
        per_bracket_lines=[],
        detail_blocks=[],
    )

    summary_embeds = dru._build_summary_embeds(sections)

    assert len(summary_embeds) > 1
    combined = _field_text(summary_embeds)
    for idx in range(45):
        assert f"Clan {idx:03d}" in combined
    assert all(len(embed) <= dru.DISCORD_EMBED_TOTAL_LIMIT for embed in summary_embeds)


def test_summary_exceeding_message_embed_budget_sends_multiple_safe_messages(monkeypatch):
    rows = _sample_rows()
    rows[2:2] = [
        ["", "", f"Long Summary Clan {idx} " + ("x" * 900), "1", "0", "0"]
        for idx in range(10)
    ]
    headers = dru._headers_map(rows[0])

    async def fake_fetch():
        return rows, headers

    class DummyChannel:
        def __init__(self):
            self.sent = []
            self.guild = None

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    channel = DummyChannel()
    bot = MagicMock()
    bot.get_channel.return_value = channel
    bot.fetch_channel = AsyncMock()
    bot.wait_until_ready = AsyncMock()

    monkeypatch.setattr(dru, "_fetch_report_rows", fake_fetch)
    monkeypatch.setattr(dru, "get_report_destination_id", lambda: 123)
    monkeypatch.setattr(dru, "_role_mentions", lambda: ("<@&1>",))
    monkeypatch.setattr(dru.discord, "TextChannel", DummyChannel)

    ok, error = asyncio.run(dru.post_daily_recruiter_update(bot))

    assert ok is True
    assert error == "-"
    assert len(channel.sent) > 1
    assert isinstance(channel.sent[0]["view"], dru.OpenSpotsPager)
    assert channel.sent[0]["content"].startswith("# Update ")
    assert "<@&1>" in channel.sent[0]["content"]
    for sent in channel.sent:
        assert sum(len(embed) for embed in sent["embeds"]) <= dru.DISCORD_MESSAGE_EMBED_TOTAL_LIMIT


def test_summary_more_than_ten_generated_embeds_posts_multiple_safe_messages(monkeypatch):
    rows = _sample_rows()
    generated_count = dru.DISCORD_EMBEDS_PER_MESSAGE_LIMIT * 8
    rows[2:2] = [
        ["", "", f"Huge Summary Clan {idx:03d} " + ("x" * 900), "1", "0", "0"]
        for idx in range(generated_count)
    ]
    headers = dru._headers_map(rows[0])

    async def fake_fetch():
        return rows, headers

    class DummyChannel:
        def __init__(self):
            self.sent = []
            self.guild = None

        async def send(self, **kwargs):
            self.sent.append(kwargs)

    channel = DummyChannel()
    bot = MagicMock()
    bot.get_channel.return_value = channel
    bot.fetch_channel = AsyncMock()
    bot.wait_until_ready = AsyncMock()

    monkeypatch.setattr(dru, "_fetch_report_rows", fake_fetch)
    monkeypatch.setattr(dru, "get_report_destination_id", lambda: 123)
    monkeypatch.setattr(dru, "_role_mentions", lambda: ())
    monkeypatch.setattr(dru.discord, "TextChannel", DummyChannel)

    ok, error = asyncio.run(dru.post_daily_recruiter_update(bot))

    assert ok is True
    assert error == "-"
    total_embeds = sum(len(sent["embeds"]) for sent in channel.sent)
    assert total_embeds > dru.DISCORD_EMBEDS_PER_MESSAGE_LIMIT
    combined = "\n".join(
        field.value
        for sent in channel.sent
        for embed in sent["embeds"]
        for field in embed.fields
    )
    for idx in range(generated_count):
        assert f"Huge Summary Clan {idx:03d}" in combined
    for sent in channel.sent:
        assert len(sent["embeds"]) <= dru.DISCORD_EMBEDS_PER_MESSAGE_LIMIT
        assert sum(len(embed) for embed in sent["embeds"]) <= dru.DISCORD_MESSAGE_EMBED_TOTAL_LIMIT


def test_report_too_large_for_details_button_fails_loudly(monkeypatch, caplog):
    rows = [
        [
            "H1_Headline",
            "H2_Headline",
            "Key",
            "open_spots",
            "inactives",
            "reserved_spots",
        ],
        ["General Overview", "", "", "", "", ""],
        ["", "", "Ops Summary", "3", "1", "0"],
        ["Bracket Details", "", "", "", "", ""],
    ]
    for idx in range(10):
        rows.extend(
            [
                ["", f"Bracket {idx}", "", "", "", ""],
                ["", "", f"Clan {idx} " + ("x" * 900), "1", "0", "0"],
            ]
        )
    headers = dru._headers_map(rows[0])

    async def fake_fetch():
        dru._REPORT_CONTEXT_CACHE = dru._report_fetch_context(
            tab_name=dru.DEFAULT_REPORTS_TAB_NAME, rows=rows, data_source="test"
        )
        return rows, headers

    class DummyChannel:
        guild = None

        async def send(self, **kwargs):
            raise AssertionError("partial report should not be sent")

    bot = MagicMock()
    bot.get_channel.return_value = DummyChannel()
    bot.fetch_channel = AsyncMock()
    bot.wait_until_ready = AsyncMock()

    monkeypatch.setattr(dru, "_fetch_report_rows", fake_fetch)
    monkeypatch.setattr(dru, "get_report_destination_id", lambda: 123)
    monkeypatch.setattr(dru.discord, "TextChannel", DummyChannel)

    with caplog.at_level("WARNING", logger="c1c.recruitment.reporting.daily"):
        ok, error = asyncio.run(dru.post_daily_recruiter_update(bot))

    assert ok is False
    assert "Bracket Details button would require multiple Discord edit payloads" in error
    assert "phase=pagination" in caplog.text
    assert "row_count=" in caplog.text


def test_bracket_details_button_cannot_generate_oversized_edit_payload():
    sections = dru.ReportSections(
        general_lines=["🔹 **Ops Summary:** open 3 | inactives 1 | reserved 0"],
        per_bracket_lines=[],
        detail_blocks=[
            (
                f"Bracket {idx}",
                [f"🔹 **Clan {idx} " + ("x" * 900) + ":** open 1 | inactives 0 | reserved 0"],
            )
            for idx in range(10)
        ],
    )

    async def runner():
        pager = dru.OpenSpotsPager(sections)
        interaction = MagicMock()
        interaction.response = AsyncMock()
        interaction.response.edit_message = AsyncMock()
        with pytest.raises(dru.DailyReportSectionError):
            await pager.set_details(interaction)
        interaction.response.edit_message.assert_not_awaited()

    asyncio.run(runner())


def test_parse_utc_time_returns_aware_time():
    parsed = dru._parse_utc_time("09:30")
    assert parsed.hour == 9
    assert parsed.minute == 30
    assert parsed.tzinfo is dru.UTC


def test_report_command_feature_disabled(monkeypatch):
    monkeypatch.setattr("cogs.recruitment_reporting.feature_enabled", lambda: False)

    log_calls = []

    async def fake_log_manual_result(**kwargs):
        log_calls.append(kwargs)

    monkeypatch.setattr(
        "cogs.recruitment_reporting.log_manual_result", fake_log_manual_result
    )

    bot = MagicMock()
    cog = RecruitmentReporting(bot)

    ctx = MagicMock()
    ctx.reply = AsyncMock()
    ctx.author.id = 42

    async def runner() -> None:
        await cog.report_group.callback(cog, ctx, "recruiters")

    asyncio.run(runner())

    ctx.reply.assert_awaited_once_with("Daily Recruiter Update is disabled.", mention_author=False)
    assert log_calls
    assert log_calls[0]["result"] == "blocked"
