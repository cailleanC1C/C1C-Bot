from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from modules.community.shard_tracker import data as shard_data


class _RuntimeConfigStub:
    def __init__(self, values: dict[str, object]):
        self._values = {str(key).lower(): value for key, value in values.items()}

    def get(self, key, default=None):  # pragma: no cover - trivial passthrough
        lookup = str(key).lower()
        return self._values.get(lookup, default)


def _patch_runtime_config(
    monkeypatch, *, tab: str = "ShardTracker", sheet_channel: object = 987654321
) -> None:
    stub = _RuntimeConfigStub(
        {
            "shard_mercy_tab": tab,
            "shard_mercy_channel_id": sheet_channel,
        }
    )
    monkeypatch.setattr(shard_data, "runtime_config", stub)


def test_get_config_prefers_env_channel_id(monkeypatch):
    async def runner():
        store = shard_data.ShardSheetStore()
        monkeypatch.setattr(shard_data, "get_milestones_sheet_id", lambda: "sheet-123")
        _patch_runtime_config(monkeypatch, sheet_channel=999999999)
        monkeypatch.setenv("SHARD_MERCY_CHANNEL_ID", "123")

        config = await store.get_config()

        assert config.sheet_id == "sheet-123"
        assert config.tab_name == "ShardTracker"
        assert config.channel_id == 123

    asyncio.run(runner())


def test_get_config_sheet_fallback_when_env_missing(monkeypatch):
    async def runner():
        store = shard_data.ShardSheetStore()
        monkeypatch.setattr(shard_data, "get_milestones_sheet_id", lambda: "sheet-abc")
        _patch_runtime_config(monkeypatch, sheet_channel="456")
        monkeypatch.delenv("SHARD_MERCY_CHANNEL_ID", raising=False)

        config = await store.get_config()

        assert config.sheet_id == "sheet-abc"
        assert config.tab_name == "ShardTracker"
        assert config.channel_id == 456

    asyncio.run(runner())


def test_get_config_missing_tab_raises(monkeypatch):
    async def runner():
        store = shard_data.ShardSheetStore()
        monkeypatch.setattr(shard_data, "get_milestones_sheet_id", lambda: "sheet-321")
        _patch_runtime_config(monkeypatch, tab="", sheet_channel=999)
        monkeypatch.delenv("SHARD_MERCY_CHANNEL_ID", raising=False)

        with pytest.raises(shard_data.ShardTrackerConfigError):
            await store.get_config()

    asyncio.run(runner())


def test_load_record_existing_row(monkeypatch):
    async def runner():
        store = shard_data.ShardSheetStore()
        config = shard_data.ShardTrackerConfig(
            sheet_id="sheet-1", tab_name="ShardTracker", channel_id=123
        )
        store.get_config = AsyncMock(return_value=config)

        header = list(shard_data.EXPECTED_HEADERS)
        row = [
            "12345",
            "Tester",
            "5",
            "6",
            "7",
            "8",
            "10",
            "11",
            "12",
            "13",
            "14",
            "2024-01-01T00:00:00+00:00",
            "2024-01-02T00:00:00+00:00",
            "2024-01-03T00:00:00+00:00",
            "2024-01-04T00:00:00+00:00",
            "2024-01-05T00:00:00+00:00",
            "17",
            "18",
            "19",
            "20",
            "21",
            "2024-01-06T00:00:00+00:00",
            "22",
            "2300",
            "24",
            "2024-01-07T00:00:00+00:00",
            "25",
        ]

        async def fake_values(sheet_id, tab_name, **kwargs):
            return [header, row]

        monkeypatch.setattr(shard_data.async_core, "afetch_values", fake_values)

        record = await store.load_record(12345, "New Name")

        assert record.row_number == 2
        assert record.voids_owned == 6
        assert record.sacreds_since_lego == 12
        assert record.mysteries_owned == 22
        assert record.remnants_owned == 2300
        assert record.remnants_since_mythic == 24
        assert record.last_remnant_mythic_iso == "2024-01-07T00:00:00+00:00"
        assert record.last_remnant_mythic_depth == 25
        assert record.username_snapshot.startswith("New Name")

    asyncio.run(runner())


