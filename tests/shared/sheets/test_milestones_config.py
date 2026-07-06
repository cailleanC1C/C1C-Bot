import asyncio

import pytest

from shared.sheets import milestones_config


def test_parse_normalizes_headers_keys_and_values():
    rows = [
        {" KEY ": " FUSION_TAB ", " VALUE ": " fusion "},
        {"key": " FUSION_EVENT_TAB ", "value": " fusion_events "},
        {"KEY": " FUSION_REMINDER_SETTINGS_TAB ", "VALUE": " FusionReminderSettings "},
        {"Key": " RESET_REMINDER_TAB ", "Value": " ResetReminder "},
        {"key": " SHARD_MERCY_TAB ", "value": " ShardTracker "},
        {"key": "BLANK_WITH_NOTE", "value": " ", "notes": "not a config value"},
    ]

    parsed = milestones_config._parse(rows)

    assert parsed["FUSION_TAB"] == "fusion"
    assert parsed["FUSION_EVENT_TAB"] == "fusion_events"
    assert parsed["FUSION_REMINDER_SETTINGS_TAB"] == "FusionReminderSettings"
    assert parsed["RESET_REMINDER_TAB"] == "ResetReminder"
    assert parsed["SHARD_MERCY_TAB"] == "ShardTracker"
    assert parsed["BLANK_WITH_NOTE"] == ""


def test_require_value_blank_missing_and_load_failures(monkeypatch):
    async def _run():
        monkeypatch.setenv("MILESTONES_CONFIG_TAB", "ConfigFromEnv")
        monkeypatch.setattr(milestones_config, "get_milestones_sheet_id", lambda: "sheet-abcdef")

        async def blank_records(sheet_id, tab):
            return [{"KEY": "FUSION_TAB", "VALUE": "   "}]

        monkeypatch.setattr(milestones_config.async_core, "afetch_records", blank_records)
        with pytest.raises(milestones_config.MilestonesConfigValueBlank) as blank:
            await milestones_config.arequire_value("FUSION_TAB")
        assert blank.value.reason == "key_value_blank"
        assert blank.value.present is True

        async def missing_records(sheet_id, tab):
            return [{"KEY": "FUSION_TBA", "VALUE": "fusion"}]

        monkeypatch.setattr(milestones_config.async_core, "afetch_records", missing_records)
        with pytest.raises(milestones_config.MilestonesConfigKeyMissing) as missing:
            await milestones_config.arequire_value("FUSION_TAB")
        assert missing.value.reason == "key_missing"
        assert missing.value.keys_loaded == 1
        assert "FUSION_TBA" in missing.value.nearest

        async def load_fails(sheet_id, tab):
            raise RuntimeError("boom")

        monkeypatch.setattr(milestones_config.async_core, "afetch_records", load_fails)
        with pytest.raises(milestones_config.MilestonesConfigLoadFailed) as load_failed:
            await milestones_config.arequire_value("FUSION_TAB")
        assert load_failed.value.reason == "config_load_failed"

        monkeypatch.delenv("MILESTONES_CONFIG_TAB", raising=False)
        with pytest.raises(milestones_config.MilestonesConfigSourceUnavailable) as no_source:
            await milestones_config.arequire_value("FUSION_TAB")
        assert no_source.value.reason == "config_source_unavailable"

        monkeypatch.setenv("MILESTONES_CONFIG_TAB", "ConfigFromEnv")
        monkeypatch.setattr(milestones_config, "get_milestones_sheet_id", lambda: "")
        with pytest.raises(milestones_config.MilestonesSheetIdUnavailable) as no_sheet:
            await milestones_config.arequire_value("FUSION_TAB")
        assert no_sheet.value.reason == "sheet_id_unavailable"

    asyncio.run(_run())


def test_milestones_config_tab_env_selects_config_without_fallback(monkeypatch):
    seen = {}
    monkeypatch.setenv("MILESTONES_CONFIG_TAB", "Config")
    monkeypatch.setattr(milestones_config, "get_milestones_sheet_id", lambda: "sheet-abcdef")

    async def records(sheet_id, tab):
        seen["sheet_id"] = sheet_id
        seen["tab"] = tab
        return [{"KEY": "FUSION_TAB", "VALUE": "fusion"}]

    monkeypatch.setattr(milestones_config.async_core, "afetch_records", records)

    value = asyncio.run(milestones_config.arequire_value("FUSION_TAB"))

    assert value == "fusion"
    assert seen == {"sheet_id": "sheet-abcdef", "tab": "Config"}


def test_milestones_config_does_not_fallback_to_config_when_source_missing(monkeypatch):
    monkeypatch.delenv("MILESTONES_CONFIG_TAB", raising=False)
    monkeypatch.setattr(milestones_config, "get_milestones_sheet_id", lambda: "sheet-abcdef")

    async def records(*args, **kwargs):
        raise AssertionError("Config tab should not be loaded without explicit source")

    monkeypatch.setattr(milestones_config.async_core, "afetch_records", records)

    with pytest.raises(milestones_config.MilestonesConfigSourceUnavailable):
        asyncio.run(milestones_config.arequire_value("FUSION_TAB"))
