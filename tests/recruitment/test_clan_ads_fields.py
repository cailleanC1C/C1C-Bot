from modules.recruitment import clan_ads
from shared.sheets import recruitment


def test_clan_ads_bracket_resolves_from_progression_header(monkeypatch):
    monkeypatch.setattr(
        recruitment,
        "get_clan_header_map",
        lambda: {"clan_tag": 0, "clan_name": 1, "bracket": 2},
    )
    record = recruitment.RecruitmentClanRecord(
        row=("C1CE", "Clan One", "Late Game"),
        open_spots=3,
        inactives=0,
        reserved=0,
        roster="",
    )

    data = clan_ads.clan_data(record)

    assert data.bracket == "Late Game"


def test_clan_ads_bracket_falls_back_to_record_roster(monkeypatch):
    monkeypatch.setattr(
        recruitment,
        "get_clan_header_map",
        lambda: {"clan_tag": 0, "clan_name": 1},
    )
    record = recruitment.RecruitmentClanRecord(
        row=("C1CE", "Clan One"),
        open_spots=1,
        inactives=0,
        reserved=0,
        roster="Elite End Game",
    )

    data = clan_ads.clan_data(record)

    assert data.bracket == "Elite End Game"


def test_clan_ads_run_reports_when_all_clans_fail_required_field_resolution(
    monkeypatch,
):
    import asyncio

    class Bot:
        pass

    class Channel:
        pass

    async def fake_load_config(*args, **kwargs):
        return clan_ads.Config(
            messages_tab="ClanAdMessages",
            rules_tab="ClanAdRules",
            channel_id=123,
            raid_role_id="",
            notification="",
            interval_hours=24,
            last_posted="",
        )

    async def fake_resolve_channel(*args, **kwargs):
        return Channel()

    async def fake_load_rules(*args, **kwargs):
        return {}

    async def fake_load_messages(*args, **kwargs):
        return {}, None, {}

    async def fake_fetch_clan_records(*args, **kwargs):
        return [
            recruitment.RecruitmentClanRecord(
                row=("C1CE",),
                open_spots=1,
                inactives=0,
                reserved=0,
                roster="Late Game",
            )
        ]

    async def fake_send_log_message(*args, **kwargs):
        return None

    monkeypatch.setattr(clan_ads.feature_flags, "is_enabled", lambda _key: True)
    monkeypatch.setattr(clan_ads, "load_config", fake_load_config)
    monkeypatch.setattr(clan_ads, "_resolve_channel", fake_resolve_channel)
    monkeypatch.setattr(clan_ads, "load_rules", fake_load_rules)
    monkeypatch.setattr(clan_ads, "load_messages", fake_load_messages)
    monkeypatch.setattr(clan_ads.sheets, "fetch_clan_records", fake_fetch_clan_records)
    monkeypatch.setattr(
        clan_ads.runtime_helpers, "send_log_message", fake_send_log_message
    )
    monkeypatch.setattr(recruitment, "get_clan_header_map", lambda: {"clan_tag": 0})

    result = asyncio.run(clan_ads.run(Bot(), scheduled=False))

    assert result["message"] == (
        "Clan ads could not evaluate any clans because required clan data fields are missing. "
        "Check the bot logging channel for details."
    )
    assert result["skipped"] == 1


def _message_row(
    row_number=2,
    tag="C1CE",
    enabled=True,
    title="",
    desc="",
    footer="",
    last_id="",
    color="",
):
    return clan_ads.MessageRow(
        row_number=row_number,
        tag=tag,
        enabled=enabled,
        embed_title=title,
        embed_description=desc,
        embed_footer=footer,
        last_message_id=last_id,
        embed_color=color,
    )


def _clan():
    return clan_ads.ClanData(
        record=None,
        tag="C1CE",
        name="Clan One",
        bracket="Late Game",
        open_spots=3,
        description="Desc",
    )


