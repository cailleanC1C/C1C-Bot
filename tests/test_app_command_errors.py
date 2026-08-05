from __future__ import annotations

import importlib
import sys


_REQUIRED_ENV = {
    "DISCORD_TOKEN": "test-token",
    "GSPREAD_CREDENTIALS": "{}",
    "RECRUITMENT_SHEET_ID": "sheet-id",
}


def _import_app(monkeypatch, **env):
    for key, value in _REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    for key in (
        "LOG_MESSAGE_CONTENT",
        "LOG_LEVEL",
        "BOT_VERSION",
        "PROMO_CHANNEL_ID",
        "WELCOME_CHANNEL_ID",
    ):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    for module_name in ["app", "shared.config"]:
        sys.modules.pop(module_name, None)

    import shared

    if hasattr(shared, "config"):
        delattr(shared, "config")
    return importlib.import_module("app")


def test_log_message_content_defaults_false(monkeypatch):
    app = _import_app(monkeypatch)

    assert app.shared_config.cfg.get("LOG_MESSAGE_CONTENT") is False
    assert app.LOG_MESSAGE_CONTENT is False


def test_log_message_content_keeps_true_false_parsing(monkeypatch):
    for raw in ["1", "true", "TRUE", "yes", "on"]:
        app = _import_app(monkeypatch, LOG_MESSAGE_CONTENT=raw)
        assert app.shared_config.cfg.get("LOG_MESSAGE_CONTENT") is True
        assert app.LOG_MESSAGE_CONTENT is True

    for raw in ["0", "false", "FALSE", "no", "off", "unexpected"]:
        app = _import_app(monkeypatch, LOG_MESSAGE_CONTENT=raw)
        assert app.shared_config.cfg.get("LOG_MESSAGE_CONTENT") is False
        assert app.LOG_MESSAGE_CONTENT is False


def test_log_level_and_bot_version_preserve_fallbacks(monkeypatch):
    app = _import_app(monkeypatch)

    assert app.shared_config.cfg.get("LOG_LEVEL") == "INFO"
    assert app.BOT_VERSION == "dev"


def test_log_level_and_bot_version_use_shared_config_values(monkeypatch):
    app = _import_app(monkeypatch, LOG_LEVEL="DEBUG", BOT_VERSION="v1.2.3")

    assert app.shared_config.cfg.get("LOG_LEVEL") == "DEBUG"
    assert app.BOT_VERSION == "v1.2.3"


def test_startup_summary_channel_formatting_for_configured_ids(monkeypatch):
    app = _import_app(
        monkeypatch,
        PROMO_CHANNEL_ID="12345",
        WELCOME_CHANNEL_ID="67890",
    )

    assert app._startup_channel_mention_id("PROMO_CHANNEL_ID") == "12345"
    assert app._startup_channel_raw_id("PROMO_CHANNEL_ID") == "12345"
    assert app._startup_channel_mention_id("WELCOME_CHANNEL_ID") == "67890"
    assert app._startup_channel_raw_id("WELCOME_CHANNEL_ID") == "67890"


def test_startup_summary_channel_formatting_for_missing_ids(monkeypatch):
    app = _import_app(monkeypatch)

    assert f"<#{app._startup_channel_mention_id('PROMO_CHANNEL_ID')}>" == "<#>"
    assert app._startup_channel_raw_id("PROMO_CHANNEL_ID") == "-"
    assert f"<#{app._startup_channel_mention_id('WELCOME_CHANNEL_ID')}>" == "<#>"
    assert app._startup_channel_raw_id("WELCOME_CHANNEL_ID") == "-"
