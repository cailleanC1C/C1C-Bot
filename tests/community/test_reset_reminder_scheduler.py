import asyncio
import datetime as dt
from types import SimpleNamespace

from modules.community.reset_reminders import scheduler


class _FakeJob:
    def __init__(self, *, name: str) -> None:
        self.name = name
        self._runner = None

    def do(self, runner):
        self._runner = runner


class _FakeScheduler:
    def __init__(self) -> None:
        self.jobs = []

    def every(self, **kwargs):
        job = _FakeJob(name=kwargs.get("name", ""))
        self.jobs.append(job)
        return job


class _DummyMessage:
    def __init__(self, message_id: int) -> None:
        self.id = message_id
        self.deleted = False

    async def delete(self) -> None:
        self.deleted = True


class _DummyChannel:
    def __init__(self) -> None:
        self.last_message = _DummyMessage(111)
        self.sent = []

    async def fetch_message(self, message_id: int):
        assert message_id == 111
        return self.last_message

    async def send(self, *, content=None, embed=None, view=None):
        self.sent.append({"content": content, "embed": embed, "view": view})
        return _DummyMessage(222)


class _DummyBot:
    def __init__(self, channel) -> None:
        self._channel = channel

    def is_closed(self) -> bool:
        return False

    def is_ready(self) -> bool:
        return True

    def get_channel(self, channel_id: int):
        assert channel_id == 123
        return self._channel

    def add_view(self, _view):
        return None


def test_schedule_reset_jobs_is_idempotent(monkeypatch) -> None:
    runtime = SimpleNamespace(bot=SimpleNamespace(), scheduler=_FakeScheduler())
    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)

    scheduler.schedule_reset_reminder_jobs(runtime)
    scheduler.schedule_reset_reminder_jobs(runtime)

    assert len(runtime.scheduler.jobs) == 1
    assert runtime.scheduler.jobs[0].name == "reset_reminders"


def test_schedule_reset_jobs_not_registered_when_disabled(monkeypatch) -> None:
    runtime = SimpleNamespace(bot=SimpleNamespace(), scheduler=_FakeScheduler())
    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: False)
    scheduler.schedule_reset_reminder_jobs(runtime)
    assert runtime.scheduler.jobs == []


def test_schedule_reset_jobs_registered_when_enabled(monkeypatch) -> None:
    runtime = SimpleNamespace(bot=SimpleNamespace(), scheduler=_FakeScheduler())
    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)
    scheduler.schedule_reset_reminder_jobs(runtime)
    assert len(runtime.scheduler.jobs) == 1
    assert runtime.scheduler.jobs[0].name == "reset_reminders"


def test_process_reset_reminder_sends_once_and_updates_sheet(monkeypatch) -> None:
    now = dt.datetime(2026, 5, 22, 23, 30, tzinfo=dt.timezone.utc)
    reminder = scheduler.ResetReminder(
        reset_id="doom_tower",
        label="Doom Tower",
        status="active",
        reference_date_utc=dt.datetime(2026, 3, 24, 0, 0, tzinfo=dt.timezone.utc),
        cycle_days=30,
        lead_minutes=60,
        role_id=999,
        channel_id=123,
        thread_id=None,
        embed_title="",
        embed_description="Reset incoming",
        embed_footer="footer",
        button_label_opt_in="Opt in",
        button_label_opt_out="Opt out",
        last_sent_for_reset_utc=None,
        next_scheduled_post_utc=None,
        last_message_id=111,
    )
    record = scheduler._ResetReminderRecord(row_number=7, reminder=reminder)
    updates = []

    async def _load(*, active_only: bool):
        return "ResetTab", {"last_sent_for_reset_utc": 14, "next_scheduled_post_utc": 15, "last_message_id": 16}, [record]

    async def _update(**kwargs):
        updates.append(kwargs)

    monkeypatch.setattr(scheduler, "_load_reset_reminder_records", _load)
    monkeypatch.setattr(scheduler, "_update_row_after_send", _update)
    monkeypatch.setattr(scheduler, "_update_next_scheduled_post", _update)
    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)

    channel = _DummyChannel()

    async def _resolve_target(_bot, _reminder):
        return channel

    monkeypatch.setattr(scheduler, "_resolve_target_channel", _resolve_target)
    bot = _DummyBot(channel)

    asyncio.run(scheduler.process_reset_reminders(bot, now=now))

    assert channel.last_message.deleted is True
    assert len(channel.sent) == 1
    assert channel.sent[0]["content"] == "<@&999>"
    assert len(updates) == 2
    assert updates[0]["row_number"] == 7
    assert updates[-1]["row_number"] == 7


