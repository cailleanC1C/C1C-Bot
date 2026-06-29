from modules.housekeeping import keepalive

REQUIRED_HEADERS = [
    "enabled",
    "target_id",
    "target_type",
    "target_name",
    "parent_name",
    "keepalive_message",
    "last_seen_at_utc",
    "last_keepalive_sent_at_utc",
    "last_status",
    "last_checked_at_utc",
    "notes",
]


def _toggle_status(*, present=True, enabled=False, invalid=False, invalid_value=None):
    return {
        "present": present,
        "enabled": enabled,
        "invalid": invalid,
        "invalid_value": invalid_value,
        "source_tab": "FeatureToggles",
    }


def test_resolve_keepalive_config_requires_sheet_keys(monkeypatch):
    monkeypatch.setenv("KEEPALIVE_CHANNEL_IDS", "123")
    monkeypatch.setenv("KEEPALIVE_THREAD_IDS", "456")
    monkeypatch.setenv("KEEPALIVE_INTERVAL_HOURS", "1")
    monkeypatch.setattr(
        keepalive.feature_flags, "status", lambda key: _toggle_status(enabled=True)
    )
    monkeypatch.setattr(
        keepalive.recruitment, "get_config_value", lambda _key, default=None: default
    )

    assert keepalive.resolve_keepalive_config() is None


def test_missing_feature_toggle_logs_missing_and_does_not_resolve(monkeypatch, caplog):
    monkeypatch.setattr(
        keepalive.feature_flags, "status", lambda key: _toggle_status(present=False)
    )
    monkeypatch.setattr(
        keepalive.recruitment,
        "get_config_value",
        lambda key, default=None: (_ for _ in ()).throw(
            AssertionError("Config must not be read when keepalive toggle is not TRUE")
        ),
    )

    assert keepalive.resolve_keepalive_config() is None

    assert (
        "required Feature Toggle HOUSEKEEPING_KEEPALIVE_ENABLED is missing"
        in caplog.text
    )


def test_false_feature_toggle_logs_disabled_and_does_not_resolve(monkeypatch, caplog):
    caplog.set_level("INFO", logger="c1c.housekeeping.keepalive")
    monkeypatch.setattr(
        keepalive.feature_flags, "status", lambda key: _toggle_status(enabled=False)
    )
    monkeypatch.setattr(
        keepalive.recruitment,
        "get_config_value",
        lambda key, default=None: (_ for _ in ()).throw(
            AssertionError("Config must not be read when keepalive toggle is not TRUE")
        ),
    )

    assert keepalive.resolve_keepalive_config() is None

    assert (
        "thread keepalive disabled by Feature Toggle HOUSEKEEPING_KEEPALIVE_ENABLED=FALSE"
        in caplog.text
    )


def test_invalid_feature_toggle_logs_invalid_and_does_not_resolve(monkeypatch, caplog):
    monkeypatch.setattr(
        keepalive.feature_flags,
        "status",
        lambda key: _toggle_status(invalid=True, invalid_value="sometimes"),
    )
    monkeypatch.setattr(
        keepalive.recruitment,
        "get_config_value",
        lambda key, default=None: (_ for _ in ()).throw(
            AssertionError("Config must not be read when keepalive toggle is not TRUE")
        ),
    )

    assert keepalive.resolve_keepalive_config() is None

    assert (
        "required Feature Toggle HOUSEKEEPING_KEEPALIVE_ENABLED has invalid value"
        in caplog.text
    )
    assert "sometimes" in caplog.text


def test_resolve_keepalive_config_async_never_calls_sync_resolver(monkeypatch):
    import asyncio

    values = {
        keepalive.CONFIG_TAB: "ConfiguredBySheet",
        keepalive.CONFIG_DEFAULT_MESSAGE: "bump",
        keepalive.CONFIG_STALE_AFTER_HOURS: "144",
        keepalive.CONFIG_RUN_EVERY_HOURS: "6",
    }

    monkeypatch.setattr(
        keepalive.feature_flags, "status", lambda key: _toggle_status(enabled=True)
    )
    monkeypatch.setattr(
        keepalive,
        "resolve_keepalive_config",
        lambda _logger=None: (_ for _ in ()).throw(
            AssertionError("async resolver must not call sync resolver")
        ),
    )

    async def fake_get_config_value_async(key, default=None):
        return values.get(key, default)

    monkeypatch.setattr(
        keepalive.recruitment, "get_config_value_async", fake_get_config_value_async
    )

    config = asyncio.run(keepalive.resolve_keepalive_config_async())

    assert config is not None
    assert config.tab_name == "ConfiguredBySheet"
    assert config.default_message == "bump"
    assert config.stale_after_hours == 144
    assert config.run_every_hours == 6

