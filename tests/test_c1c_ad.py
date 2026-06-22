import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

from modules.housekeeping import c1c_ad


class DummyMessage:
    def __init__(self, message_id):
        self.id = message_id
        self.deleted = False

    async def delete(self):
        self.deleted = True


class DummyChannel:
    name = "recruitment-thread"

    def __init__(self):
        self.sent = []
        self.messages = {10: DummyMessage(10), 11: DummyMessage(11)}

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            import discord

            raise discord.NotFound(
                SimpleNamespace(status=404, reason="missing"), "missing"
            )
        return self.messages[message_id]

    async def send(self, content=None, file=None):
        msg = DummyMessage(100 + len(self.sent))
        self.messages[msg.id] = msg
        self.sent.append((content, file, msg.id))
        return msg


class DummyBot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel


def _config_values():
    return {
        "C1C_AD_TAB": "C1C_AD",
        "C1C_AD_IMAGE_RANGE": "A1:V42",
        "C1C_AD_TEXT_TAB": "C1C_AD_TEXT",
        "C1C_AD_TEXT_ROW": "2",
        "C1C_AD_TARGET_THREAD_ID": "1324313499731755039",
        "C1C_AD_REFRESH_DAYS": "7",
        "REPORTS_TAB": "Statistics",
    }


def _sheet_rows(ad_text="Join C1C!", last_posted=""):
    return [
        [
            "ad_text",
            "last_posted_at_utc",
            "last_image_message_id",
            "last_text_message_id",
            "last_post_status",
            "last_post_error",
            "updated_at_utc",
        ],
        [ad_text, last_posted, "10", "11", "success", "", ""],
    ]


@pytest.fixture
def harness(monkeypatch):
    config = _config_values()
    rows = _sheet_rows()
    updates = []
    channel = DummyChannel()
    monkeypatch.setattr(c1c_ad.feature_flags, "is_enabled", lambda key: True)
    monkeypatch.setattr(
        c1c_ad.recruitment,
        "get_config_value",
        lambda key, default=None: config.get(key, default),
    )
    monkeypatch.setattr(
        c1c_ad.recruitment, "get_recruitment_sheet_id", lambda: "sheet123"
    )
    monkeypatch.setattr(
        c1c_ad.sheets_core, "sheets_read", lambda sheet_id, a1_range: rows
    )
    monkeypatch.setattr(c1c_ad, "get_tab_gid", lambda sheet_id, tab_name: "456")

    async def render(*args, **kwargs):
        return b"png"

    monkeypatch.setattr(c1c_ad, "export_pdf_as_png", render)

    class Worksheet:
        def batch_update(self, cells):
            updates.extend(cells)

    monkeypatch.setattr(
        c1c_ad.sheets_core, "get_worksheet", lambda sheet_id, tab: Worksheet()
    )
    monkeypatch.setattr(
        c1c_ad.sheets_core,
        "call_with_backoff",
        lambda func, *args, **kwargs: func(*args, **kwargs),
    )
    return SimpleNamespace(
        config=config,
        rows=rows,
        updates=updates,
        channel=channel,
        bot=DummyBot(channel),
    )


def test_disabled_feature_skips_without_post(monkeypatch, harness):
    monkeypatch.setattr(c1c_ad.feature_flags, "is_enabled", lambda key: False)
    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))
    assert result.status == "skipped"
    assert harness.channel.sent == []


def test_missing_config_skips_without_post(harness):
    del harness.config["C1C_AD_IMAGE_RANGE"]
    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))
    assert result.status == "skipped"
    assert "missing Config key C1C_AD_IMAGE_RANGE" == result.message
    assert harness.channel.sent == []


def test_missing_header_skips_without_post(harness):
    harness.rows[0][0] = "copy"
    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))
    assert result.status == "skipped"
    assert "missing C1C_AD_TEXT header ad_text" == result.message
    assert harness.channel.sent == []


def test_empty_ad_text_fails_without_post(harness):
    harness.rows[1][0] = ""
    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))
    assert result.status == "failed"
    assert harness.channel.sent == []
    assert any(cell["values"] == [["failed"]] for cell in harness.updates)


def test_render_failure_does_not_post_text_only(monkeypatch, harness):
    async def render(*args, **kwargs):
        return None

    monkeypatch.setattr(c1c_ad, "export_pdf_as_png", render)
    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))
    assert result.status == "failed"
    assert harness.channel.sent == []


