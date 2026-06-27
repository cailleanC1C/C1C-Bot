import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from discord.ext import commands
import shared.sheets.core as sheets_core

from modules.common import runtime
from modules.housekeeping import cleanup

REQUIRED_HEADERS = list(cleanup.REQUIRED_HEADERS)


def _toggle_status(*, present=True, enabled=False, invalid=False, invalid_value=None):
    return {"present": present, "enabled": enabled, "invalid": invalid, "invalid_value": invalid_value, "source_tab": "FeatureToggles"}


def test_missing_feature_toggle_prevents_config_read(monkeypatch, caplog):
    monkeypatch.setattr(cleanup.feature_flags, "status", lambda key: _toggle_status(present=False))
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda *_: (_ for _ in ()).throw(AssertionError("no config read")))
    assert cleanup.resolve_cleanup_config() is None
    assert "missing Feature Toggle HOUSEKEEPING_CLEANUP_ENABLED" in caplog.text


def test_false_feature_toggle_prevents_scheduling(monkeypatch, caplog):
    caplog.set_level("INFO", logger="c1c.housekeeping.cleanup")
    monkeypatch.setattr(cleanup.feature_flags, "status", lambda key: _toggle_status(enabled=False))
    assert cleanup.resolve_cleanup_config() is None
    assert "cleanup disabled by Feature Toggle HOUSEKEEPING_CLEANUP_ENABLED=FALSE" in caplog.text


def test_invalid_feature_toggle_prevents_scheduling(monkeypatch, caplog):
    monkeypatch.setattr(cleanup.feature_flags, "status", lambda key: _toggle_status(invalid=True, invalid_value="maybe"))
    assert cleanup.resolve_cleanup_config() is None
    assert "required Feature Toggle HOUSEKEEPING_CLEANUP_ENABLED has invalid value" in caplog.text


def test_enabled_feature_toggle_reads_config_not_toggle_from_config(monkeypatch):
    requested_toggles = []
    config_keys = []
    values = {
        cleanup.CONFIG_ENABLED: "FALSE",
        cleanup.CONFIG_TAB: "CleanupRows",
        cleanup.CONFIG_RUN_EVERY_HOURS: "6",
        cleanup.CONFIG_DRY_RUN: "TRUE",
    }
    monkeypatch.setattr(cleanup.feature_flags, "status", lambda key: requested_toggles.append(key) or _toggle_status(enabled=True))
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None, **_kwargs: config_keys.append(key) or values.get(key, default))
    cfg = cleanup.resolve_cleanup_config()
    assert requested_toggles == [cleanup.CONFIG_ENABLED]
    assert cleanup.CONFIG_ENABLED not in config_keys
    assert cfg and cfg.tab_name == "CleanupRows" and cfg.run_every_hours == 6 and cfg.dry_run is True


def test_enabled_feature_toggle_missing_config_prevents_scheduling(monkeypatch):
    monkeypatch.setattr(cleanup.feature_flags, "status", lambda key: _toggle_status(enabled=True))
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None, **_kwargs: default)
    assert cleanup.resolve_cleanup_config() is None




def test_aresolve_cleanup_config_awaits_inside_active_loop(monkeypatch):
    monkeypatch.setattr(cleanup.feature_flags, "status", lambda key: _toggle_status(enabled=True))
    monkeypatch.setattr(cleanup.recruitment, "get_config_tab_name", lambda: "Config")
    monkeypatch.setattr(cleanup.recruitment, "get_recruitment_sheet_id", lambda: "sheet")

    async def fake_afetch_records(sheet_id, worksheet, **_kwargs):
        assert (sheet_id, worksheet) == ("sheet", "Config")
        return [
            {"Key": cleanup.CONFIG_TAB, "Value": "CleanupRows"},
            {"Key": cleanup.CONFIG_RUN_EVERY_HOURS, "Value": "6"},
            {"Key": cleanup.CONFIG_DRY_RUN, "Value": "TRUE"},
        ]

    monkeypatch.setattr(cleanup.async_core, "afetch_records", fake_afetch_records)
    cfg = asyncio.run(cleanup.aresolve_cleanup_config())
    assert cfg == cleanup.CleanupConfig(True, "CleanupRows", 6, True, source="Config:Config")


def test_aresolve_cleanup_config_does_not_call_sync_config_lookup(monkeypatch):
    monkeypatch.setattr(cleanup.feature_flags, "status", lambda key: _toggle_status(enabled=True))
    monkeypatch.setattr(cleanup.recruitment, "get_config_tab_name", lambda: "Config")
    monkeypatch.setattr(cleanup.recruitment, "get_recruitment_sheet_id", lambda: "sheet")
    monkeypatch.setattr(
        cleanup.recruitment,
        "get_config_value",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("sync config lookup must not be used")),
    )

    async def fake_afetch_records(*_args, **_kwargs):
        return [
            {"key": cleanup.CONFIG_TAB, "value": "CleanupRows"},
            {"key": cleanup.CONFIG_RUN_EVERY_HOURS, "value": "1"},
            {"key": cleanup.CONFIG_DRY_RUN, "value": "false"},
        ]

    monkeypatch.setattr(cleanup.async_core, "afetch_records", fake_afetch_records)
    cfg = asyncio.run(cleanup.aresolve_cleanup_config())
    assert cfg and cfg.tab_name == "CleanupRows" and cfg.dry_run is False

def test_header_lookup_requires_headers_without_column_position_fallback():
    header_map = cleanup.build_header_map(list(reversed(REQUIRED_HEADERS)))
    assert header_map["enabled"] == len(REQUIRED_HEADERS) - 1
    assert header_map["notes"] == 0


def test_row_update_only_writes_bot_owned_columns():
    header_map = cleanup.build_header_map(REQUIRED_HEADERS)
    row = cleanup.CleanupRow(sheet_row=2, values={})
    updates = cleanup._row_update(row, header_map, {"enabled": "FALSE", "target_id": "1", "notes": "x", "target_type": "thread", "last_status": "ok"})
    assert updates == {"C2": "thread", "L2": "ok"}