def test_resolve_keepalive_config_reads_enabled_from_feature_toggle_only(monkeypatch):
    requested_toggles = []
    config_keys = []
    values = {
        keepalive.CONFIG_ENABLED: "FALSE",
        keepalive.CONFIG_TAB: "ConfiguredBySheet",
        keepalive.CONFIG_DEFAULT_MESSAGE: "bump",
        keepalive.CONFIG_STALE_AFTER_HOURS: "144",
        keepalive.CONFIG_RUN_EVERY_HOURS: "6",
    }

    def fake_status(key):
        requested_toggles.append(key)
        return _toggle_status(enabled=True)

    def fake_get_config_value(key, default=None):
        config_keys.append(key)
        return values.get(key, default)

    monkeypatch.setattr(keepalive.feature_flags, "status", fake_status)
    monkeypatch.setattr(
        keepalive.recruitment,
        "get_config_value",
        fake_get_config_value,
    )

    config = keepalive.resolve_keepalive_config()

    assert requested_toggles == [keepalive.CONFIG_ENABLED]
    assert keepalive.CONFIG_ENABLED not in config_keys
    assert config is not None
    assert config.tab_name == "ConfiguredBySheet"
    assert config.stale_after_hours == 144
    assert config.run_every_hours == 6


def test_header_lookup_requires_headers_without_column_position_fallback():
    shuffled = list(reversed(REQUIRED_HEADERS))
    header_map = keepalive.build_header_map(shuffled)

    assert header_map["enabled"] == len(REQUIRED_HEADERS) - 1
    assert header_map["notes"] == 0


def test_header_lookup_rejects_missing_required_header():
    headers = [header for header in REQUIRED_HEADERS if header != "target_id"]

    try:
        keepalive.build_header_map(headers)
    except ValueError as exc:
        assert "target_id" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("missing target_id header should fail")


def test_message_selection_priority_and_no_hidden_fallback():
    assert (
        keepalive.select_keepalive_message("thread msg", "parent msg", "default msg")
        == "thread msg"
    )
    assert (
        keepalive.select_keepalive_message("", "parent msg", "default msg")
        == "parent msg"
    )
    assert keepalive.select_keepalive_message("", "", "default msg") == "default msg"
    assert keepalive.select_keepalive_message("", "", "") == ""


def test_row_update_only_writes_bot_owned_columns():
    header_map = keepalive.build_header_map(REQUIRED_HEADERS)
    row = keepalive.KeepaliveRow(sheet_row=2, values={})

    updates = keepalive._row_update(
        row,
        header_map,
        {
            "enabled": "FALSE",
            "target_id": "999",
            "keepalive_message": "do not overwrite",
            "notes": "do not overwrite",
            "target_type": "thread",
            "last_status": "posted",
        },
    )

    assert updates == {"C2": "thread", "I2": "posted"}


def _values(*rows):
    return [REQUIRED_HEADERS, *rows]


class _Worksheet:
    def __init__(self, values):
        self._values = values
        self.updates = []

    def get_all_values(self):
        return self._values

    def batch_update(self, updates):
        self.updates.extend(updates)


def _update_map(worksheet):
    return {item["range"]: item["values"][0][0] for item in worksheet.updates}


def _target(target_id, name, parent=None):
    return type("Target", (), {"id": target_id, "name": name, "parent": parent})()


def _run_keepalive_with_sheet(
    monkeypatch, worksheet, resolve_map, process_results, collect_map=None
):
    config = keepalive.KeepaliveConfig(
        enabled=True,
        tab_name="ConfiguredBySheet",
        default_message="global default",
        stale_after_hours=144,
        run_every_hours=6,
    )
    sent_messages = []

    async def fake_aget_worksheet(_sheet_id, _tab_name):
        return worksheet

    async def fake_resolve(_bot, target_id):
        return resolve_map[target_id]

    async def fake_collect_channel_threads(channel, _logger):
        return (collect_map or {}).get(channel.id, {}), 0

    async def fake_process_thread(thread, *, message, **_kwargs):
        sent_messages.append((thread.id, message))
        return process_results.get(
            thread.id, ("posted", True, "2026-01-02T00:00:00Z", 0)
        )

    async def fake_send_log_message(_summary):
        return None

    async def fake_resolve_config(_logger=None):
        return config

    monkeypatch.setattr(
        keepalive, "resolve_keepalive_config_async", fake_resolve_config
    )
    monkeypatch.setattr(
        keepalive.recruitment, "get_recruitment_sheet_id", lambda: "sheet"
    )
    monkeypatch.setattr(keepalive.async_core, "aget_worksheet", fake_aget_worksheet)
    monkeypatch.setattr(keepalive, "_resolve_any", fake_resolve)
    monkeypatch.setattr(
        keepalive, "_collect_channel_threads", fake_collect_channel_threads
    )
    monkeypatch.setattr(keepalive, "_process_thread", fake_process_thread)
    monkeypatch.setattr(
        keepalive.runtime_helpers, "send_log_message", fake_send_log_message
    )

    import asyncio

    asyncio.run(keepalive.run_keepalive(bot=object()))
    return sent_messages


