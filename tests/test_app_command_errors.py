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


class _Author:
    id = 42


class _Ctx:
    command = None
    author = _Author()
    guild = None

    def __init__(self, invoked_with="remind", content="!remind now"):
        self.invoked_with = invoked_with
        self.message = type("Message", (), {"content": content})()


class _Runtime:
    def __init__(self):
        self.messages = []

    async def send_log_message(self, message):
        self.messages.append(message)


def _help_row(app, bot_key, command="!remind", command_key="remind", usage="!remind me"):
    return app.help_commands.HelpCommandRow(
        bot_key=bot_key,
        command_key=command_key,
        command=command,
        usage=usage,
        category="Ops",
        access_level="user",
        summary="Summary",
        details="Details",
        sort_order=None,
        source_order=0,
    )


def _run_command_error(app, monkeypatch, rows, *, error=None, lookup_raises=False):
    runtime = _Runtime()
    monkeypatch.setattr(app, "runtime", runtime, raising=False)

    original_find_rows = app.help_commands.find_rows

    async def get_rows():
        return rows

    def find_rows(source_rows, query):
        if lookup_raises:
            raise RuntimeError("lookup failed")
        return original_find_rows(source_rows, query)

    monkeypatch.setattr(app.help_commands, "get_rows", get_rows)
    monkeypatch.setattr(app.help_commands, "find_rows", find_rows)
    err = error if error is not None else app.commands.CommandNotFound("not found")
    asyncio = __import__("asyncio")
    asyncio.run(app.on_command_error(_Ctx(), err))
    return runtime.messages


def test_external_reminder_command_not_found_is_ignored(monkeypatch):
    app = _import_app(monkeypatch)

    messages = _run_command_error(app, monkeypatch, (_help_row(app, "reminder"),))

    assert messages == []


def test_external_achievement_command_not_found_is_ignored(monkeypatch):
    app = _import_app(monkeypatch)

    messages = _run_command_error(app, monkeypatch, (_help_row(app, "achievements"),))

    assert messages == []


def test_commands_shared_only_by_external_bots_are_ignored(monkeypatch):
    app = _import_app(monkeypatch)

    messages = _run_command_error(
        app,
        monkeypatch,
        (_help_row(app, "reminder"), _help_row(app, "achievements")),
    )

    assert messages == []


def test_woadkeeper_owned_command_not_found_is_reported(monkeypatch):
    app = _import_app(monkeypatch)

    messages = _run_command_error(app, monkeypatch, (_help_row(app, "woadkeeper"),))

    assert len(messages) == 1


def test_mixed_ownership_command_not_found_is_reported(monkeypatch):
    app = _import_app(monkeypatch)

    messages = _run_command_error(
        app,
        monkeypatch,
        (_help_row(app, "reminder"), _help_row(app, "woadkeeper")),
    )

    assert len(messages) == 1


def test_blank_ownership_command_not_found_is_reported(monkeypatch):
    app = _import_app(monkeypatch)

    messages = _run_command_error(app, monkeypatch, (_help_row(app, ""),))

    assert len(messages) == 1


def test_unknown_command_not_found_is_reported(monkeypatch):
    app = _import_app(monkeypatch)

    messages = _run_command_error(app, monkeypatch, ())

    assert len(messages) == 1


def test_unavailable_help_data_reports_normally(monkeypatch):
    app = _import_app(monkeypatch)

    messages = _run_command_error(app, monkeypatch, None)

    assert len(messages) == 1


def test_help_lookup_exception_reports_normally(monkeypatch):
    app = _import_app(monkeypatch)

    messages = _run_command_error(app, monkeypatch, (_help_row(app, "reminder"),), lookup_raises=True)

    assert len(messages) == 1


def test_non_command_not_found_exception_reports_normally(monkeypatch):
    app = _import_app(monkeypatch)

    messages = _run_command_error(app, monkeypatch, (_help_row(app, "reminder"),), error=RuntimeError("boom"))

    assert len(messages) == 1