class Worksheet:
    def __init__(self, values):
        self.values = values
        self.updates = []
    def get_all_values(self):
        return self.values
    def batch_update(self, updates):
        self.updates.extend(updates)


def update_map(ws):
    return {item["range"]: item["values"][0][0] for item in ws.updates}


class Msg:
    def __init__(
        self,
        content,
        *,
        author_id=10,
        pinned=False,
        hours_old=10,
        author_bot=False,
        roles=None,
        webhook_id=None,
        message_type=None,
        application_id=None,
        system_content=None,
        automod_rule_id=None,
    ):
        self.content = content
        self.system_content = system_content if system_content is not None else content
        self.author = type("Author", (), {"id": author_id, "bot": author_bot, "roles": roles})()
        self.pinned = pinned
        self.created_at = datetime.now(timezone.utc) - timedelta(hours=hours_old)
        self.channel = type("Channel", (), {"id": 123})()
        self.deleted = False
        self.webhook_id = webhook_id
        self.type = message_type
        self.application_id = application_id
        self.automod_rule_id = automod_rule_id
    async def delete(self, reason=None):
        self.deleted = True


class PartialMsgNoReason(Msg):
    async def delete(self):
        self.deleted = True


class MsgWithReason(Msg):
    def __init__(self, content, *, author_id=10, pinned=False, hours_old=10):
        super().__init__(content, author_id=author_id, pinned=pinned, hours_old=hours_old)
        self.delete_reasons = []

    async def delete(self, reason=None):
        self.delete_reasons.append(reason)
        self.deleted = True


class MsgUnexpectedTypeError(Msg):
    async def delete(self, reason=None):
        raise TypeError("network adapter exploded")


class HistoryTarget:
    id = 123
    name = "target-name"
    parent = None
    def __init__(self, messages=None):
        self.messages = messages or []
        self.history_calls = 0
    async def history(self, limit=None, oldest_first=True):
        self.history_calls += 1
        for msg in self.messages:
            yield msg


class Thread(HistoryTarget):
    name = "thread-name"
    parent = type("Parent", (), {"name": "parent-name"})()


class Channel(HistoryTarget):
    name = "channel-name"


class UnsupportedTarget:
    id = 123
    name = "voice-name"




def patch_async_cleanup_config(monkeypatch, cfg):
    async def fake_aresolve_cleanup_config(logger=None, *, force_refresh=False):
        return cfg
    monkeypatch.setattr(cleanup, "aresolve_cleanup_config", fake_aresolve_cleanup_config)


class Bot:
    command_prefix = "!"
    user = type("User", (), {"id": 99})()
    def __init__(self, target=None):
        self.target = target
    def get_channel(self, target_id):
        return self.target
    async def fetch_channel(self, target_id):
        return self.target


def run_with_sheet(monkeypatch, rows, messages, *, dry_run=True, startup_validation=False, target_type="thread", target=None):
    ws = Worksheet([REQUIRED_HEADERS, *rows])
    target = target or (Channel(messages) if target_type == "channel" else Thread(messages))
    cfg = cleanup.CleanupConfig(True, "CleanupRows", 6, dry_run)
    async def fake_sheet(*_): return ws
    async def fake_log(*_): return None
    patch_async_cleanup_config(monkeypatch, cfg)
    monkeypatch.setattr(cleanup.recruitment, "get_recruitment_sheet_id", lambda: "sheet")
    monkeypatch.setattr(cleanup.async_core, "aget_worksheet", fake_sheet)
    monkeypatch.setattr(cleanup.runtime_helpers, "send_log_message", fake_log)
    async def fake_resolve(_bot, target_id):
        if target_type == "unsupported":
            return (target, None, "invalid_target_type")
        return (target, target_type, None)
    monkeypatch.setattr(cleanup, "_resolve_any", fake_resolve)
    asyncio.run(cleanup.run_cleanup(Bot(target), startup_validation=startup_validation))
    return ws, messages, target


def active_row(**overrides):
    data = {"enabled":"TRUE","target_id":"123","target_type":"","target_name":"","parent_name":"","cleanup_mode":"bot_messages_only","min_age_hours":"1","last_checked_at_utc":"","last_deleted_count":"","last_candidate_count":"","last_skipped_count":"","last_status":"","notes":"admin"}
    data.update(overrides)
    return [data[h] for h in REQUIRED_HEADERS]


def test_run_cleanup_uses_async_safe_sheets_helper_for_read_and_write(monkeypatch):
    ws = Worksheet([REQUIRED_HEADERS, active_row()])
    target = Thread([Msg("bot", author_id=99)])
    cfg = cleanup.CleanupConfig(True, "CleanupRows", 6, False)
    calls = []

    async def fake_sheet(*_):
        return ws

    async def fake_log(*_):
        return None

    async def fake_resolve(_bot, _target_id):
        return target, "thread", None

    async def fake_to_thread_with_backoff(func, *args, **kwargs):
        calls.append(func.__name__)
        return func(*args, **kwargs)

    async def fail_acall_with_backoff(*_args, **_kwargs):
        raise AssertionError("cleanup must use a_to_thread_with_backoff")

    patch_async_cleanup_config(monkeypatch, cfg)
    monkeypatch.setattr(cleanup.recruitment, "get_recruitment_sheet_id", lambda: "sheet")
    monkeypatch.setattr(cleanup.async_core, "aget_worksheet", fake_sheet)
    monkeypatch.setattr(cleanup.async_core, "a_to_thread_with_backoff", fake_to_thread_with_backoff)
    monkeypatch.setattr(cleanup.async_core, "acall_with_backoff", fail_acall_with_backoff)
    monkeypatch.setattr(cleanup.runtime_helpers, "send_log_message", fake_log)
    monkeypatch.setattr(cleanup, "_resolve_any", fake_resolve)

    summary = asyncio.run(cleanup.run_cleanup(Bot(target)))

    assert summary.status == "ok"
    assert calls == ["get_all_values", "batch_update"]
    assert update_map(ws)["L2"] == "deleted"


