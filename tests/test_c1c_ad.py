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
    written_values = [cell["values"][0][0] for cell in harness.updates]
    assert "100" in written_values
    assert "101" in written_values
    assert "success" in written_values


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


def test_image_range_comes_from_config(monkeypatch, harness):
    captured = {}

    async def render(sheet_id, gid, cell_range, **kwargs):
        captured["range"] = cell_range
        return b"png"

    monkeypatch.setattr(c1c_ad, "export_pdf_as_png", render)
    result = asyncio.run(c1c_ad.run_c1c_ad_job(harness.bot, force=True))
    assert result.status == "success"
    assert captured["range"] == "A1:V42"


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