def test_load_record_appends_when_missing(monkeypatch):
    async def runner():
        store = shard_data.ShardSheetStore()
        config = shard_data.ShardTrackerConfig(
            sheet_id="sheet-1", tab_name="ShardTracker", channel_id=123
        )
        store.get_config = AsyncMock(return_value=config)

        async def fake_values(sheet_id, tab_name, **kwargs):
            return [list(shard_data.EXPECTED_HEADERS)]

        monkeypatch.setattr(shard_data.async_core, "afetch_values", fake_values)

        class DummyWorksheet:
            def __init__(self) -> None:
                self.append_payloads: list[list[str]] = []

            async def append_row(self, row, value_input_option="RAW"):
                self.append_payloads.append(list(row))

        worksheet = DummyWorksheet()

        async def fake_worksheet(sheet_id, tab_name, **kwargs):
            return worksheet

        async def fake_backoff(func, *args, **kwargs):
            return await func(*args, **kwargs)

        monkeypatch.setattr(shard_data.async_core, "aget_worksheet", fake_worksheet)
        monkeypatch.setattr(shard_data.async_core, "acall_with_backoff", fake_backoff)

        record = await store.load_record(99999, "Fresh User")

        assert record.row_number == 2
        assert worksheet.append_payloads, "append_row should be invoked for new records"
        assert worksheet.append_payloads[0][0] == "99999"
        assert record.mysteries_owned == 0
        assert record.remnants_owned == 0
        assert record.remnants_since_mythic == 0
        assert record.last_remnant_mythic_iso == ""
        assert record.last_remnant_mythic_depth == 0

    asyncio.run(runner())


def test_load_record_invalid_header(monkeypatch):
    async def runner():
        store = shard_data.ShardSheetStore()
        config = shard_data.ShardTrackerConfig(
            sheet_id="sheet-1", tab_name="ShardTracker", channel_id=123
        )
        store.get_config = AsyncMock(return_value=config)

        async def fake_values(sheet_id, tab_name, **kwargs):
            return [["discord_id", "unexpected"]]

        monkeypatch.setattr(shard_data.async_core, "afetch_values", fake_values)

        with pytest.raises(shard_data.ShardTrackerSheetError):
            await store.load_record(1, "Name")

    asyncio.run(runner())


def test_expected_headers_include_mystery_and_remnant_columns():
    assert shard_data.EXPECTED_HEADERS[-5:] == [
        "mysteries_owned",
        "remnants_owned",
        "remnants_since_mythic",
        "last_remnant_mythic_iso",
        "last_remnant_mythic_depth",
    ]


def test_save_record_writes_through_aa(monkeypatch):
    async def runner():
        store = shard_data.ShardSheetStore()
        config = shard_data.ShardTrackerConfig(sheet_id="sheet-1", tab_name="ShardTracker", channel_id=123)
        record = shard_data.ShardRecord(
            header=list(shard_data.EXPECTED_HEADERS),
            discord_id=1,
            username_snapshot="Tester",
            row_number=7,
        )

        class DummyWorksheet:
            def __init__(self) -> None:
                self.calls = []

            async def update(self, range_label, rows, value_input_option="RAW"):
                self.calls.append((range_label, rows, value_input_option))

        worksheet = DummyWorksheet()

        async def fake_worksheet(sheet_id, tab_name, **kwargs):
            return worksheet

        async def fake_backoff(func, *args, **kwargs):
            return await func(*args, **kwargs)

        monkeypatch.setattr(shard_data.async_core, "aget_worksheet", fake_worksheet)
        monkeypatch.setattr(shard_data.async_core, "acall_with_backoff", fake_backoff)

        await store.save_record(config, record)

        assert worksheet.calls[0][0] == "A7:AA7"
        assert len(worksheet.calls[0][1][0]) == len(shard_data.EXPECTED_HEADERS)

    asyncio.run(runner())


