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


def test_areload_config_uses_async_sheet_loaders(monkeypatch):
    _apply_required_env(monkeypatch)
    import shared.config as cfg

    monkeypatch.setattr(cfg, "_CONFIG", dict(cfg._CONFIG))

    def sync_forbidden(*_args, **_kwargs):
        raise AssertionError("sync Sheets loader called from async config reload")

    async def load_onboarding():
        return "onboarding-sheet", {"ONBOARDING_TAB": "Questions"}

    async def load_milestones():
        return "milestones-sheet", {"SHARD_MERCY_TAB": "Mercy"}

    monkeypatch.setattr(cfg, "_load_onboarding_config_values", sync_forbidden)
    monkeypatch.setattr(cfg, "_load_milestones_config_values", sync_forbidden)
    monkeypatch.setattr(cfg, "_aload_onboarding_config_values", load_onboarding)
    monkeypatch.setattr(cfg, "_aload_milestones_config_values", load_milestones)

    snapshot = asyncio.run(cfg.areload_config())

    assert snapshot["ONBOARDING_TAB"] == "Questions"
    assert snapshot["SHARD_MERCY_TAB"] == "Mercy"


@pytest.mark.parametrize("missing", sorted(_REQUIRED_ENV))
def test_reload_config_fails_when_required_missing(monkeypatch, missing):
    _apply_required_env(monkeypatch)
    monkeypatch.delenv(missing, raising=False)
    import shared.config as cfg
    with pytest.raises(RuntimeError):
        cfg.reload_config()