def test_explicit_thread_uses_parent_channel_message_before_default(monkeypatch):
    parent = _target(100, "parent-channel")
    thread = _target(200, "child-thread", parent=parent)
    worksheet = _Worksheet(
        _values(
            ["TRUE", "200", "thread", "", "", "", "", "", "", "", ""],
            ["TRUE", "100", "channel", "", "", "parent msg", "", "", "", "", ""],
        )
    )

    sent_messages = _run_keepalive_with_sheet(
        monkeypatch,
        worksheet,
        {
            200: (thread, "thread", None),
            100: (parent, "channel", None),
        },
        {200: ("posted", True, "2026-01-01T00:00:00Z", 0)},
        {100: {}},
    )

    assert (200, "parent msg") in sent_messages
    assert (200, "global default") not in sent_messages


def test_channel_row_writes_keepalive_sent_at_when_child_posts(monkeypatch):
    channel = _target(100, "parent-channel")
    child = _target(200, "child-thread", parent=channel)
    worksheet = _Worksheet(
        _values(
            ["TRUE", "100", "channel", "", "", "parent msg", "", "", "", "", ""],
        )
    )

    _run_keepalive_with_sheet(
        monkeypatch,
        worksheet,
        {100: (channel, "channel", None)},
        {200: ("posted", True, "2026-01-01T00:00:00Z", 0)},
        {100: {200: child}},
    )

    updates = _update_map(worksheet)
    assert updates["G2"] == "2026-01-01T00:00:00Z"
    assert updates["H2"]
    assert updates["I2"] == "posted"
    assert updates["J2"] == updates["H2"]


def test_disabled_rows_write_status_and_checked_timestamp(monkeypatch):
    worksheet = _Worksheet(
        _values(
            ["FALSE", "200", "thread", "", "", "", "", "", "", "", ""],
        )
    )

    _run_keepalive_with_sheet(monkeypatch, worksheet, {}, {})

    updates = _update_map(worksheet)
    assert updates["I2"] == "disabled"
    assert updates["J2"]
    assert "B2" not in updates
    assert "F2" not in updates


def test_parent_channel_before_explicit_child_uses_child_message(monkeypatch):
    channel = _target(100, "parent-channel")
    child = _target(200, "child-thread", parent=channel)
    worksheet = _Worksheet(
        _values(
            ["TRUE", "100", "channel", "", "", "parent msg", "", "", "", "", ""],
            ["TRUE", "200", "thread", "", "", "child msg", "", "", "", "", ""],
        )
    )

    sent_messages = _run_keepalive_with_sheet(
        monkeypatch,
        worksheet,
        {100: (channel, "channel", None), 200: (child, "thread", None)},
        {200: ("posted", True, "2026-01-01T00:00:00Z", 0)},
        {100: {200: child}},
    )

    assert sent_messages == [(200, "child msg")]
    updates = _update_map(worksheet)
    assert updates["I2"] == "posted"
    assert updates["I3"] == "posted"


def test_explicit_child_before_parent_channel_posts_once(monkeypatch):
    channel = _target(100, "parent-channel")
    child = _target(200, "child-thread", parent=channel)
    worksheet = _Worksheet(
        _values(
            ["TRUE", "200", "thread", "", "", "child msg", "", "", "", "", ""],
            ["TRUE", "100", "channel", "", "", "parent msg", "", "", "", "", ""],
        )
    )

    sent_messages = _run_keepalive_with_sheet(
        monkeypatch,
        worksheet,
        {200: (child, "thread", None), 100: (channel, "channel", None)},
        {200: ("posted", True, "2026-01-01T00:00:00Z", 0)},
        {100: {200: child}},
    )

    assert sent_messages == [(200, "child msg")]