def test_run_cleanup_does_not_raise_active_loop_retry_runtime_error(monkeypatch):
    ws = Worksheet([REQUIRED_HEADERS, active_row()])
    target = Thread([Msg("bot", author_id=99)])
    cfg = cleanup.CleanupConfig(True, "CleanupRows", 6, False)

    async def fake_sheet(*_):
        return ws

    async def fake_log(*_):
        return None

    async def fake_resolve(_bot, _target_id):
        return target, "thread", None

    async def fake_arun(func, *args, **kwargs):
        return func(*args, **kwargs)

    def fail_sync_retry(*_args, **_kwargs):
        raise RuntimeError("_retry_with_backoff must not run inside an active event loop; use the async variant")

    patch_async_cleanup_config(monkeypatch, cfg)
    monkeypatch.setattr(cleanup.recruitment, "get_recruitment_sheet_id", lambda: "sheet")
    monkeypatch.setattr(cleanup.async_core, "aget_worksheet", fake_sheet)
    monkeypatch.setattr(cleanup.runtime_helpers, "send_log_message", fake_log)
    monkeypatch.setattr(cleanup, "_resolve_any", fake_resolve)
    monkeypatch.setattr(sheets_core.async_adapter, "arun", fake_arun)
    monkeypatch.setattr(sheets_core, "_retry_with_backoff", fail_sync_retry)

    summary = asyncio.run(cleanup.run_cleanup(Bot(target)))

    assert summary.status == "ok"
    assert update_map(ws)["L2"] == "deleted"




def test_run_cleanup_reaches_scan_for_automod_system_mode(monkeypatch):
    automod_type = getattr(cleanup.discord.MessageType, "auto_moderation_action")
    msgs = [Msg("", author_id=1, message_type=automod_type)]
    ws, msgs, _target = run_with_sheet(
        monkeypatch,
        [active_row(cleanup_mode="automod_system_messages_only", min_age_hours="0")],
        msgs,
        dry_run=False,
    )

    updates = update_map(ws)
    assert msgs[0].deleted is True
    assert updates["J2"] == "1"
    assert updates["L2"] == "deleted"

def test_blank_target_type_resolved_and_dry_run_deletes_nothing(monkeypatch):
    ws, msgs, _target = run_with_sheet(monkeypatch, [active_row()], [Msg("bot", author_id=99)])
    updates = update_map(ws)
    assert updates["C2"] == "thread"
    assert updates["D2"] == "thread-name"
    assert updates["E2"] == "parent-name"
    assert updates["I2"] == "0"
    assert updates["J2"] == "1"
    assert updates["K2"] == "0"
    assert updates["L2"] == "dry_run_ok"
    assert not msgs[0].deleted


def test_invalid_rows_write_statuses(monkeypatch):
    rows = [active_row(target_id="abc"), active_row(cleanup_mode="bad"), active_row(min_age_hours="bad")]
    ws, _, _target = run_with_sheet(monkeypatch, rows, [])
    updates = update_map(ws)
    assert updates["L2"] == "invalid_target_id"
    assert updates["L3"] == "invalid_cleanup_mode"
    assert updates["L4"] == "invalid_min_age_hours"


def test_cleanup_modes_and_min_age_respected(monkeypatch):
    msgs = [Msg("human", author_id=1), Msg("!cmd", author_id=1), Msg("bot", author_id=99), Msg("pinned", author_id=99, pinned=True), Msg("young", author_id=99, hours_old=0)]
    ws, msgs, _target = run_with_sheet(monkeypatch, [active_row(cleanup_mode="bot_messages_and_commands", min_age_hours="1")], msgs, dry_run=False)
    assert [m.deleted for m in msgs] == [False, True, True, False, False]
    updates = update_map(ws)
    assert updates["I2"] == "2"
    assert updates["J2"] == "2"
    assert updates["K2"] == "3"
    assert updates["L2"] == "deleted"


def test_delete_messages_retries_partial_message_without_reason():
    msg = PartialMsgNoReason("bot", author_id=99)

    result = asyncio.run(cleanup._delete_messages([msg], cleanup.log))

    assert result.status == "deleted"
    assert result.deleted == 1
    assert result.errors == 0
    assert msg.deleted is True


def test_delete_messages_still_passes_reason_when_supported():
    msg = MsgWithReason("bot", author_id=99)

    result = asyncio.run(cleanup._delete_messages([msg], cleanup.log))

    assert result.status == "deleted"
    assert result.deleted == 1
    assert msg.delete_reasons == ["housekeeping cleanup"]


def test_delete_messages_counts_unexpected_type_error_and_continues(caplog):
    bad = MsgUnexpectedTypeError("bad", author_id=99)
    good = Msg("good", author_id=99)

    result = asyncio.run(cleanup._delete_messages([bad, good], cleanup.log))

    assert result.status == "partial_delete_failed"
    assert result.deleted == 1
    assert result.errors == 1
    assert good.deleted is True
    assert "cleanup delete failed unexpectedly" in caplog.text


def test_all_non_pinned_skips_pinned(monkeypatch):
    msgs = [Msg("human", author_id=1), Msg("pinned", author_id=1, pinned=True)]
    run_with_sheet(monkeypatch, [active_row(cleanup_mode="all_non_pinned", min_age_hours="0")], msgs, dry_run=False)
    assert [m.deleted for m in msgs] == [True, False]


def test_bot_messages_only_matches_own_bot_messages(monkeypatch):
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None, **_kwargs: default)
    assert cleanup._matches_mode(Msg("bot", author_id=99), "bot_messages_only", Bot()) is True