def test_success_deletes_old_messages_posts_and_stores_state(harness):
    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))
    assert result.status == "success"
    assert harness.channel.messages[10].deleted is True
    assert harness.channel.messages[11].deleted is True
    assert len(harness.channel.sent) == 2
    text_content, text_file, text_id = harness.channel.sent[0]
    image_content, image_file, image_id = harness.channel.sent[1]
    assert text_content == "Join C1C!"
    assert text_file is None
    assert image_content is None
    assert image_file is not None
    assert text_id == 100
    assert image_id == 101
    written = {cell["range"]: cell["values"][0][0] for cell in harness.updates}
    assert written["C2"] == "101"
    assert written["D2"] == "100"
    assert "success" in written.values()


def test_image_send_failure_deletes_new_text_and_does_not_write_success(harness):
    async def send(content=None, file=None):
        if file is not None:
            raise RuntimeError("image send failed")
        return await DummyChannel.send(harness.channel, content=content, file=file)

    harness.channel.send = send

    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))

    assert result.status == "failed"
    assert result.message == "Discord post failed"
    assert len(harness.channel.sent) == 1
    assert harness.channel.sent[0][0] == "Join C1C!"
    assert harness.channel.messages[100].deleted is True
    written_values = [cell["values"][0][0] for cell in harness.updates]
    assert "success" not in written_values
    assert "Discord post failed" in written_values


def test_scheduled_refresh_not_due_skips_restart_spam(harness):
    recent = (
        (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1))
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )
    harness.rows[1][1] = recent
    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=False))
    assert result.status == "skipped"
    assert result.message == "refresh not due"
    assert harness.channel.sent == []


def test_missing_old_messages_do_not_block_repost(harness):
    harness.rows[1][2] = "404"
    harness.rows[1][3] = "405"
    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))
    assert result.status == "success"
    assert len(harness.channel.sent) == 2


def test_image_range_and_export_options_come_from_config(monkeypatch, harness):
    captured = {}

    async def render(sheet_id, gid, cell_range, **kwargs):
        captured["range"] = cell_range
        captured["kwargs"] = kwargs
        return b"png"

    monkeypatch.setattr(c1c_ad, "export_pdf_as_png", render)
    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))
    assert result.status == "success"
    assert captured["range"] == "A1:V42"
    assert captured["kwargs"]["fit_range_to_one_page"] is True
    assert captured["kwargs"]["fail_on_multi_page"] is True
    assert captured["kwargs"]["crop_to_content"] is False


def _c1cad_check():
    from cogs.housekeeping_c1c_ad import C1CAdCog

    return C1CAdCog.c1cad.checks[0]


class DummyRole:
    def __init__(self, role_id):
        self.id = role_id


class DummyMember:
    def __init__(self, roles=(), administrator=False):
        self.roles = [DummyRole(role_id) for role_id in roles]
        self.guild_permissions = SimpleNamespace(administrator=administrator)
        self.id = 123


class DummyCtx:
    guild = object()
    _coreops_suppress_denials = True
    command = SimpleNamespace(qualified_name="c1cad")

    def __init__(self, author):
        self.author = author


@pytest.fixture
def rbac_members(monkeypatch):
    import c1c_coreops.rbac as rbac

    monkeypatch.setattr(rbac.discord, "Member", DummyMember)
    monkeypatch.setattr(rbac, "get_admin_role_ids", lambda: {1})
    monkeypatch.setattr(rbac, "get_staff_role_ids", lambda: {2})


def test_c1cad_permission_allows_admin(rbac_members):
    predicate = _c1cad_check()
    assert asyncio.run(predicate(DummyCtx(DummyMember(roles=[1])))) is True


def test_c1cad_permission_allows_staff(rbac_members):
    predicate = _c1cad_check()
    assert asyncio.run(predicate(DummyCtx(DummyMember(roles=[2])))) is True


def test_c1cad_permission_denies_non_staff_non_admin(rbac_members):
    from discord.ext import commands

    predicate = _c1cad_check()
    with pytest.raises(commands.CheckFailure):
        asyncio.run(predicate(DummyCtx(DummyMember(roles=[3]))))


def _extend_c1c_ad_text_headers(harness):
    headers = harness.rows[0]
    row = harness.rows[1]
    extra = [
        ("open_spots_endgame_brackets", "Elite End Game, Early End Game"),
        ("open_spots_lategame_brackets", "Late Game"),
        ("open_spots_midgame_brackets", "Mid Game"),
        ("open_spots_early_brackets", "Early Game, Beginners"),
        ("open_spots_empty_text", "Join us and we’ll help you find the right fit."),
    ]
    for header, value in extra:
        if header not in headers:
            headers.append(header)
            row.append(value)