def test_process_reset_reminder_dedupes_by_last_sent(monkeypatch) -> None:
    now = dt.datetime(2026, 5, 22, 23, 30, tzinfo=dt.timezone.utc)
    next_reset = dt.datetime(2026, 5, 23, 0, 0, tzinfo=dt.timezone.utc)
    reminder = scheduler.ResetReminder(
        reset_id="grim_forest",
        label="Grim Forest",
        status="active",
        reference_date_utc=dt.datetime(2026, 3, 24, 0, 0, tzinfo=dt.timezone.utc),
        cycle_days=30,
        lead_minutes=60,
        role_id=999,
        channel_id=123,
        thread_id=None,
        embed_title="",
        embed_description="Reset incoming",
        embed_footer="footer",
        button_label_opt_in="Opt in",
        button_label_opt_out="Opt out",
        last_sent_for_reset_utc=next_reset,
        next_scheduled_post_utc=dt.datetime(2026, 6, 21, 23, 0, tzinfo=dt.timezone.utc),
        last_message_id=111,
    )
    record = scheduler._ResetReminderRecord(row_number=7, reminder=reminder)
    channel = _DummyChannel()
    updates = []
    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)
    monkeypatch.setattr(scheduler, "_load_reset_reminder_records", lambda *, active_only: asyncio.sleep(0, result=("ResetTab", {"last_sent_for_reset_utc": 14, "next_scheduled_post_utc": 15, "last_message_id": 16}, [record])))
    monkeypatch.setattr(scheduler, "_resolve_target_channel", lambda *_args: asyncio.sleep(0, result=channel))
    async def _update(**kwargs):
        updates.append(kwargs)

    monkeypatch.setattr(scheduler, "_update_row_after_send", _update)
    monkeypatch.setattr(scheduler, "_update_next_scheduled_post", _update)
    bot = _DummyBot(channel)
    asyncio.run(scheduler.process_reset_reminders(bot, now=now))
    assert channel.sent == []
    assert updates == []


def test_process_reset_reminder_delete_failure_does_not_block_send(monkeypatch) -> None:
    now = dt.datetime(2026, 5, 22, 23, 30, tzinfo=dt.timezone.utc)

    class _FailDeleteMessage(_DummyMessage):
        async def delete(self) -> None:
            raise RuntimeError("delete failed")

    class _DeleteFailChannel(_DummyChannel):
        def __init__(self) -> None:
            super().__init__()
            self.last_message = _FailDeleteMessage(111)

    reminder = scheduler.ResetReminder(
        reset_id="cursed_city",
        label="Cursed City",
        status="active",
        reference_date_utc=dt.datetime(2026, 3, 24, 0, 0, tzinfo=dt.timezone.utc),
        cycle_days=30,
        lead_minutes=60,
        role_id=999,
        channel_id=123,
        thread_id=None,
        embed_title="",
        embed_description="Reset incoming",
        embed_footer="footer",
        button_label_opt_in="Opt in",
        button_label_opt_out="Opt out",
        last_sent_for_reset_utc=None,
        next_scheduled_post_utc=None,
        last_message_id=111,
    )
    record = scheduler._ResetReminderRecord(row_number=7, reminder=reminder)
    updates = []
    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)
    monkeypatch.setattr(scheduler, "_load_reset_reminder_records", lambda *, active_only: asyncio.sleep(0, result=("ResetTab", {"last_sent_for_reset_utc": 14, "next_scheduled_post_utc": 15, "last_message_id": 16}, [record])))
    async def _update(**kwargs):
        updates.append(kwargs)

    monkeypatch.setattr(scheduler, "_update_row_after_send", _update)
    monkeypatch.setattr(scheduler, "_update_next_scheduled_post", _update)
    channel = _DeleteFailChannel()
    monkeypatch.setattr(scheduler, "_resolve_target_channel", lambda *_args: asyncio.sleep(0, result=channel))
    bot = _DummyBot(channel)
    asyncio.run(scheduler.process_reset_reminders(bot, now=now))
    assert len(channel.sent) == 1
    assert len(updates) == 2


def test_invalid_rows_skipped_without_crash(monkeypatch) -> None:
    async def _fetch_values(_sheet_id, _tab):
        return [
            ["reset_id", "label", "status", "reference_date_utc", "cycle_days", "lead_minutes", "role_id", "channel_id", "thread_id", "embed_title", "embed_description", "embed_footer", "button_label_opt_in", "button_label_opt_out", "last_sent_for_reset_utc", "next_scheduled_post_utc", "last_message_id"],
            ["doom", "Doom", "active", "not-a-date", "30", "60", "1", "2", "", "", "", "", "Opt in", "Opt out", "", "", ""],
            ["grim", "Grim", "active", "2026-01-01T00:00:00Z", "30", "60", "1", "2", "", "", "", "", "Opt in", "Opt out", "", "", ""],
        ]

    monkeypatch.setattr(scheduler, "afetch_values", _fetch_values)
    monkeypatch.setattr(scheduler, "_tab_name", lambda: "ResetTab")
    monkeypatch.setattr(scheduler, "_sheet_id", lambda: "sheet")
    tab, header, records = asyncio.run(scheduler._load_reset_reminder_records(active_only=True))
    assert tab == "ResetTab"
    assert "reset_id" in header
    assert len(records) == 1


