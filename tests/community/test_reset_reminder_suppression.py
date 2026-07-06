import asyncio
import logging

from modules.community.reset_reminders import scheduler


def _exc(message="config boom", *, stage="config_load", tab="unknown"):
    exc = RuntimeError(message)
    setattr(exc, "reset_reminder_stage", stage)
    setattr(exc, "reset_reminder_tab", tab)
    setattr(exc, "reset_reminder_elapsed", 0.01)
    return exc


def _reset_state(monkeypatch):
    monkeypatch.setattr(scheduler, "_LOAD_FAILURE_ALERT_THRESHOLD", 1)
    monkeypatch.setattr(scheduler, "_LOAD_FAILURE_ALERT_COOLDOWN_SEC", 9999.0)
    scheduler._load_failure_state.update(
        {"key": None, "last_alert": 0.0, "failures": 0, "alert_sent": False}
    )


def test_reset_reminder_first_distinct_failure_logs_full_context(monkeypatch, caplog):
    sent = []

    async def fake_send(message):
        sent.append(message)

    _reset_state(monkeypatch)
    monkeypatch.setattr(scheduler, "_send_ops_log", fake_send)
    caplog.set_level(logging.INFO, logger="c1c.community.reset_reminders.scheduler")

    asyncio.run(scheduler._record_load_failure(_exc(tab="unknown")))

    full_logs = [
        record
        for record in caplog.records
        if record.message == "failed to load reset reminders; scheduler tick skipped"
    ]
    assert len(full_logs) == 1
    record = full_logs[0]
    assert record.exc_info is not None
    assert record.scheduler == "reset_reminders"
    assert record.config_key == "RESET_REMINDER_TAB"
    assert record.operation == "config_load"
    assert record.tab == "unknown"
    assert record.exception_type == "RuntimeError"
    assert record.exception_message == "config boom"
    assert len(sent) == 1


def test_reset_reminder_repeated_failure_full_logs_once(monkeypatch, caplog):
    sent = []

    async def fake_send(message):
        sent.append(message)

    _reset_state(monkeypatch)
    monkeypatch.setattr(scheduler, "_send_ops_log", fake_send)
    caplog.set_level(logging.INFO, logger="c1c.community.reset_reminders.scheduler")

    asyncio.run(scheduler._record_load_failure(_exc()))
    asyncio.run(scheduler._record_load_failure(_exc()))

    assert caplog.text.count("failed to load reset reminders; scheduler tick skipped") == 1
    assert "repeated reset reminder load failure suppressed" in caplog.text
    assert len(sent) == 1


def test_reset_reminder_changed_failure_reports_again(monkeypatch, caplog):
    sent = []

    async def fake_send(message):
        sent.append(message)

    _reset_state(monkeypatch)
    monkeypatch.setattr(scheduler, "_send_ops_log", fake_send)
    caplog.set_level(logging.INFO, logger="c1c.community.reset_reminders.scheduler")

    asyncio.run(scheduler._record_load_failure(_exc("first")))
    asyncio.run(scheduler._record_load_failure(_exc("second")))

    assert caplog.text.count("failed to load reset reminders; scheduler tick skipped") == 2
    assert len(sent) == 2


def test_reset_reminder_success_resets_suppression(monkeypatch, caplog):
    sent = []

    async def fake_send(message):
        sent.append(message)

    _reset_state(monkeypatch)
    monkeypatch.setattr(scheduler, "_send_ops_log", fake_send)
    caplog.set_level(logging.INFO, logger="c1c.community.reset_reminders.scheduler")

    asyncio.run(scheduler._record_load_failure(_exc("first")))
    asyncio.run(scheduler._record_load_failure(_exc("first")))
    asyncio.run(scheduler._record_load_success())
    asyncio.run(scheduler._record_load_failure(_exc("first")))

    assert caplog.text.count("failed to load reset reminders; scheduler tick skipped") == 2
    assert "reset reminder recovery message sent" in caplog.text
    assert scheduler._load_failure_state["failures"] == 1
    assert len(sent) == 3
