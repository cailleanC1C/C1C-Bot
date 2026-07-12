import asyncio
from types import SimpleNamespace

import pytest

from cogs.housekeeping_achievements import AchievementsCog
from modules.housekeeping import achievements


class DummyMessage:
    def __init__(self, message_id):
        self.id = message_id
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class DummyChannel:
    def __init__(self):
        self.sent = []
        self.messages = {55: DummyMessage(55)}

    async def send(self, **kwargs):
        msg = DummyMessage(100 + len(self.sent))
        self.sent.append(kwargs)
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            import discord

            raise discord.NotFound(
                SimpleNamespace(status=404, reason="missing"), "missing"
            )
        return self.messages[message_id]


class DummyBot:
    def __init__(self, channel):
        self.channel = channel

    def get_channel(self, channel_id):
        return self.channel if channel_id == 123 else None


@pytest.fixture
def harness(monkeypatch):
    config = {
        "achievement_tab": "Achievements",
        "achievement_range": "A1:H20",
        "achievement_champion_range": "J1:Q20",
        "achievement_post_channel_id": "123",
        "achievement_post_message_id": "55",
    }
    writes = []
    channel = DummyChannel()

    monkeypatch.setattr(
        achievements.recruitment, "get_recruitment_sheet_id", lambda: "sheet123"
    )
    monkeypatch.setattr(
        achievements.recruitment, "get_config_tab_name", lambda: "Config"
    )

    async def afetch_values(sheet_id, tab_name):
        assert sheet_id == "sheet123"
        assert tab_name == "Config"
        return [["Key", "Value"], *[[key, value] for key, value in config.items()]]

    monkeypatch.setattr(achievements.async_core, "afetch_values", afetch_values)
    monkeypatch.setattr(achievements, "get_tab_gid", lambda sheet_id, tab_name: "999")

    async def render(*args, **kwargs):
        return b"png"

    monkeypatch.setattr(achievements, "export_pdf_as_png", render)

    class Worksheet:
        def update(self, target, values, **kwargs):
            writes.append((target, values, kwargs))

    async def aget_worksheet(sheet_id, tab_name):
        assert sheet_id == "sheet123"
        assert tab_name == "Config"
        return Worksheet()

    async def acall_with_backoff(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(achievements.async_core, "aget_worksheet", aget_worksheet)
    monkeypatch.setattr(
        achievements.async_core, "acall_with_backoff", acall_with_backoff
    )
    return SimpleNamespace(
        config=config, writes=writes, channel=channel, bot=DummyBot(channel)
    )


def test_publish_writes_achievement_post_message_id(harness):
    result = asyncio.run(achievements.publish_achievements(harness.bot))

    assert result.status == "success"
    assert result.message_id == 100
    assert harness.writes == [("B6", [["100"]], {"value_input_option": "RAW"})]
    assert len(harness.channel.sent) == 1
    assert len(harness.channel.sent[0]["files"]) == 2


def test_publish_requires_existing_message_id_config_row(harness):
    del harness.config["achievement_post_message_id"]

    with pytest.raises(achievements.AchievementsConfigError) as exc:
        asyncio.run(achievements.publish_achievements(harness.bot))

    assert "achievement_post_message_id" in str(exc.value)
    assert harness.channel.sent == []
    assert harness.writes == []


def test_refresh_edits_configured_message_and_does_not_send(harness):
    result = asyncio.run(achievements.refresh_achievements(harness.bot))

    assert result.status == "success"
    assert harness.channel.sent == []
    assert len(harness.channel.messages[55].edits) == 1
    assert len(harness.channel.messages[55].edits[0]["attachments"]) == 2


def test_refresh_missing_message_id_tells_admin_to_publish(harness):
    harness.config["achievement_post_message_id"] = ""

    with pytest.raises(achievements.AchievementsConfigError) as exc:
        asyncio.run(achievements.refresh_achievements(harness.bot))

    assert "Run !achievements publish" in str(exc.value)
    assert harness.channel.sent == []


def test_refresh_invalid_existing_message_does_not_send(harness):
    harness.config["achievement_post_message_id"] = "999"

    result = asyncio.run(achievements.refresh_achievements(harness.bot))

    assert result.status == "error"
    assert "Run !achievements publish" in result.message
    assert harness.channel.sent == []


def test_render_uses_one_page_export_options(monkeypatch, harness):
    calls = []

    async def render(*args, **kwargs):
        calls.append((args, kwargs))
        return b"png"

    monkeypatch.setattr(achievements, "export_pdf_as_png", render)
    config = asyncio.run(achievements.resolve_config(require_message_id=True))

    files = asyncio.run(achievements.render_achievement_files(config))

    assert len(files) == 2
    assert [call[1]["fit_range_to_one_page"] for call in calls] == [True, True]
    assert [call[1]["fail_on_multi_page"] for call in calls] == [True, True]


def test_multi_page_export_fails_clearly(monkeypatch, harness):
    async def render(*args, **kwargs):
        raise achievements.ImageExportError("image export produced multiple pages")

    monkeypatch.setattr(achievements, "export_pdf_as_png", render)
    config = asyncio.run(achievements.resolve_config(require_message_id=True))

    with pytest.raises(achievements.AchievementsConfigError) as exc:
        asyncio.run(achievements.render_achievement_files(config))

    assert "image export produced multiple pages" in str(exc.value)


def test_achievements_commands_are_admin_gated():
    commands = [
        AchievementsCog.achievements_group,
        AchievementsCog.achievements_publish,
        AchievementsCog.achievements_refresh,
    ]
    for command in commands:
        checks = getattr(command, "checks", [])
        assert checks, f"{command} has no command checks"
