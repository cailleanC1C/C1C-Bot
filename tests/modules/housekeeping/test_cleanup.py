import asyncio
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

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
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None: config_keys.append(key) or values.get(key, default))
    cfg = cleanup.resolve_cleanup_config()
    assert requested_toggles == [cleanup.CONFIG_ENABLED]
    assert cleanup.CONFIG_ENABLED not in config_keys
    assert cfg and cfg.tab_name == "CleanupRows" and cfg.run_every_hours == 6 and cfg.dry_run is True


def test_enabled_feature_toggle_missing_config_prevents_scheduling(monkeypatch):
    monkeypatch.setattr(cleanup.feature_flags, "status", lambda key: _toggle_status(enabled=True))
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None: default)
    assert cleanup.resolve_cleanup_config() is None


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
    def __init__(self, content, *, author_id=10, pinned=False, hours_old=10):
        self.content = content
        self.author = type("Author", (), {"id": author_id})()
        self.pinned = pinned
        self.created_at = datetime.now(timezone.utc) - timedelta(hours=hours_old)
        self.channel = type("Channel", (), {"id": 123})()
        self.deleted = False
    async def delete(self, reason=None):
        self.deleted = True


class Thread:
    id = 123
    name = "thread-name"
    parent = type("Parent", (), {"name": "parent-name"})()
    async def history(self, limit=None, oldest_first=True):
        for msg in self.messages:
            yield msg


class Bot:
    command_prefix = "!"
    user = type("User", (), {"id": 99})()
    def __init__(self, target=None):
        self.target = target
    def get_channel(self, target_id):
        return self.target
    async def fetch_channel(self, target_id):
        return self.target


def run_with_sheet(monkeypatch, rows, messages, *, dry_run=True, startup_validation=False):
    ws = Worksheet([REQUIRED_HEADERS, *rows])
    thread = Thread(); thread.messages = messages
    cfg = cleanup.CleanupConfig(True, "CleanupRows", 6, dry_run)
    async def fake_sheet(*_): return ws
    async def fake_log(*_): return None
    monkeypatch.setattr(cleanup, "resolve_cleanup_config", lambda logger=None: cfg)
    monkeypatch.setattr(cleanup.recruitment, "get_recruitment_sheet_id", lambda: "sheet")
    monkeypatch.setattr(cleanup.async_core, "aget_worksheet", fake_sheet)
    monkeypatch.setattr(cleanup.runtime_helpers, "send_log_message", fake_log)
    async def fake_resolve(_bot, target_id):
        return (thread, "thread", None)
    monkeypatch.setattr(cleanup, "_resolve_any", fake_resolve)
    asyncio.run(cleanup.run_cleanup(Bot(thread), startup_validation=startup_validation))
    return ws, messages


def active_row(**overrides):
    data = {"enabled":"TRUE","target_id":"123","target_type":"","target_name":"","parent_name":"","cleanup_mode":"bot_messages_only","min_age_hours":"1","last_checked_at_utc":"","last_deleted_count":"","last_candidate_count":"","last_skipped_count":"","last_status":"","notes":"admin"}
    data.update(overrides)
    return [data[h] for h in REQUIRED_HEADERS]


def test_blank_target_type_resolved_and_dry_run_deletes_nothing(monkeypatch):
    ws, msgs = run_with_sheet(monkeypatch, [active_row()], [Msg("bot", author_id=99)])
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
    ws, _ = run_with_sheet(monkeypatch, rows, [])
    updates = update_map(ws)
    assert updates["L2"] == "invalid_target_id"
    assert updates["L3"] == "invalid_cleanup_mode"
    assert updates["L4"] == "invalid_min_age_hours"


def test_cleanup_modes_and_min_age_respected(monkeypatch):
    msgs = [Msg("human", author_id=1), Msg("!cmd", author_id=1), Msg("bot", author_id=99), Msg("pinned", author_id=99, pinned=True), Msg("young", author_id=99, hours_old=0)]
    ws, msgs = run_with_sheet(monkeypatch, [active_row(cleanup_mode="bot_messages_and_commands", min_age_hours="1")], msgs, dry_run=False)
    assert [m.deleted for m in msgs] == [False, True, True, False, False]
    updates = update_map(ws)
    assert updates["I2"] == "2"
    assert updates["J2"] == "2"
    assert updates["K2"] == "3"
    assert updates["L2"] == "deleted"


def test_all_non_pinned_skips_pinned(monkeypatch):
    msgs = [Msg("human", author_id=1), Msg("pinned", author_id=1, pinned=True)]
    run_with_sheet(monkeypatch, [active_row(cleanup_mode="all_non_pinned", min_age_hours="0")], msgs, dry_run=False)
    assert [m.deleted for m in msgs] == [True, False]


def test_bot_messages_only_and_commands_only_filters(monkeypatch):
    assert cleanup._matches_mode(Msg("human", author_id=1), "bot_messages_only", Bot(),) is False
    assert cleanup._matches_mode(Msg("!cmd", author_id=1), "commands_only", Bot(),) is True
    assert cleanup._matches_mode(Msg("normal", author_id=1), "commands_only", Bot(),) is False


def test_startup_validation_writeback_does_not_delete_when_dry_run_false(monkeypatch):
    msgs = [Msg("bot", author_id=99)]
    ws, _ = run_with_sheet(monkeypatch, [active_row()], msgs, dry_run=False, startup_validation=True)
    updates = update_map(ws)
    assert updates["C2"] == "thread"
    assert updates["H2"]
    assert updates["J2"] == "1"
    assert updates["K2"] == "0"
    assert updates["L2"] == "dry_run_ok"
    assert not msgs[0].deleted


def test_dry_run_writes_candidate_count_separately_from_skipped(monkeypatch):
    msgs = [Msg("bot", author_id=99), Msg("human", author_id=1)]
    ws, msgs = run_with_sheet(monkeypatch, [active_row()], msgs, dry_run=True)
    updates = update_map(ws)
    assert updates["I2"] == "0"
    assert updates["J2"] == "1"
    assert updates["K2"] == "1"
    assert updates["L2"] == "dry_run_ok"
    assert [m.deleted for m in msgs] == [False, False]


def test_cleanup_config_does_not_fall_back_to_legacy_env(monkeypatch):
    monkeypatch.setenv("CLEANUP_THREAD_IDS", "123")
    monkeypatch.setenv("CLEANUP_INTERVAL_HOURS", "1")
    monkeypatch.setattr(cleanup.feature_flags, "status", lambda key: _toggle_status(enabled=True))
    monkeypatch.setattr(cleanup.recruitment, "get_config_value", lambda key, default=None: default)
    assert cleanup.resolve_cleanup_config() is None


class _FakeJob:
    def __init__(self):
        self.runner = None
    def do(self, runner):
        self.runner = runner


class _FakeScheduler:
    def __init__(self):
        self.calls = []
    def every(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeJob()


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
    assert "housekeeping cleanup disabled via global housekeeping feature toggle" in caplog.text


def test_cleanup_schedules_only_when_global_and_cleanup_toggles_enabled():
    rt = object.__new__(runtime.Runtime)
    rt.scheduler = _FakeScheduler()
    rt.bot = type("Bot", (), {"loop": _FakeLoop()})()
    calls = {"resolved": 0}

    async def fake_run_cleanup(*_args, **_kwargs):
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
    assert len(rt.bot.loop.created) == 1
    assert successes[0][0].bucket == "cleanup"
