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