def test_clan_ad_messages_embed_headers_do_not_require_message(monkeypatch):
    import asyncio

    rows = [
        [
            "clan_tag",
            "enabled",
            "embed_title",
            "embed_description",
            "embed_footer",
            "last_ad_message_id",
            "last_posted_at_utc",
            "last_open_spots",
            "last_status",
            "last_error",
            "embed_color",
        ],
        [
            "default",
            "TRUE",
            "Join {clan_name}",
            "Open: {open_spots}",
            "Footer {clan_tag}",
            "",
            "",
            "",
            "",
            "",
            "#5865F2",
        ],
    ]

    async def fake_fetch_values(*args, **kwargs):
        return rows

    monkeypatch.setattr(clan_ads.sheets, "fetch_values", fake_fetch_values)
    monkeypatch.setattr(
        clan_ads.recruitment, "get_recruitment_sheet_id", lambda: "sheet"
    )
    loaded = asyncio.run(
        clan_ads.load_messages(
            clan_ads.Config("ClanAdMessages", "Rules", 1, "", "", 24, ""),
            clan_ads.RunReporter(None),
        )
    )

    assert loaded is not None
    _items, default, header_map = loaded
    assert "message" not in header_map
    assert {"embed_title", "embed_description", "embed_footer", "embed_color"} <= set(
        header_map
    )
    assert default.embed_title == "Join {clan_name}"
    assert default.embed_color == "#5865F2"


def test_embed_field_fallbacks_are_independent_and_footer_optional():
    clan = _clan()
    default = _message_row(
        tag="DEFAULT",
        title="Default {clan_name}",
        desc="Default {open_spots}",
        footer="Default {clan_tag}",
    )
    title_only = _message_row(title="Clan {bracket}")
    desc_only = _message_row(desc="Clan desc {clan_description}")
    empty_footer_default = _message_row(
        tag="DEFAULT", title="Default", desc="Default", footer=""
    )

    assert (
        clan_ads.render(title_only.embed_title or default.embed_title, clan, None)
        == "Clan Late Game"
    )
    assert (
        clan_ads.render(
            title_only.embed_description or default.embed_description, clan, None
        )
        == "Default 3"
    )
    assert (
        clan_ads.render(desc_only.embed_title or default.embed_title, clan, None)
        == "Default Clan One"
    )
    assert (
        clan_ads.render(
            desc_only.embed_description or default.embed_description, clan, None
        )
        == "Clan desc Desc"
    )
    assert (
        clan_ads.render(
            desc_only.embed_footer or empty_footer_default.embed_footer, clan, None
        )
        == ""
    )


def test_missing_embed_title_or_description_skips_with_clear_status(monkeypatch):
    import asyncio

    writes = []

    async def fake_write_state(*args, **kwargs):
        writes.append(kwargs)

    async def fake_warn(*args, **kwargs):
        return None

    monkeypatch.setattr(clan_ads, "write_state", fake_write_state)
    reporter = clan_ads.RunReporter(None)
    monkeypatch.setattr(reporter, "warn", fake_warn)

    decision = asyncio.run(
        clan_ads.decide(
            _clan(),
            {"C1CE": _message_row(title="", desc="")},
            _message_row(tag="DEFAULT", title="", desc=""),
            {},
            {"last_status": 8, "last_error": 9},
            clan_ads.Config("ClanAdMessages", "Rules", 1, "", "", 24, ""),
            reporter,
        )
    )

    assert decision.status == clan_ads.STATUS_MISSING_DEFAULT
    assert decision.status != clan_ads.STATUS_NOT_QUALIFIED
    assert "embed_title" in writes[-1]["last_error"]
    assert "embed_description" in writes[-1]["last_error"]