def test_bot_messages_only_matches_other_discord_bot_accounts(monkeypatch):
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None, **_kwargs: default)
    assert cleanup._matches_mode(Msg("bot", author_id=42, author_bot=True), "bot_messages_only", Bot()) is True


def test_bot_messages_only_matches_configured_common_bot_role(monkeypatch):
    role = type("Role", (), {"id": 12345})()
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None, **_kwargs: "12345" if key == cleanup.CONFIG_BOT_ROLE_IDS else default)
    assert cleanup._matches_mode(Msg("role bot", author_id=42, roles=[role]), "bot_messages_only", Bot()) is True


def test_bot_messages_only_rejects_non_bot_user_without_role(monkeypatch):
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None, **_kwargs: default)
    assert cleanup._matches_mode(Msg("human", author_id=1, author_bot=False, roles=[]), "bot_messages_only", Bot()) is False


def test_bot_messages_only_does_not_match_webhook_messages(monkeypatch):
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None, **_kwargs: default)
    assert cleanup._matches_mode(Msg("", author_id=1, webhook_id=555), "bot_messages_only", Bot()) is False


def test_webhook_inclusive_modes_match_webhook_messages(monkeypatch):
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None, **_kwargs: default)
    assert cleanup._matches_mode(Msg("", author_id=1, webhook_id=555), "bot_and_webhook_messages_only", Bot()) is True
    assert cleanup._matches_mode(Msg("", author_id=1, webhook_id=555), "bot_webhook_messages_and_commands", Bot()) is True
    assert cleanup._matches_mode(Msg("!cmd", author_id=1), "bot_webhook_messages_and_commands", Bot()) is True




def test_automod_system_messages_only_matches_auto_moderation_enum(monkeypatch):
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None, **_kwargs: default)
    automod_type = getattr(cleanup.discord.MessageType, "auto_moderation_action")
    assert cleanup._matches_mode(Msg("", author_id=1, message_type=automod_type), "automod_system_messages_only", Bot()) is True


def test_automod_system_messages_only_uses_conservative_fallbacks(monkeypatch):
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None, **_kwargs: default)
    generic_system_type = cleanup.discord.MessageType.pins_add
    assert cleanup._matches_mode(Msg("", author_id=1, message_type=generic_system_type, automod_rule_id=123), "automod_system_messages_only", Bot()) is True
    assert cleanup._matches_mode(Msg("AutoMod blocked a message", author_id=1, message_type=generic_system_type), "automod_system_messages_only", Bot()) is True


def test_automod_system_messages_only_rejects_non_automod_sources(monkeypatch):
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None, **_kwargs: default)
    automod_type = getattr(cleanup.discord.MessageType, "auto_moderation_action")
    generic_system_type = cleanup.discord.MessageType.pins_add
    assert cleanup._matches_mode(Msg("human", author_id=1), "automod_system_messages_only", Bot()) is False
    assert cleanup._matches_mode(Msg("bot", author_id=42, author_bot=True), "automod_system_messages_only", Bot()) is False
    assert cleanup._matches_mode(Msg("", author_id=1, webhook_id=555), "automod_system_messages_only", Bot()) is False
    assert cleanup._matches_mode(Msg("", author_id=1, message_type=generic_system_type), "automod_system_messages_only", Bot()) is False
    assert cleanup._matches_mode(Msg("", author_id=1, pinned=True, message_type=automod_type), "automod_system_messages_only", Bot()) is False


def test_automod_system_and_webhook_combined_mode_includes_webhooks(monkeypatch):
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None, **_kwargs: default)
    assert cleanup._matches_mode(Msg("", author_id=1, webhook_id=555), "automod_system_and_webhook_messages_only", Bot()) is True

def test_commands_only_uses_project_prefix_fallback_not_hardcoded(monkeypatch):
    bot = Bot()
    bot.command_prefix = None
    monkeypatch.setattr(cleanup, "get_command_prefix", lambda default="!": "?")
    assert cleanup._matches_mode(Msg("?cleanup run", author_id=1), "commands_only", bot) is True
    assert cleanup._matches_mode(Msg("!cleanup run", author_id=1), "commands_only", bot) is False


def test_combined_mode_uses_bot_or_command_helpers(monkeypatch):
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None, **_kwargs: default)
    assert cleanup._matches_mode(Msg("bot", author_id=99), "bot_messages_and_commands", Bot()) is True
    assert cleanup._matches_mode(Msg("!cmd", author_id=1), "bot_messages_and_commands", Bot()) is True
    assert cleanup._matches_mode(Msg("normal", author_id=1), "bot_messages_and_commands", Bot()) is False


def test_pinned_bot_and_command_messages_do_not_match_for_deletion(monkeypatch):
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None, **_kwargs: default)
    assert cleanup._matches_mode(Msg("bot", author_id=99, pinned=True), "bot_messages_only", Bot()) is False
    assert cleanup._matches_mode(Msg("!cmd", author_id=1, pinned=True), "commands_only", Bot()) is False
    assert cleanup._matches_mode(Msg("", author_id=1, pinned=True, webhook_id=555), "bot_and_webhook_messages_only", Bot()) is False


