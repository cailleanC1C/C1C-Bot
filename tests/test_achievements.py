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
        self.messages = {
            55: DummyMessage(55),
            56: DummyMessage(56),
            57: DummyMessage(57),
        }

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
        "achievement_post_channel_id": "123",
        "achievement_range_count": "3",
        "achievement_range_1": "A1:H20",
        "achievement_range_2": "A22:H40",
        "achievement_range_3": "A42:H60",
        "achievement_post_message_id_1": "55",
        "achievement_post_message_id_2": "56",
        "achievement_post_message_id_3": "57",
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


def test_publish_sends_configured_messages_and_writes_ids(harness):
    result = asyncio.run(achievements.publish_achievements(harness.bot))

    assert result.status == "success"
    assert result.message_ids == (100, 101, 102)
    assert harness.writes == [
        ("B8", [["100"]], {"value_input_option": "RAW"}),
        ("B9", [["101"]], {"value_input_option": "RAW"}),
        ("B10", [["102"]], {"value_input_option": "RAW"}),
    ]
    assert len(harness.channel.sent) == 3
    assert [len(sent["files"]) for sent in harness.channel.sent] == [1, 1, 1]
    assert harness.channel.sent[0]["files"][0].filename == "achievements_1.png"
    assert harness.channel.sent[1]["files"][0].filename == "achievements_2.png"
    assert harness.channel.sent[2]["files"][0].filename == "achievements_3.png"
    assert harness.channel.sent[0]["content"].startswith(
        "# Achievements\n-# Last updated "
    )
    assert harness.channel.sent[1]["content"] == ""
    assert harness.channel.sent[2]["content"] == ""


def test_publish_allows_blank_message_ids_when_rows_exist(harness):
    harness.config["achievement_post_message_id_1"] = ""
    harness.config["achievement_post_message_id_2"] = ""
    harness.config["achievement_post_message_id_3"] = ""

    result = asyncio.run(achievements.publish_achievements(harness.bot))

    assert result.status == "success"
    assert result.message_ids == (100, 101, 102)


def test_publish_requires_existing_message_id_config_rows(harness):
    del harness.config["achievement_post_message_id_2"]

    with pytest.raises(achievements.AchievementsConfigError) as exc:
        asyncio.run(achievements.publish_achievements(harness.bot))

    assert "achievement_post_message_id_2" in str(exc.value)
    assert harness.channel.sent == []
    assert harness.writes == []


def test_missing_range_key_fails_clearly(harness):
    del harness.config["achievement_range_2"]

    with pytest.raises(achievements.AchievementsConfigError) as exc:
        asyncio.run(achievements.publish_achievements(harness.bot))

    assert "achievement_range_2" in str(exc.value)
    assert harness.channel.sent == []


def test_blank_range_key_fails_clearly(harness):
    harness.config["achievement_range_2"] = ""

    with pytest.raises(achievements.AchievementsConfigError) as exc:
        asyncio.run(achievements.publish_achievements(harness.bot))

    assert "achievement_range_2" in str(exc.value)
    assert "must not be blank" in str(exc.value)


def test_refresh_edits_configured_messages_and_does_not_send(harness):
    result = asyncio.run(achievements.refresh_achievements(harness.bot))

    assert result.status == "success"
    assert result.message_ids == (55, 56, 57)
    assert harness.channel.sent == []
    for message_id in (55, 56, 57):
        assert len(harness.channel.messages[message_id].edits) == 1
        edit = harness.channel.messages[message_id].edits[0]
        assert len(edit["attachments"]) == 1
        assert "files" not in edit
    assert (
        harness.channel.messages[55]
        .edits[0]["content"]
        .startswith("# Achievements\n-# Last updated ")
    )
    assert harness.channel.messages[56].edits[0]["content"] == ""
    assert harness.channel.messages[57].edits[0]["content"] == ""


def test_refresh_blank_message_id_tells_admin_to_publish(harness):
    harness.config["achievement_post_message_id_1"] = ""

    with pytest.raises(achievements.AchievementsConfigError) as exc:
        asyncio.run(achievements.refresh_achievements(harness.bot))

    assert "Run !achievements publish" in str(exc.value)
    assert harness.channel.sent == []


def test_refresh_missing_message_id_config_row_fails_clearly(harness):
    del harness.config["achievement_post_message_id_3"]

    with pytest.raises(achievements.AchievementsConfigError) as exc:
        asyncio.run(achievements.refresh_achievements(harness.bot))

    assert "achievement_post_message_id_3" in str(exc.value)
    assert harness.channel.sent == []


def test_refresh_invalid_existing_message_does_not_send(harness):
    harness.config["achievement_post_message_id_2"] = "999"

    result = asyncio.run(achievements.refresh_achievements(harness.bot))

    assert result.status == "error"
    assert "Run !achievements publish" in result.message
    assert harness.channel.sent == []


def test_range_count_must_be_positive_integer(harness):
    harness.config["achievement_range_count"] = "0"

    with pytest.raises(achievements.AchievementsConfigError) as exc:
        asyncio.run(achievements.publish_achievements(harness.bot))

    assert "achievement_range_count must be a positive integer" in str(exc.value)


def test_render_uses_one_page_export_options(monkeypatch, harness):
    calls = []

    async def render(*args, **kwargs):
        calls.append((args, kwargs))
        return b"png"

    monkeypatch.setattr(achievements, "export_pdf_as_png", render)
    config = asyncio.run(achievements.resolve_config(require_message_id=True))

    files = asyncio.run(achievements.render_achievement_files(config))

    assert len(files) == 3
    assert [call[1]["fit_range_to_one_page"] for call in calls] == [True, True, True]
    assert [call[1]["fail_on_multi_page"] for call in calls] == [True, True, True]
    assert [call[1]["crop_to_content"] for call in calls] == [True, True, True]


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