def test_parent_channel_message_used_when_child_has_no_specific_message(monkeypatch):
    channel = _target(100, "parent-channel")
    child = _target(200, "child-thread", parent=channel)
    worksheet = _Worksheet(
        _values(
            ["TRUE", "100", "channel", "", "", "parent msg", "", "", "", "", ""],
        )
    )

    sent_messages = _run_keepalive_with_sheet(
        monkeypatch,
        worksheet,
        {100: (channel, "channel", None)},
        {200: ("posted", True, "2026-01-01T00:00:00Z", 0)},
        {100: {200: child}},
    )

    assert sent_messages == [(200, "parent msg")]


def test_global_default_used_when_child_and_parent_messages_missing(monkeypatch):
    channel = _target(100, "parent-channel")
    child = _target(200, "child-thread", parent=channel)
    worksheet = _Worksheet(
        _values(
            ["TRUE", "100", "channel", "", "", "", "", "", "", "", ""],
        )
    )

    sent_messages = _run_keepalive_with_sheet(
        monkeypatch,
        worksheet,
        {100: (channel, "channel", None)},
        {200: ("posted", True, "2026-01-01T00:00:00Z", 0)},
        {100: {200: child}},
    )

    assert sent_messages == [(200, "global default")]


def test_parent_channel_before_blank_child_thread_uses_child_message(monkeypatch):
    channel = _target(100, "parent-channel")
    child = _target(200, "child-thread", parent=channel)
    worksheet = _Worksheet(
        _values(
            ["TRUE", "100", "channel", "", "", "parent msg", "", "", "", "", ""],
            ["TRUE", "200", "", "", "", "child msg", "", "", "", "", ""],
        )
    )

    sent_messages = _run_keepalive_with_sheet(
        monkeypatch,
        worksheet,
        {100: (channel, "channel", None), 200: (child, "thread", None)},
        {200: ("posted", True, "2026-01-01T00:00:00Z", 0)},
        {100: {200: child}},
    )

    assert sent_messages == [(200, "child msg")]


def test_blank_channel_row_is_usable_as_parent_message(monkeypatch):
    channel = _target(100, "parent-channel")
    child = _target(200, "child-thread", parent=channel)
    worksheet = _Worksheet(
        _values(
            ["TRUE", "100", "", "", "", "blank parent msg", "", "", "", "", ""],
        )
    )

    sent_messages = _run_keepalive_with_sheet(
        monkeypatch,
        worksheet,
        {100: (channel, "channel", None)},
        {200: ("posted", True, "2026-01-01T00:00:00Z", 0)},
        {100: {200: child}},
    )

    assert sent_messages == [(200, "blank parent msg")]


def test_unresolved_blank_target_type_not_added_to_message_maps(monkeypatch):
    row = keepalive.KeepaliveRow(
        sheet_row=2,
        values={
            "enabled": "TRUE",
            "target_id": "999",
            "target_type": "",
            "keepalive_message": "should not classify",
        },
    )

    async def fake_resolve(_bot, _target_id):
        return None, None, "not_found"

    monkeypatch.setattr(keepalive, "_resolve_any", fake_resolve)

    import asyncio

    parent_messages, explicit_thread_messages, sent_at = asyncio.run(
        keepalive._build_message_maps([row], bot=object())
    )

    assert parent_messages == {}
    assert explicit_thread_messages == {}
    assert sent_at == {}


def test_parent_not_stale_preserves_explicit_child_keepalive_sent_at(monkeypatch):
    channel = _target(100, "parent-channel")
    child = _target(200, "child-thread", parent=channel)
    prior_sent_at = "2026-01-01T12:00:00Z"
    worksheet = _Worksheet(
        _values(
            ["TRUE", "100", "channel", "", "", "parent msg", "", "", "", "", ""],
            [
                "TRUE",
                "200",
                "thread",
                "",
                "",
                "child msg",
                "",
                prior_sent_at,
                "",
                "",
                "",
            ],
        )
    )

    sent_messages = _run_keepalive_with_sheet(
        monkeypatch,
        worksheet,
        {100: (channel, "channel", None), 200: (child, "thread", None)},
        {200: ("ok_not_stale", False, "2026-01-02T00:00:00Z", 0)},
        {100: {200: child}},
    )

    updates = _update_map(worksheet)
    assert sent_messages == [(200, "child msg")]
    assert updates["H3"] == prior_sent_at
    assert updates["I3"] == "ok_not_stale"

class _KeepalivePerms:
    read_message_history = True
    send_messages = True
    manage_threads = True


class _KeepaliveAuthor:
    def __init__(self, author_id):
        self.id = author_id


class _KeepaliveMessage:
    def __init__(self, *, content, author_id, created_at, message_id=10):
        self.content = content
        self.author = _KeepaliveAuthor(author_id)
        self.created_at = created_at
        self.id = message_id
        self.deleted = False

    async def delete(self):
        self.deleted = True