def test_zero_match_diagnostics_counts_webhooks_empty_content_and_bounded_authors(monkeypatch, caplog):
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None, **_kwargs: default)
    messages = [
        Msg("", author_id=1, webhook_id=101),
        Msg("", author_id=1, webhook_id=102),
        Msg("plain", author_id=2),
        Msg("", author_id=3),
        Msg("plain", author_id=4),
        Msg("plain", author_id=5),
        Msg("plain", author_id=6),
        Msg("plain", author_id=7),
    ]
    caplog.set_level("INFO", logger="c1c.housekeeping.cleanup")

    result = asyncio.run(
        cleanup._scan_message_history(
            Thread(messages),
            min_age_hours=0,
            mode="bot_messages_only",
            dry_run=True,
            bot=Bot(),
            logger=cleanup.log,
            context={"row": 5},
        )
    )

    assert result.candidates == 0
    record = next(record for record in caplog.records if record.message.startswith("cleanup row scan complete"))
    assert "webhook_message_count=2" in record.message
    assert "message_type_counts=none:8" in record.message
    assert "empty_content_count=3" in record.message
    assert "messages_with_content_count=5" in record.message
    assert "unique_author_count=7" in record.message
    sample = record.message.split("top_author_sample=", 1)[1]
    assert sample.count(",") == 4
    assert sample.startswith("1:2:false:2")




def test_zero_match_diagnostics_counts_system_automod_and_message_types(monkeypatch, caplog):
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None, **_kwargs: default)
    automod_type = getattr(cleanup.discord.MessageType, "auto_moderation_action")
    messages = [
        Msg("", author_id=1, message_type=automod_type, pinned=True),
        Msg("", author_id=2, message_type=cleanup.discord.MessageType.pins_add),
        Msg("normal", author_id=3),
    ]
    caplog.set_level("INFO", logger="c1c.housekeeping.cleanup")

    result = asyncio.run(
        cleanup._scan_message_history(
            Thread(messages),
            min_age_hours=0,
            mode="automod_system_messages_only",
            dry_run=True,
            bot=Bot(),
            logger=cleanup.log,
            context={"row": 5},
        )
    )

    assert result.candidates == 0
    record = next(record for record in caplog.records if record.message.startswith("cleanup row scan complete"))
    assert "system_message_count=2" in record.message
    assert "automod_system_seen=1" in record.message
    assert "message_type_counts=" in record.message
    assert "auto_moderation_action:1" in record.message
    assert "pins_add:1" in record.message

def test_zero_match_diagnostics_counts_prefix_and_role_matches(monkeypatch, caplog):
    role = type("Role", (), {"id": 12345})()
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None, **_kwargs: "12345" if key == cleanup.CONFIG_BOT_ROLE_IDS else default)
    caplog.set_level("INFO", logger="c1c.housekeeping.cleanup")

    asyncio.run(
        cleanup._scan_message_history(
            Thread([Msg("?cmd", author_id=1), Msg("role", author_id=2, roles=[role])]),
            min_age_hours=0,
            mode="commands_only",
            dry_run=True,
            bot=Bot(),
            logger=cleanup.log,
            context={"row": 5},
        )
    )

    record = next(record for record in caplog.records if record.message.startswith("cleanup row scan complete"))
    assert "prefix_matched_count=0" in record.message
    assert "prefix_count=1" in record.message
    assert "prefix_values_used=!" in record.message
    assert "role_matched_count=1" in record.message


def test_startup_validation_writeback_does_not_delete_when_dry_run_false(monkeypatch):
    msgs = [Msg("bot", author_id=99)]
    ws, _, _target = run_with_sheet(monkeypatch, [active_row()], msgs, dry_run=False, startup_validation=True)
    updates = update_map(ws)
    assert updates["C2"] == "thread"
    assert updates["H2"]
    assert updates["J2"] == "1"
    assert updates["K2"] == "0"
    assert updates["L2"] == "dry_run_ok"
    assert not msgs[0].deleted


def test_dry_run_writes_candidate_count_separately_from_skipped(monkeypatch):
    msgs = [Msg("bot", author_id=99), Msg("human", author_id=1)]
    ws, msgs, _target = run_with_sheet(monkeypatch, [active_row()], msgs, dry_run=True)
    updates = update_map(ws)
    assert updates["I2"] == "0"
    assert updates["J2"] == "1"
    assert updates["K2"] == "1"
    assert updates["L2"] == "dry_run_ok"
    assert [m.deleted for m in msgs] == [False, False]


def test_thread_cleanup_scans_configured_thread_only(monkeypatch):
    thread_msgs = [Msg("bot", author_id=99)]
    sibling_thread = Thread([Msg("bot", author_id=99)])
    parent = Channel([])
    parent.threads = [sibling_thread]
    thread = Thread(thread_msgs)
    thread.parent = parent

    ws, msgs, target = run_with_sheet(
        monkeypatch,
        [active_row(target_type="thread", cleanup_mode="bot_messages_only")],
        thread_msgs,
        dry_run=False,
        target_type="thread",
        target=thread,
    )

    updates = update_map(ws)
    assert target.history_calls == 1
    assert sibling_thread.history_calls == 0
    assert msgs[0].deleted is True
    assert sibling_thread.messages[0].deleted is False
    assert updates["C2"] == "thread"
    assert updates["L2"] == "deleted"

def test_channel_target_type_scans_channel_own_messages_not_child_threads(monkeypatch):
    channel_msgs = [Msg("bot", author_id=99)]
    thread = Thread([Msg("bot", author_id=99)])
    channel = Channel(channel_msgs)
    channel.threads = [thread]

    ws, msgs, target = run_with_sheet(
        monkeypatch,
        [active_row(target_type="channel", cleanup_mode="bot_messages_only")],
        channel_msgs,
        dry_run=False,
        target_type="channel",
        target=channel,
    )

    updates = update_map(ws)
    assert target.history_calls == 1
    assert thread.history_calls == 0
    assert msgs[0].deleted is True
    assert thread.messages[0].deleted is False
    assert updates["C2"] == "channel"
    assert updates["D2"] == "channel-name"
    assert updates["E2"] == ""
    assert updates["I2"] == "1"
    assert updates["J2"] == "1"
    assert updates["L2"] == "deleted"


def test_blank_target_type_resolving_to_channel_writes_channel(monkeypatch):
    ws, _msgs, _target = run_with_sheet(monkeypatch, [active_row(target_type="")], [Msg("bot", author_id=99)], target_type="channel")
    updates = update_map(ws)
    assert updates["C2"] == "channel"
    assert updates["D2"] == "channel-name"
    assert updates["E2"] == ""
    assert updates["L2"] == "dry_run_ok"


