import asyncio
import logging

from modules.housekeeping import cleanup


def test_resolve_cleanup_config_treats_false_as_not_dry_run(monkeypatch, caplog):
    calls = []
    values = {
        cleanup.CONFIG_TAB: "HousekeepingCleanUp",
        cleanup.CONFIG_RUN_EVERY_HOURS: "24",
        cleanup.CONFIG_DRY_RUN: "FALSE",
    }

    monkeypatch.setattr(
        cleanup.feature_flags,
        "status",
        lambda key: {
            "present": True,
            "enabled": True,
            "invalid": False,
            "source_tab": "FeatureToggles",
        },
    )

    def fake_get_config_value(key, default=None, *, force=False):
        calls.append((key, force))
        return values.get(key, default)

    monkeypatch.setattr(cleanup.recruitment, "get_config_value", fake_get_config_value)
    monkeypatch.setattr(cleanup.recruitment, "get_config_tab_name", lambda: "Config")

    logger = logging.getLogger("test.cleanup.config")
    with caplog.at_level(logging.INFO, logger="test.cleanup.config"):
        config = cleanup.resolve_cleanup_config(logger)

    assert config is not None
    assert config.tab_name == "HousekeepingCleanUp"
    assert config.run_every_hours == 24
    assert config.dry_run is False
    assert all(force is True for _, force in calls)
    assert (
        "cleanup config resolved: tab=HousekeepingCleanUp run_every_hours=24 dry_run=false delete_batch_size=25 delete_batch_pause_seconds=2.0 delete_per_message_pause_seconds=0.15 source=Config:Config"
        in caplog.text
    )


def test_resolve_cleanup_config_can_use_cached_values_when_requested(monkeypatch):
    values = {
        cleanup.CONFIG_TAB: "HousekeepingCleanUp",
        cleanup.CONFIG_RUN_EVERY_HOURS: "24",
        cleanup.CONFIG_DRY_RUN: "TRUE",
    }
    force_flags = []

    monkeypatch.setattr(
        cleanup.feature_flags,
        "status",
        lambda key: {"present": True, "enabled": True, "invalid": False},
    )

    def fake_get_config_value(key, default=None, *, force=False):
        force_flags.append(force)
        return values.get(key, default)

    monkeypatch.setattr(cleanup.recruitment, "get_config_value", fake_get_config_value)
    monkeypatch.setattr(cleanup.recruitment, "get_config_tab_name", lambda: "Config")

    config = cleanup.resolve_cleanup_config(force_refresh=False)

    assert config is not None
    assert config.dry_run is True
    assert force_flags == [False, False, False, False, False, False]


def test_resolve_cleanup_config_async_uses_async_config_lookup(monkeypatch, caplog):
    values = {
        cleanup.CONFIG_TAB: "HousekeepingCleanUp",
        cleanup.CONFIG_RUN_EVERY_HOURS: "24",
        cleanup.CONFIG_DRY_RUN: "FALSE",
    }
    calls = []

    monkeypatch.setattr(
        cleanup.feature_flags,
        "status",
        lambda key: {
            "present": True,
            "enabled": True,
            "invalid": False,
            "source_tab": "FeatureToggles",
        },
    )

    def fail_sync_config_lookup(*_args, **_kwargs):
        raise AssertionError("cleanup async config resolution must not use sync lookup")

    async def fake_get_config_value_async(key, default=None, *, force=False):
        calls.append((key, force))
        return values.get(key, default)

    monkeypatch.setattr(
        cleanup.recruitment, "get_config_value", fail_sync_config_lookup
    )
    monkeypatch.setattr(
        cleanup.recruitment, "get_config_value_async", fake_get_config_value_async
    )
    monkeypatch.setattr(cleanup.recruitment, "get_config_tab_name", lambda: "Config")

    logger = logging.getLogger("test.cleanup.config.async")
    with caplog.at_level(logging.INFO, logger="test.cleanup.config.async"):
        config = asyncio.run(cleanup.resolve_cleanup_config_async(logger))

    assert config is not None
    assert config.tab_name == "HousekeepingCleanUp"
    assert config.dry_run is False
    assert [key for key, _force in calls] == [
        cleanup.CONFIG_TAB,
        cleanup.CONFIG_RUN_EVERY_HOURS,
        cleanup.CONFIG_DRY_RUN,
        cleanup.CONFIG_DELETE_BATCH_SIZE,
        cleanup.CONFIG_DELETE_BATCH_PAUSE_SECONDS,
        cleanup.CONFIG_DELETE_PER_MESSAGE_PAUSE_SECONDS,
    ]
    assert "cleanup config resolve started" in caplog.text
    assert "cleanup config resolve succeeded: tab=HousekeepingCleanUp" in caplog.text