def test_register_persistent_views_disabled(monkeypatch) -> None:
    class _Bot:
        def __init__(self):
            self.views = []

        def add_view(self, view):
            self.views.append(view)

    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: False)
    bot = _Bot()
    asyncio.run(scheduler.register_persistent_reset_views(bot))
    assert bot.views == []


def test_register_persistent_views_active_rows(monkeypatch) -> None:
    class _Bot:
        def __init__(self):
            self.views = []

        def add_view(self, view):
            self.views.append(view)

    reminder = scheduler.ResetReminder(
        reset_id="doom_tower",
        label="Doom Tower",
        status="active",
        reference_date_utc=dt.datetime(2026, 3, 24, 0, 0, tzinfo=dt.timezone.utc),
        cycle_days=30,
        lead_minutes=60,
        role_id=999,
        channel_id=123,
        thread_id=None,
        embed_title="",
        embed_description="Reset incoming",
        embed_footer="footer",
        button_label_opt_in="Opt in",
        button_label_opt_out="Opt out",
        last_sent_for_reset_utc=None,
        next_scheduled_post_utc=None,
        last_message_id=None,
    )
    record = scheduler._ResetReminderRecord(row_number=2, reminder=reminder)
    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)
    monkeypatch.setattr(
        scheduler,
        "_load_reset_reminder_records",
        lambda *, active_only: asyncio.sleep(0, result=("ResetTab", {"reset_id": 0}, [record])),
    )

    bot = _Bot()
    asyncio.run(scheduler.register_persistent_reset_views(bot))
    assert len(bot.views) == 1
    view = bot.views[0]
    assert view.timeout is None
    custom_ids = [child.custom_id for child in view.children]
    assert custom_ids == ["reset_reminder:999:in", "reset_reminder:999:out"]


def _make_reset_reminder(**overrides):
    values = {
        "reset_id": "cursed_city",
        "label": "Cursed City",
        "status": "active",
        "reference_date_utc": dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc),
        "cycle_days": 28,
        "lead_minutes": 0,
        "role_id": 999,
        "channel_id": 123,
        "thread_id": None,
        "embed_title": "",
        "embed_description": "Reset incoming",
        "embed_footer": "footer",
        "button_label_opt_in": "Opt in",
        "button_label_opt_out": "Opt out",
        "last_sent_for_reset_utc": None,
        "next_scheduled_post_utc": None,
        "last_message_id": None,
    }
    values.update(overrides)
    return scheduler.ResetReminder(**values)


def _run_reset_process(monkeypatch, reminder, now):
    record = scheduler._ResetReminderRecord(row_number=7, reminder=reminder)
    channel = _DummyChannel()
    updates = []

    async def _load(*, active_only: bool):
        return (
            "ResetTab",
            {"last_sent_for_reset_utc": 14, "next_scheduled_post_utc": 15, "last_message_id": 16},
            [record],
        )

    async def _update(**kwargs):
        updates.append(kwargs)

    async def _resolve_target(_bot, resolved_reminder):
        assert resolved_reminder.thread_id is reminder.thread_id
        return channel

    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)
    monkeypatch.setattr(scheduler, "_load_reset_reminder_records", _load)
    monkeypatch.setattr(scheduler, "_update_next_scheduled_post", _update)
    monkeypatch.setattr(scheduler, "_update_row_after_send", _update)
    monkeypatch.setattr(scheduler, "_resolve_target_channel", _resolve_target)
    bot = _DummyBot(channel)
    asyncio.run(scheduler.process_reset_reminders(bot, now=now))
    return channel, updates


def test_next_scheduled_post_written_from_reference_minus_lead(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(reference_date_utc=reference, lead_minutes=60)

    channel, updates = _run_reset_process(
        monkeypatch,
        reminder,
        dt.datetime(2026, 5, 29, 8, 0, tzinfo=dt.timezone.utc),
    )

    assert channel.sent == []
    assert updates == [
        {
            "tab_name": "ResetTab",
            "header_map": {"last_sent_for_reset_utc": 14, "next_scheduled_post_utc": 15, "last_message_id": 16},
            "row_number": 7,
            "reminder_time": dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc),
        }
    ]


