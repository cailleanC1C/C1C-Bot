import asyncio
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
    monkeypatch.setattr(dru, "_role_mentions", lambda: ())
    monkeypatch.setattr(dru.discord, "TextChannel", DummyChannel)

    ok, error = asyncio.run(dru.post_daily_recruiter_update(bot))

    assert ok is True
    assert error == "-"
    assert channel.sent
    sent_kwargs = channel.sent[0]
    assert len(sent_kwargs["embeds"]) == 1
    assert isinstance(sent_kwargs["view"], dru.OpenSpotsPager)


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