def test_channel_dry_run_counts_candidates_and_deletes_nothing(monkeypatch):
    msgs = [Msg("bot", author_id=99), Msg("human", author_id=1)]
    ws, msgs, _target = run_with_sheet(monkeypatch, [active_row(target_type="channel")], msgs, target_type="channel")
    updates = update_map(ws)
    assert [m.deleted for m in msgs] == [False, False]
    assert updates["I2"] == "0"
    assert updates["J2"] == "1"
    assert updates["K2"] == "1"
    assert updates["L2"] == "dry_run_ok"


def test_channel_cleanup_modes_match_thread_modes(monkeypatch):
    cases = [
        ("all_non_pinned", [Msg("human", author_id=1), Msg("pinned", author_id=99, pinned=True)], [True, False]),
        ("bot_messages_only", [Msg("human", author_id=1), Msg("bot", author_id=99)], [False, True]),
        ("commands_only", [Msg("normal", author_id=1), Msg("!cmd", author_id=1)], [False, True]),
        ("bot_messages_and_commands", [Msg("normal", author_id=1), Msg("!cmd", author_id=1), Msg("bot", author_id=99)], [False, True, True]),
    ]
    for mode, msgs, expected in cases:
        run_with_sheet(monkeypatch, [active_row(target_type="channel", cleanup_mode=mode, min_age_hours="0")], msgs, dry_run=False, target_type="channel")
        assert [m.deleted for m in msgs] == expected


def test_channel_like_history_target_resolves_as_channel_without_child_traversal(monkeypatch):
    class ChannelLikeTarget(HistoryTarget):
        name = "channel-like-name"

    child_thread = Thread([Msg("bot", author_id=99)])
    target = ChannelLikeTarget([Msg("bot", author_id=99)])
    target.threads = [child_thread]
    bot = Bot(target)
    monkeypatch.setattr(cleanup.discord, "Thread", Thread)
    monkeypatch.setattr(cleanup.discord, "TextChannel", Channel)

    resolved, detected_type, status = asyncio.run(cleanup._resolve_any(bot, 123))
    result = asyncio.run(
        cleanup._scan_channel(
            resolved,
            min_age_hours=0,
            mode="bot_messages_only",
            dry_run=False,
            bot=bot,
            logger=cleanup.log,
        )
    )

    assert resolved is target
    assert detected_type == "channel"
    assert status is None
    assert result.status == "deleted"
    assert target.history_calls == 1
    assert child_thread.history_calls == 0
    assert target.messages[0].deleted is True
    assert child_thread.messages[0].deleted is False


def test_unsupported_target_type_still_applies_to_unsupported_resolved_objects(monkeypatch):
    ws, _msgs, _target = run_with_sheet(monkeypatch, [active_row(target_type="")], [], target_type="unsupported", target=UnsupportedTarget())
    updates = update_map(ws)
    assert updates["L2"] == "invalid_target_type"


def test_cleanup_headers_and_config_keys_unchanged():
    assert REQUIRED_HEADERS == [
        "enabled", "target_id", "target_type", "target_name", "parent_name", "cleanup_mode", "min_age_hours",
        "last_checked_at_utc", "last_deleted_count", "last_candidate_count", "last_skipped_count", "last_status", "notes",
    ]
    assert cleanup.REQUIRED_CONFIG_KEYS == (cleanup.CONFIG_TAB, cleanup.CONFIG_RUN_EVERY_HOURS, cleanup.CONFIG_DRY_RUN)


def test_cleanup_config_does_not_fall_back_to_legacy_env(monkeypatch):
    monkeypatch.setenv("CLEANUP_THREAD_IDS", "123")
    monkeypatch.setenv("CLEANUP_INTERVAL_HOURS", "1")
    monkeypatch.setattr(cleanup.feature_flags, "status", lambda key: _toggle_status(enabled=True))
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None, **_kwargs: default)
    assert cleanup.resolve_cleanup_config() is None


class _FakeJob:
    def __init__(self):
        self.runner = None
        self.next_run = datetime(2026, 6, 26, 12, 0, tzinfo=timezone.utc)
    def do(self, runner):
        self.runner = runner


class _FakeScheduler:
    def __init__(self):
        self.calls = []
        self.jobs = []
        self.spawned = []
    def every(self, **kwargs):
        self.calls.append(kwargs)
        job = _FakeJob()
        self.jobs.append(job)
        return job
    def spawn(self, coro, *, name=None):
        self.spawned.append({"coro": coro, "name": name})
        return SimpleNamespace(name=name)


class _FakeLoop:
    def __init__(self):
        self.created = []
    def create_task(self, coro):
        self.created.append(coro)
        coro.close()


def test_global_housekeeping_disabled_prevents_cleanup_resolution_and_startup_validation(monkeypatch, caplog):
    caplog.set_level("INFO", logger="c1c.housekeeping.cleanup")
    rt = object.__new__(runtime.Runtime)
    rt.scheduler = _FakeScheduler()
    rt.bot = type("Bot", (), {"loop": _FakeLoop()})()
    cleanup_module = SimpleNamespace(
        resolve_cleanup_config=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("cleanup config must not resolve")),
        run_cleanup=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("startup validation must not run")),
    )

    successes = []
    rt._register_cleanup_scheduler(
        toggles=type("Toggles", (), {"housekeeping_enabled": False})(),
        successes=successes,
        housekeeping_cleanup=cleanup_module,
    )

    assert successes == []
    assert rt.scheduler.calls == []
    assert rt.bot.loop.created == []
    assert rt.scheduler.spawned == []
    assert "housekeeping cleanup disabled via global housekeeping feature toggle" in caplog.text