def test_lead_zero_posts_at_reset_time(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(reference_date_utc=reference, lead_minutes=0)

    channel, updates = _run_reset_process(monkeypatch, reminder, reference)

    assert len(channel.sent) == 1
    assert updates[-1]["reset_time"] == reference
    assert updates[-1]["next_scheduled_post"] == dt.datetime(2026, 6, 26, 10, 0, tzinfo=dt.timezone.utc)


def test_lead_zero_posts_after_reset_time_when_not_already_sent(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(reference_date_utc=reference, lead_minutes=0)

    channel, updates = _run_reset_process(
        monkeypatch,
        reminder,
        dt.datetime(2026, 5, 29, 10, 5, tzinfo=dt.timezone.utc),
    )

    assert len(channel.sent) == 1
    assert updates[-1]["reset_time"] == reference


def test_positive_lead_posts_before_reset_time(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(reference_date_utc=reference, lead_minutes=60)

    channel, updates = _run_reset_process(
        monkeypatch,
        reminder,
        dt.datetime(2026, 5, 29, 9, 30, tzinfo=dt.timezone.utc),
    )

    assert len(channel.sent) == 1
    assert updates[-1]["reset_time"] == reference
    assert updates[-1]["next_scheduled_post"] == dt.datetime(2026, 6, 26, 9, 0, tzinfo=dt.timezone.utc)


def test_successful_post_updates_last_sent_and_next_scheduled(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(reference_date_utc=reference, lead_minutes=0)

    _channel, updates = _run_reset_process(monkeypatch, reminder, reference)

    assert updates[-1]["reset_time"] == reference
    assert updates[-1]["next_scheduled_post"] == dt.datetime(2026, 6, 26, 10, 0, tzinfo=dt.timezone.utc)
    assert updates[-1]["message_id"] == 222


def test_same_last_sent_suppresses_duplicate(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    following = dt.datetime(2026, 6, 26, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference,
        lead_minutes=0,
        last_sent_for_reset_utc=reference,
        next_scheduled_post_utc=following,
    )

    channel, updates = _run_reset_process(monkeypatch, reminder, reference)

    assert channel.sent == []
    assert updates == []


def test_older_last_sent_does_not_suppress_current_reset(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference,
        lead_minutes=0,
        last_sent_for_reset_utc=dt.datetime(2026, 5, 1, 10, 0, tzinfo=dt.timezone.utc),
    )

    channel, updates = _run_reset_process(monkeypatch, reminder, reference)

    assert len(channel.sent) == 1
    assert updates[-1]["reset_time"] == reference


def test_empty_thread_id_posts_to_channel_id(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(reference_date_utc=reference, thread_id=None)

    channel, updates = _run_reset_process(monkeypatch, reminder, reference)

    assert len(channel.sent) == 1
    assert updates[-1]["reset_time"] == reference


def test_reset_reminder_load_timeout_ops_log_is_rate_limited_and_recovers(monkeypatch) -> None:
    now = dt.datetime(2026, 5, 29, 8, 0, tzinfo=dt.timezone.utc)
    sent_logs = []
    outcomes = [TimeoutError(), TimeoutError(), ("ResetTab", {}, [])]

    async def _load(*, active_only: bool):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            setattr(outcome, "reset_reminder_stage", "sheet_fetch")
            setattr(outcome, "reset_reminder_tab", "ResetTab")
            setattr(outcome, "reset_reminder_elapsed", 30.0)
            raise outcome
        return outcome

    async def _send(message: str):
        sent_logs.append(message)

    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)
    monkeypatch.setattr(scheduler, "_load_reset_reminder_records", _load)
    monkeypatch.setattr(scheduler, "_send_ops_log", _send)
    monkeypatch.setattr(scheduler, "_load_failure_state", {"key": None, "last_alert": 0.0, "failures": 0})
    monkeypatch.setattr(scheduler, "time", SimpleNamespace(monotonic=lambda: 100.0))

    bot = _DummyBot(_DummyChannel())
    asyncio.run(scheduler.process_reset_reminders(bot, now=now))
    asyncio.run(scheduler.process_reset_reminders(bot, now=now))
    asyncio.run(scheduler.process_reset_reminders(bot, now=now))

    assert sent_logs == [
        "⚠️ Reset reminders failed to load; scheduler tick skipped. See app logs. error=TimeoutError",
        "✅ Reset reminders loaded again after 2 failed tick(s).",
    ]


def test_reset_reminder_scheduler_runner_continues_after_timeout(monkeypatch) -> None:
    runtime = SimpleNamespace(bot=_DummyBot(_DummyChannel()), scheduler=_FakeScheduler())
    calls = {"count": 0}

    async def _process(_bot):
        calls["count"] += 1

    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)
    monkeypatch.setattr(scheduler, "process_reset_reminders", _process)

    scheduler.schedule_reset_reminder_jobs(runtime)
    asyncio.run(runtime.scheduler.jobs[0]._runner())
    asyncio.run(runtime.scheduler.jobs[0]._runner())

    assert calls["count"] == 2
