import asyncio
import importlib

import pytest


_REQUIRED_ENV = {
    "DISCORD_TOKEN": "token",
    "GSPREAD_CREDENTIALS": "{}",
    "RECRUITMENT_SHEET_ID": "sheet",
}


def _apply_required_env(monkeypatch):
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)


def test_reload_config_happy_path(monkeypatch):
    _apply_required_env(monkeypatch)
    cfg = importlib.import_module("shared.config")
    importlib.reload(cfg)
    cfg.reload_config()


@pytest.mark.parametrize("missing", sorted(_REQUIRED_ENV))
def test_reload_config_fails_when_required_missing(monkeypatch, missing):
    _apply_required_env(monkeypatch)
    monkeypatch.delenv(missing, raising=False)
    import shared.config as cfg
    with pytest.raises(RuntimeError):
        cfg.reload_config()


def test_areload_config_uses_async_sheet_loaders(monkeypatch):
    _apply_required_env(monkeypatch)
    import shared.config as cfg

    async def onboarding_values():
        return "onboarding", {"ONBOARDING_TAB": "Async Onboarding"}

    async def milestone_values():
        return "milestones", {"SHARD_TRACKER_TAB": "Async Shards"}

    monkeypatch.setattr(cfg, "_aload_onboarding_config_values", onboarding_values)
    monkeypatch.setattr(cfg, "_aload_milestones_config_values", milestone_values)
    monkeypatch.setattr(
        cfg,
        "_merge_onboarding_tab",
        lambda _config: (_ for _ in ()).throw(AssertionError("sync onboarding read")),
    )
    monkeypatch.setattr(
        cfg,
        "_merge_milestones_tab",
        lambda _config: (_ for _ in ()).throw(AssertionError("sync milestones read")),
    )

    snapshot = asyncio.run(cfg.areload_config())

    assert snapshot["ONBOARDING_TAB"] == "Async Onboarding"
    assert snapshot["SHARD_TRACKER_TAB"] == "Async Shards"