def test_cleanup_schedules_only_when_global_and_cleanup_toggles_enabled(monkeypatch, caplog):
    caplog.set_level("INFO", logger="c1c.housekeeping.cleanup")
    rt = object.__new__(runtime.Runtime)
    rt.scheduler = _FakeScheduler()
    rt.bot = type("Bot", (), {"loop": _FakeLoop()})()
    calls = {"resolved": 0}
    run_cleanup_calls = []
    sent_logs = []

    async def fake_send_log(message):
        sent_logs.append(message)

    monkeypatch.setattr(runtime, "send_log_message", fake_send_log)

    async def fake_run_cleanup(*args, **kwargs):
        run_cleanup_calls.append((args, kwargs))
        return None

    def fake_resolve(_logger=None):
        calls["resolved"] += 1
        return cleanup.CleanupConfig(True, "CleanupRows", 6, True)

    cleanup_module = SimpleNamespace(
        resolve_cleanup_config=fake_resolve,
        run_cleanup=fake_run_cleanup,
    )
    successes = []

    rt._register_cleanup_scheduler(
        toggles=type("Toggles", (), {"housekeeping_enabled": True})(),
        successes=successes,
        housekeeping_cleanup=cleanup_module,
    )

    assert calls["resolved"] == 1
    assert rt.scheduler.calls == [{"hours": 6.0, "tag": "cleanup", "name": "cleanup_watcher"}]
    assert [task["name"] for task in rt.scheduler.spawned] == [
        "cleanup_startup_validation",
        "cleanup_registration_notice",
    ]
    assert rt.bot.loop.created == []
    assert rt.scheduler.jobs[0].runner is not None
    assert successes[0][0].bucket == "cleanup"
    assert (
        "cleanup watcher scheduled: every=6h dry_run=true tab=CleanupRows "
        "next_run=2026-06-26T12:00:00Z"
    ) in caplog.text

    startup_validation = rt.scheduler.spawned[0]["coro"]
    asyncio.run(startup_validation)
    assert run_cleanup_calls[-1][1] == {
        "startup_validation": True,
        "writeback": False,
    }

    asyncio.run(rt.scheduler.jobs[0].runner())

    assert run_cleanup_calls[-1][0] == (rt.bot, cleanup.log)
    assert run_cleanup_calls[-1][1] == {
        "startup_validation": False,
        "writeback": True,
    }

    registration_notice = rt.scheduler.spawned[1]["coro"]
    asyncio.run(registration_notice)
    assert sent_logs == [
        "🧹 cleanup watcher tick: startup_validation=false writeback=true",
        "🧹 cleanup watcher scheduled: every=6h dry_run=true "
        "tab=CleanupRows next_run=2026-06-26T12:00:00Z",
    ]


def test_cleanup_manual_command_is_admin_only():
    admin_checks = [
        check for check in cleanup.CleanupCog.cleanup_run.checks
        if getattr(check, "__module__", "") == "c1c_coreops.rbac"
        and getattr(check, "__qualname__", "").startswith("admin_only")
    ]
    assert admin_checks, "!cleanup run must use the shared admin_only gate"

    replies = []

    class Ctx:
        guild = object()
        author = object()
        command = cleanup.CleanupCog.cleanup_run

        async def reply(self, message, *, mention_author=False):
            replies.append((message, mention_author))

    with pytest.raises(commands.CheckFailure):
        asyncio.run(admin_checks[0](Ctx()))
    assert replies == [("Admins only.", False)]


def test_cleanup_manual_command_calls_real_cleanup(monkeypatch, caplog):
    calls = []
    replies = []
    sent_logs = []

    async def fake_run_cleanup(*args, **kwargs):
        calls.append((args, kwargs))

    async def fake_send_log(message):
        sent_logs.append(message)

    class Ctx:
        bot = object()
        author = SimpleNamespace(id=1234)
        channel = SimpleNamespace(id=5678)

        async def reply(self, message, *, mention_author=False):
            replies.append((message, mention_author))

    monkeypatch.setattr(cleanup, "run_cleanup", fake_run_cleanup)
    monkeypatch.setattr(cleanup.runtime_helpers, "send_log_message", fake_send_log)
    caplog.set_level("INFO", logger="c1c.housekeeping.cleanup")

    cog = cleanup.CleanupCog(object())
    asyncio.run(cog.cleanup_run.callback(cog, Ctx()))

    assert replies == [
        ("Cleanup run started.", False),
        ("Cleanup run finished: deleted=0 candidates=0 skipped=0 errors=0", False),
    ]
    assert sent_logs == [
        "🧹 cleanup manual run requested: actor=1234 channel=5678",
        "🧹 Cleanup run finished: deleted=0 candidates=0 skipped=0 errors=0",
    ]
    assert "cleanup manual run requested: actor=1234 channel=5678" in caplog.text
    assert calls[0][0] == (Ctx.bot, cleanup.log)
    assert calls[0][1] == {"startup_validation": False, "writeback": True}


def test_cleanup_manual_command_surfaces_summary_errors(monkeypatch):
    replies = []
    sent_logs = []
    summary = cleanup.CleanupRunSummary(
        deleted=0,
        candidates=0,
        skipped=0,
        errors=1,
        status="sheet_unavailable_or_invalid",
        first_error="APIError: quota exceeded stage=read_values",
    )

    async def fake_run_cleanup(*_args, **_kwargs):
        return summary

    async def fake_send_log(message):
        sent_logs.append(message)

    class Ctx:
        bot = object()
        author = SimpleNamespace(id=1234)
        channel = SimpleNamespace(id=5678)

        async def reply(self, message, *, mention_author=False):
            replies.append((message, mention_author))

    monkeypatch.setattr(cleanup, "run_cleanup", fake_run_cleanup)
    monkeypatch.setattr(cleanup.runtime_helpers, "send_log_message", fake_send_log)

    cog = cleanup.CleanupCog(object())
    asyncio.run(cog.cleanup_run.callback(cog, Ctx()))

    expected = (
        "Cleanup run finished with errors: deleted=0 candidates=0 skipped=0 errors=1 "
        "status=sheet_unavailable_or_invalid first_error=APIError: quota exceeded at read_values"
    )
    assert replies[-1] == (expected, False)
    assert sent_logs[-1] == f"🧹 {expected}"