def test_share_config_uses_lowercase_sheet_keys(monkeypatch):
    async def runner():
        store = shard_data.ShardSheetStore()
        monkeypatch.setattr(
            shard_data,
            "runtime_config",
            _RuntimeConfigStub(
                {
                    "shard_share_default_voice": "standard",
                    "shard_share_random_copy_enabled": "TRUE",
                    "shard_share_stash_low_threshold": "5",
                    "shard_share_stash_flex_threshold": "100",
                    "shard_share_mercy_high_percent": "85",
                }
            ),
        )

        config = await store.get_share_config()

        assert config.default_voice == "standard"
        assert config.random_copy_enabled is True
        assert config.stash_low_threshold == 5
        assert config.stash_flex_threshold == 100
        assert config.mercy_high_percent == 85

    asyncio.run(runner())


def test_share_copy_and_voice_tabs_use_lowercase_config_keys(monkeypatch):
    async def runner():
        store = shard_data.ShardSheetStore()
        store.get_config = AsyncMock(return_value=shard_data.ShardTrackerConfig("sheet-1", "main", 123))
        monkeypatch.setattr(
            shard_data,
            "runtime_config",
            _RuntimeConfigStub(
                {
                    "shard_share_copy_tab": "ShardShareCopy",
                    "shard_share_voice_targets_tab": "ShardShareVoiceTargets",
                }
            ),
        )
        calls = []

        async def fake_fetch(sheet_id, tab_name):
            calls.append((sheet_id, tab_name))
            if tab_name == "ShardShareCopy":
                return [list(shard_data.SHARD_SHARE_COPY_REQUIRED_HEADERS)]
            return [list(shard_data.SHARD_SHARE_VOICE_TARGET_REQUIRED_HEADERS)]

        monkeypatch.setattr(shard_data.async_core, "afetch_values", fake_fetch)

        await store.get_share_copy_rows()
        await store.get_share_voice_target_rows()

        assert calls == [("sheet-1", "ShardShareCopy"), ("sheet-1", "ShardShareVoiceTargets")]

    asyncio.run(runner())


def test_share_config_missing_required_key_raises(monkeypatch):
    async def runner():
        store = shard_data.ShardSheetStore()
        monkeypatch.setattr(
            shard_data,
            "runtime_config",
            _RuntimeConfigStub(
                {
                    "shard_share_random_copy_enabled": "TRUE",
                    "shard_share_stash_low_threshold": "5",
                    "shard_share_stash_flex_threshold": "100",
                    "shard_share_mercy_high_percent": "85",
                }
            ),
        )

        with pytest.raises(shard_data.ShardTrackerConfigError, match="shard_share_default_voice"):
            await store.get_share_config()

    asyncio.run(runner())


@pytest.mark.parametrize("percent", [1, 85, 100])
def test_share_config_accepts_valid_mercy_high_percent_range(monkeypatch, percent):
    async def runner():
        store = shard_data.ShardSheetStore()
        monkeypatch.setattr(
            shard_data,
            "runtime_config",
            _RuntimeConfigStub(
                {
                    "shard_share_default_voice": "standard",
                    "shard_share_random_copy_enabled": "TRUE",
                    "shard_share_stash_low_threshold": "5",
                    "shard_share_stash_flex_threshold": "100",
                    "shard_share_mercy_high_percent": str(percent),
                }
            ),
        )

        config = await store.get_share_config()

        assert config.mercy_high_percent == percent

    asyncio.run(runner())


@pytest.mark.parametrize("percent", [0, 101])
def test_share_config_rejects_invalid_mercy_high_percent_range(monkeypatch, percent):
    async def runner():
        store = shard_data.ShardSheetStore()
        monkeypatch.setattr(
            shard_data,
            "runtime_config",
            _RuntimeConfigStub(
                {
                    "shard_share_default_voice": "standard",
                    "shard_share_random_copy_enabled": "TRUE",
                    "shard_share_stash_low_threshold": "5",
                    "shard_share_stash_flex_threshold": "100",
                    "shard_share_mercy_high_percent": str(percent),
                }
            ),
        )

        with pytest.raises(shard_data.ShardTrackerConfigError, match="invalid percent range: shard_share_mercy_high_percent"):
            await store.get_share_config()

    asyncio.run(runner())