class _KeepaliveGuild:
    def __init__(self, member):
        self._member = member

    def get_member(self, _user_id):
        return self._member


class _KeepaliveThread:
    def __init__(self, latest_message):
        self.id = 1234
        self.archived = False
        self.guild = _KeepaliveGuild(_KeepaliveAuthor(42))
        if isinstance(latest_message, list):
            self._latest_messages = latest_message
        else:
            self._latest_messages = [latest_message]
        self.sent = []

    def permissions_for(self, _member):
        return _KeepalivePerms()

    async def history(self, *, limit):
        assert limit == 1
        latest_message = (
            self._latest_messages.pop(0)
            if len(self._latest_messages) > 1
            else self._latest_messages[0]
        )
        if latest_message is not None:
            yield latest_message

    async def send(self, message):
        self.sent.append(message)


class _KeepaliveBot:
    user = _KeepaliveAuthor(42)


def _old_timestamp():
    from datetime import datetime, timedelta, timezone

    return datetime.now(timezone.utc) - timedelta(days=30)


def test_process_thread_deletes_previous_keepalive_when_latest():
    import asyncio
    import logging
    from datetime import timedelta

    latest = _KeepaliveMessage(
        content="🔹 KeepAlive, preventing archive",
        author_id=42,
        created_at=_old_timestamp(),
    )
    thread = _KeepaliveThread(latest)

    status, did_post, _last_seen, errors = asyncio.run(
        keepalive._process_thread(
            thread,
            stale_after_delta=timedelta(hours=1),
            message="🔹 KeepAlive, preventing archive",
            bot=_KeepaliveBot(),
            logger=logging.getLogger("test.keepalive"),
        )
    )

    assert status == "posted"
    assert did_post is True
    assert errors == 0
    assert latest.deleted is True
    assert thread.sent == ["🔹 KeepAlive, preventing archive"]


def test_process_thread_keeps_previous_keepalive_when_user_message_is_latest():
    import asyncio
    import logging
    from datetime import timedelta

    latest = _KeepaliveMessage(
        content="user follow-up",
        author_id=99,
        created_at=_old_timestamp(),
    )
    thread = _KeepaliveThread(latest)

    status, did_post, _last_seen, errors = asyncio.run(
        keepalive._process_thread(
            thread,
            stale_after_delta=timedelta(hours=1),
            message="🔹 KeepAlive, preventing archive",
            bot=_KeepaliveBot(),
            logger=logging.getLogger("test.keepalive"),
        )
    )

    assert status == "posted"
    assert did_post is True
    assert errors == 0
    assert latest.deleted is False
    assert thread.sent == ["🔹 KeepAlive, preventing archive"]


def test_process_thread_never_deletes_non_keepalive_bot_message():
    import asyncio
    import logging
    from datetime import timedelta

    latest = _KeepaliveMessage(
        content="regular bot update",
        author_id=42,
        created_at=_old_timestamp(),
    )
    thread = _KeepaliveThread(latest)

    status, did_post, _last_seen, errors = asyncio.run(
        keepalive._process_thread(
            thread,
            stale_after_delta=timedelta(hours=1),
            message="🔹 KeepAlive, preventing archive",
            bot=_KeepaliveBot(),
            logger=logging.getLogger("test.keepalive"),
        )
    )

    assert status == "posted"
    assert did_post is True
    assert errors == 0
    assert latest.deleted is False
    assert thread.sent == ["🔹 KeepAlive, preventing archive"]


def test_process_thread_refetches_latest_before_deleting_keepalive():
    import asyncio
    import logging
    from datetime import timedelta

    old_keepalive = _KeepaliveMessage(
        content="🔹 KeepAlive, preventing archive",
        author_id=42,
        created_at=_old_timestamp(),
        message_id=10,
    )
    newer_user_message = _KeepaliveMessage(
        content="user arrived after stale check",
        author_id=99,
        created_at=_old_timestamp(),
        message_id=11,
    )
    thread = _KeepaliveThread([old_keepalive, newer_user_message])

    status, did_post, _last_seen, errors = asyncio.run(
        keepalive._process_thread(
            thread,
            stale_after_delta=timedelta(hours=1),
            message="🔹 KeepAlive, preventing archive",
            bot=_KeepaliveBot(),
            logger=logging.getLogger("test.keepalive"),
        )
    )

    assert status == "posted"
    assert did_post is True
    assert errors == 0
    assert old_keepalive.deleted is False
    assert newer_user_message.deleted is False
    assert thread.sent == ["🔹 KeepAlive, preventing archive"]