def _statistics_rows():
    return [
        [
            "H1_Headline",
            "H2_Headline",
            "Key",
            "open_spots",
            "inactives",
            "reserved_spots",
        ],
        ["Bracket Details", "", "", "", "", ""],
        ["", "Elite End Game", "", "", "", ""],
        ["", "", "Torns Valhalla", "2", "99", "99"],
        ["", "", "Closed Endgame", "0", "0", "0"],
        ["", "Early End Game", "", "", "", ""],
        ["", "", "Shadow Shoguns", "1", "5", "7"],
        ["", "Late Game", "", "", "", ""],
        ["", "", "Late Open", "3", "1", "1"],
        ["", "Mid Game", "", "", "", ""],
        ["", "", "Mid Open", "4", "0", "0"],
        ["", "Early Game", "", "", "", ""],
        ["", "", "Early Closed", "0", "0", "0"],
        ["", "Beginners", "", "", "", ""],
        ["", "", "Beginner Open", "5", "8", "9"],
    ]


def test_dynamic_placeholders_replace_all_groups(monkeypatch, harness):
    _extend_c1c_ad_text_headers(harness)
    harness.rows[1][0] = "\n".join(
        [
            "End [OPEN_SPOTS_ENDGAME]",
            "Late [OPEN_SPOTS_LATEGAME]",
            "Mid [OPEN_SPOTS_MIDGAME]",
            "Early [OPEN_SPOTS_EARLY]",
        ]
    )

    async def fetch_stats(tab_name=None):
        return _statistics_rows()

    monkeypatch.setattr(c1c_ad, "afetch_reports_tab", fetch_stats)

    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))

    assert result.status == "success"
    posted_text = harness.channel.sent[0][0]
    assert "Open right now: **Torns Valhalla, Shadow Shoguns**" in posted_text
    assert "Open right now: **Late Open**" in posted_text
    assert "Open right now: **Mid Open**" in posted_text
    assert "Open right now: **Beginner Open**" in posted_text
    assert "Closed Endgame" not in posted_text
    assert "Early Closed" not in posted_text
    assert "open 2" not in posted_text
    assert "99" not in posted_text
    assert harness.rows[1][0].startswith("End [OPEN_SPOTS_ENDGAME]")


def test_dynamic_placeholder_empty_result_uses_empty_text(monkeypatch, harness):
    _extend_c1c_ad_text_headers(harness)
    harness.rows[1][0] = "Early [OPEN_SPOTS_EARLY]"

    async def fetch_stats(tab_name=None):
        rows = _statistics_rows()
        rows[-1][3] = "0"
        return rows

    monkeypatch.setattr(c1c_ad, "afetch_reports_tab", fetch_stats)

    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))

    assert result.status == "success"
    assert (
        harness.channel.sent[0][0]
        == "Early Join us and we’ll help you find the right fit."
    )


def test_dynamic_placeholder_missing_mapping_warns_and_uses_empty_text(
    monkeypatch, harness, caplog
):
    _extend_c1c_ad_text_headers(harness)
    mapping_col = harness.rows[0].index("open_spots_endgame_brackets")
    harness.rows[1][mapping_col] = ""
    harness.rows[1][0] = "End [OPEN_SPOTS_ENDGAME]"

    async def fetch_stats(tab_name=None):
        return _statistics_rows()

    monkeypatch.setattr(c1c_ad, "afetch_reports_tab", fetch_stats)

    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))

    assert result.status == "success"
    assert (
        harness.channel.sent[0][0]
        == "End Join us and we’ll help you find the right fit."
    )
    assert "missing bracket mapping placeholder=OPEN_SPOTS_ENDGAME" in caplog.text


def test_dynamic_placeholder_statistics_read_failure_fails_without_post(
    monkeypatch, harness
):
    _extend_c1c_ad_text_headers(harness)
    harness.rows[1][0] = "End [OPEN_SPOTS_ENDGAME]"

    async def fetch_stats(tab_name=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(c1c_ad, "afetch_reports_tab", fetch_stats)

    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))

    assert result.status == "failed"
    assert result.message == "Statistics tab read failed"
    assert harness.channel.sent == []
    assert any(
        cell["values"] == [["Statistics tab read failed"]] for cell in harness.updates
    )