def test_post_decision_posts_embed_deletes_old_and_writes_new_id(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    writes = []
    deleted = []
    sent = []

    class Old:
        async def delete(self):
            deleted.append(True)

    class Channel:
        guild = None

        async def fetch_message(self, message_id):
            assert message_id == 99
            return Old()

        async def send(self, *args, **kwargs):
            sent.append((args, kwargs))
            return SimpleNamespace(id=1234)

    async def fake_write_state(*args, **kwargs):
        writes.append(kwargs)

    monkeypatch.setattr(clan_ads, "write_state", fake_write_state)
    row = _message_row(
        title="Join {clan_name}",
        desc="Open {open_spots}",
        footer="Tag {clan_tag}",
        last_id="99",
    )
    decision = clan_ads.Decision("C1CE", _clan(), row, "qualified", "qualified")

    ok = asyncio.run(
        clan_ads.post_decision(
            SimpleNamespace(),
            Channel(),
            clan_ads.Config("ClanAdMessages", "Rules", 1, "", "", 24, ""),
            {},
            _message_row(tag="DEFAULT", title="D", desc="D"),
            decision,
            None,
            clan_ads.RunReporter(None),
        )
    )

    assert ok is True
    assert deleted == [True]
    assert sent[0][0] == ()
    assert sent[0][1]["embed"].title == "Join Clan One"
    assert sent[0][1]["embed"].description == "Open 3"
    assert sent[0][1]["view"].timeout is None
    assert sent[0][1]["view"].children[0].custom_id == "clan_ads:view_card:C1CE"
    assert writes[-1]["last_ad_message_id"] == "1234"


def test_clan_ads_button_builds_both_profile_pages():
    import asyncio
    from types import SimpleNamespace
    import discord

    class Cog:
        async def build_profile_pages(self, tag, *, guild):
            return (
                [discord.Embed(title="Profile"), discord.Embed(title="Entry")],
                [],
                SimpleNamespace(),
            )

    class Bot:
        def get_cog(self, name):
            assert name == "ClanProfileCog"
            return Cog()

    embeds, files, state = asyncio.run(clan_ads.build_clan_card(Bot(), "c1ce", None))
    assert [embed.title for embed in embeds] == ["Profile", "Entry"]
    assert files == []
    assert state is not None


def test_manual_clanads_summary_auto_deletes_only_in_ad_channel(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    from cogs.recruitment_clan_ads import ClanAdsCog

    sends = []

    class Ctx:
        channel = SimpleNamespace(id=123)

        async def send(self, content, **kwargs):
            sends.append((content, kwargs))

    async def fake_run(*args, **kwargs):
        return {
            "message": "Posted 1 clan ad(s).",
            "config": clan_ads.Config("ClanAdMessages", "Rules", 123, "", "", 24, ""),
        }

    monkeypatch.setattr(clan_ads, "run", fake_run)
    command = ClanAdsCog.post.callback
    asyncio.run(command(ClanAdsCog(SimpleNamespace()), Ctx(), "all"))

    assert sends == [("Posted 1 clan ad(s).", {"delete_after": 20})]


def test_embed_color_parsing_and_fallbacks(monkeypatch):
    import asyncio

    warnings = []
    reporter = clan_ads.RunReporter(None)

    async def fake_warn(message, *, dedupe_key=None):
        warnings.append((message, dedupe_key))

    monkeypatch.setattr(reporter, "warn", fake_warn)
    default = _message_row(tag="DEFAULT", color="5865F2")

    assert clan_ads.parse_embed_color("#5865F2") == 0x5865F2
    assert clan_ads.parse_embed_color("5865F2") == 0x5865F2
    assert clan_ads.parse_embed_color("0x5865F2") == 0x5865F2
    assert (
        asyncio.run(
            clan_ads.resolve_embed_color(
                _message_row(color="0xABCDEF"), default, "C1CE", reporter
            )
        )
        == 0xABCDEF
    )
    assert (
        asyncio.run(
            clan_ads.resolve_embed_color(
                _message_row(color=""), default, "C1CE", reporter
            )
        )
        == 0x5865F2
    )
    assert (
        asyncio.run(
            clan_ads.resolve_embed_color(
                _message_row(color=""),
                _message_row(tag="DEFAULT", color=""),
                "C1CE",
                reporter,
            )
        )
        is None
    )
    assert (
        asyncio.run(
            clan_ads.resolve_embed_color(
                _message_row(color="not-a-color"), default, "C1CE", reporter
            )
        )
        is None
    )
    assert "invalid embed_color `not-a-color`" in warnings[-1][0]


def test_post_decision_applies_color_and_crest_thumbnail(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import discord

    sent = []

    class Channel:
        guild = None

        async def send(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(id=5678)

    async def fake_write_state(*args, **kwargs):
        return None

    async def fake_resolve_clan_crest_thumbnail(*args, **kwargs):
        return clan_ads.ClanCrestThumbnail(
            thumbnail_url="https://example.com/crest.png"
        )

    monkeypatch.setattr(clan_ads, "write_state", fake_write_state)
    monkeypatch.setattr(
        clan_ads, "resolve_clan_crest_thumbnail", fake_resolve_clan_crest_thumbnail
    )
    decision = clan_ads.Decision(
        "C1CE",
        _clan(),
        _message_row(title="Join", desc="Open", color="#123456"),
        "qualified",
        "qualified",
    )

    ok = asyncio.run(
        clan_ads.post_decision(
            SimpleNamespace(),
            Channel(),
            clan_ads.Config("ClanAdMessages", "Rules", 1, "", "", 24, ""),
            {},
            _message_row(tag="DEFAULT", title="D", desc="D"),
            decision,
            None,
            clan_ads.RunReporter(None),
        )
    )

    assert ok is True
    embed = sent[0]["embed"]
    assert embed.color.value == 0x123456
    assert embed.thumbnail.url == "https://example.com/crest.png"
    assert sent[0]["view"].timeout is None


def test_post_decision_sends_attachment_crest_file(monkeypatch):
    import asyncio
    from types import SimpleNamespace
    import discord

    sent = []

    class Channel:
        guild = None

        async def send(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(id=3456)

    async def fake_write_state(*args, **kwargs):
        return None

    crest_file = object()

    async def fake_resolve_clan_crest_thumbnail(*args, **kwargs):
        return clan_ads.ClanCrestThumbnail(
            thumbnail_url="attachment://c1ce-badge.png", file=crest_file
        )

    monkeypatch.setattr(clan_ads, "write_state", fake_write_state)
    monkeypatch.setattr(
        clan_ads, "resolve_clan_crest_thumbnail", fake_resolve_clan_crest_thumbnail
    )
    decision = clan_ads.Decision(
        "C1CE",
        _clan(),
        _message_row(title="Join", desc="Open"),
        "qualified",
        "qualified",
    )

    ok = asyncio.run(
        clan_ads.post_decision(
            SimpleNamespace(),
            Channel(),
            clan_ads.Config("ClanAdMessages", "Rules", 1, "", "", 24, ""),
            {},
            _message_row(tag="DEFAULT", title="D", desc="D"),
            decision,
            None,
            clan_ads.RunReporter(None),
        )
    )

    assert ok is True
    assert sent[0]["embed"].thumbnail.url == "attachment://c1ce-badge.png"
    assert sent[0]["files"] == [crest_file]


def test_post_decision_still_posts_without_static_clan_card_thumbnail(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    sent = []

    class Channel:
        guild = None

        async def send(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(id=9012)

    async def fake_write_state(*args, **kwargs):
        return None

    async def fake_resolve_clan_crest_thumbnail(*args, **kwargs):
        return clan_ads.ClanCrestThumbnail()

    monkeypatch.setattr(clan_ads, "write_state", fake_write_state)
    monkeypatch.setattr(
        clan_ads, "resolve_clan_crest_thumbnail", fake_resolve_clan_crest_thumbnail
    )
    decision = clan_ads.Decision(
        "C1CE",
        _clan(),
        _message_row(title="Join", desc="Open"),
        "qualified",
        "qualified",
    )

    ok = asyncio.run(
        clan_ads.post_decision(
            SimpleNamespace(),
            Channel(),
            clan_ads.Config("ClanAdMessages", "Rules", 1, "", "", 24, ""),
            {},
            _message_row(tag="DEFAULT", title="D", desc="D"),
            decision,
            None,
            clan_ads.RunReporter(None),
        )
    )

    assert ok is True
    assert not sent[0]["embed"].thumbnail.url