def test_cleanup_manual_command_handles_failure(monkeypatch, caplog):
    replies = []
    sent_logs = []

    async def fake_run_cleanup(*_args, **_kwargs):
        raise RuntimeError("boom")

    async def fake_send_log(message):
        sent_logs.append(message)

    class Ctx:
        bot = object()
        author = SimpleNamespace(id=1234)
        channel = SimpleNamespace(id=5678)

        async def reply(self, message, *, mention_author=False):
            replies.append((message, mention_author))

    monkeypatch.setattr(cleanup, "run_cleanup", fake_run_cleanup)
    monkeypatch.setattr(cleanup.runtime_helpers, "send_log_message", fake_send_log)
    caplog.set_level("ERROR", logger="c1c.housekeeping.cleanup")

    cog = cleanup.CleanupCog(object())
    asyncio.run(cog.cleanup_run.callback(cog, Ctx()))

    assert replies == [("Cleanup run started.", False), ("Cleanup run failed: RuntimeError. See logs.", False)]
    assert sent_logs == [
        "🧹 cleanup manual run requested: actor=1234 channel=5678",
        "🧹 cleanup manual run failed: RuntimeError: boom",
    ]
    assert "cleanup manual run failed: error_type=RuntimeError error=boom" in caplog.text
    failure_record = next(record for record in caplog.records if record.message.startswith("cleanup manual run failed"))
    assert failure_record.error_type == "RuntimeError"
    assert failure_record.error == "boom"
    assert failure_record.actor == 1234
    assert failure_record.channel == 5678


def test_run_cleanup_summary_notice_failure_does_not_fail_cleanup(monkeypatch, caplog):
    ws = Worksheet([REQUIRED_HEADERS, active_row()])
    target = Thread([Msg("bot", author_id=99)])
    cfg = cleanup.CleanupConfig(True, "CleanupRows", 6, False)

    async def fake_sheet(*_):
        return ws

    async def fake_resolve(_bot, _target_id):
        return target, "thread", None

    async def fake_log(_message):
        raise RuntimeError("discord log down")

    patch_async_cleanup_config(monkeypatch, cfg)
    monkeypatch.setattr(cleanup.recruitment, "get_recruitment_sheet_id", lambda: "sheet")
    monkeypatch.setattr(cleanup.async_core, "aget_worksheet", fake_sheet)
    monkeypatch.setattr(cleanup, "_resolve_any", fake_resolve)
    monkeypatch.setattr(cleanup.runtime_helpers, "send_log_message", fake_log)
    caplog.set_level("WARNING", logger="c1c.housekeeping.cleanup")

    summary = asyncio.run(cleanup.run_cleanup(Bot(target)))

    assert summary.deleted == 1
    assert summary.candidates == 1
    assert summary.summary_notice_failed is True
    assert "cleanup summary notice failed; cleanup completed" in caplog.text
    assert target.messages[0].deleted is True


def test_run_cleanup_read_values_api_error_sets_actionable_first_error(monkeypatch):
    class APIError(Exception):
        pass

    class FailingWorksheet:
        def get_all_values(self):
            raise APIError("quota exceeded")

    cfg = cleanup.CleanupConfig(True, "CleanupRows", 6, True)

    async def fake_sheet(*_):
        return FailingWorksheet()

    async def fake_to_thread_with_backoff(func, *args, **kwargs):
        return func(*args, **kwargs)

    patch_async_cleanup_config(monkeypatch, cfg)
    monkeypatch.setattr(cleanup.recruitment, "get_recruitment_sheet_id", lambda: "sheet")
    monkeypatch.setattr(cleanup.async_core, "aget_worksheet", fake_sheet)
    monkeypatch.setattr(cleanup.async_core, "a_to_thread_with_backoff", fake_to_thread_with_backoff)

    summary = asyncio.run(cleanup.run_cleanup(Bot(Thread([]))))

    assert summary.status == "sheet_unavailable_or_invalid"
    assert summary.errors == 1
    assert summary.first_error == "APIError: quota exceeded stage=read_values"
    assert cleanup._format_summary_error(summary) == "APIError: quota exceeded at read_values"


def test_run_cleanup_unexpected_failure_logs_stage_and_context(monkeypatch, caplog):
    ws = Worksheet([REQUIRED_HEADERS, active_row()])
    target = Thread([])
    cfg = cleanup.CleanupConfig(True, "CleanupRows", 6, False)

    async def fake_sheet(*_):
        return ws

    async def fake_resolve(_bot, _target_id):
        raise TypeError("target blew up")

    async def fake_log(_message):
        return None

    patch_async_cleanup_config(monkeypatch, cfg)
    monkeypatch.setattr(cleanup.recruitment, "get_recruitment_sheet_id", lambda: "sheet")
    monkeypatch.setattr(cleanup.async_core, "aget_worksheet", fake_sheet)
    monkeypatch.setattr(cleanup, "_resolve_any", fake_resolve)
    monkeypatch.setattr(cleanup.runtime_helpers, "send_log_message", fake_log)
    caplog.set_level("ERROR", logger="c1c.housekeeping.cleanup")

    with pytest.raises(TypeError):
        asyncio.run(cleanup.run_cleanup(Bot(target)))

    record = next(record for record in caplog.records if record.message.startswith("cleanup run unexpected failure"))
    assert record.error_type == "TypeError"
    assert record.error == "target blew up"
    assert record.stage == "resolve_target"
    assert record.row == 2
    assert record.target_id == 123