def test_dynamic_placeholder_statistics_missing_header_fails_without_post(
    monkeypatch, harness
):
    _extend_c1c_ad_text_headers(harness)
    harness.rows[1][0] = "End [OPEN_SPOTS_ENDGAME]"

    async def fetch_stats(tab_name=None):
        rows = _statistics_rows()
        rows[0][3] = "spots"
        return rows

    monkeypatch.setattr(c1c_ad, "afetch_reports_tab", fetch_stats)

    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))

    assert result.status == "failed"
    assert result.message == "Statistics headers missing open_spots"
    assert harness.channel.sent == []


def test_dynamic_placeholder_missing_empty_text_fails_without_post(
    monkeypatch, harness
):
    _extend_c1c_ad_text_headers(harness)
    empty_col = harness.rows[0].index("open_spots_empty_text")
    harness.rows[1][empty_col] = ""
    harness.rows[1][0] = "End [OPEN_SPOTS_ENDGAME]"

    async def fetch_stats(tab_name=None):
        return _statistics_rows()

    monkeypatch.setattr(c1c_ad, "afetch_reports_tab", fetch_stats)

    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))

    assert result.status == "failed"
    assert result.message == "open_spots_empty_text empty"
    assert harness.channel.sent == []


def test_resolved_text_over_discord_limit_fails_before_delete_or_post(
    monkeypatch, harness, caplog
):
    _extend_c1c_ad_text_headers(harness)
    harness.rows[1][0] = "End [OPEN_SPOTS_ENDGAME]"
    long_name = "A" * 2010

    async def fetch_stats(tab_name=None):
        rows = _statistics_rows()
        rows[3][2] = long_name
        rows[6][3] = "0"
        return rows

    async def render(*args, **kwargs):
        raise AssertionError("image render should not run when text is too long")

    monkeypatch.setattr(c1c_ad, "afetch_reports_tab", fetch_stats)
    monkeypatch.setattr(c1c_ad, "export_pdf_as_png", render)

    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))

    assert result.status == "failed"
    assert result.message == "resolved ad text exceeds Discord 2000 character limit"
    assert harness.channel.sent == []
    assert harness.channel.messages[10].deleted is False
    assert harness.channel.messages[11].deleted is False
    assert any(
        cell["values"] == [["resolved ad text exceeds Discord 2000 character limit"]]
        for cell in harness.updates
    )
    assert "resolved ad text exceeds Discord limit chars=" in caplog.text
    assert "limit=2000" in caplog.text
    assert long_name not in caplog.text


def test_plain_ad_text_over_discord_limit_fails_before_delete_or_post(
    monkeypatch, harness
):
    harness.rows[1][0] = "x" * 2001

    async def render(*args, **kwargs):
        raise AssertionError("image render should not run when text is too long")

    monkeypatch.setattr(c1c_ad, "export_pdf_as_png", render)

    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))

    assert result.status == "failed"
    assert result.message == "resolved ad text exceeds Discord 2000 character limit"
    assert harness.channel.sent == []
    assert harness.channel.messages[10].deleted is False
    assert harness.channel.messages[11].deleted is False


def test_dynamic_placeholder_uses_configured_reports_tab(harness, monkeypatch):
    _extend_c1c_ad_text_headers(harness)
    harness.config["REPORTS_TAB"] = "Configured Stats"
    harness.rows[1][0] = "End [OPEN_SPOTS_ENDGAME]"
    captured = {}

    async def fetch_stats(tab_name=None):
        captured["tab_name"] = tab_name
        return _statistics_rows()

    monkeypatch.setattr(c1c_ad, "afetch_reports_tab", fetch_stats)

    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))

    assert result.status == "success"
    assert captured["tab_name"] == "Configured Stats"


def test_dynamic_placeholder_missing_reports_tab_config_fails_without_post(harness):
    _extend_c1c_ad_text_headers(harness)
    del harness.config["REPORTS_TAB"]
    harness.rows[1][0] = "End [OPEN_SPOTS_ENDGAME]"

    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))

    assert result.status == "failed"
    assert result.message == "reports tab config missing"
    assert harness.channel.sent == []


def test_multi_page_image_export_fails_before_delete_or_post(monkeypatch, harness):
    async def render(*args, **kwargs):
        raise c1c_ad.ImageExportError("image export produced multiple pages")

    monkeypatch.setattr(c1c_ad, "export_pdf_as_png", render)

    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))

    assert result.status == "failed"
    assert result.message == "image export produced multiple pages"
    assert harness.channel.sent == []
    assert harness.channel.messages[10].deleted is False
    assert harness.channel.messages[11].deleted is False
    assert any(
        cell["values"] == [["image export produced multiple pages"]]
        for cell in harness.updates
    )
