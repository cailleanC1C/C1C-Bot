import asyncio
import datetime as dt
import io
from types import SimpleNamespace

import discord
import pytest

from modules.community.reset_reminders import scheduler


class _FakeJob:
    def __init__(self, *, name: str, next_run=None) -> None:
        self.name = name
        self.next_run = next_run
        self._runner = None

    def do(self, runner):
        self._runner = runner

    def reschedule(self, next_run):
        self.next_run = next_run


class _FakeScheduler:
    def __init__(self) -> None:
        self.jobs = []

    def every(self, **kwargs):
        job = _FakeJob(name=kwargs.get("name", ""))
        self.jobs.append(job)
        return job

    def spawn(self, coro, **_kwargs):
        coro.close()

    def at(self, next_run, **kwargs):
        name = kwargs["name"]
        existing = next((job for job in self.jobs if job.name == name), None)
        if existing is not None:
            existing.reschedule(next_run)
            return existing
        job = _FakeJob(name=name, next_run=next_run)
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

    async def send(
        self, *, content=None, files=None, embed=None, view=None, allowed_mentions=None
    ):
        self.sent.append(
            {
                "content": content,
                "files": files or [],
                "embed": embed,
                "view": view,
                "allowed_mentions": allowed_mentions,
            }
        )
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


@pytest.fixture(autouse=True)
def _reset_scheduler_due_cache(monkeypatch):
    monkeypatch.setattr(
        scheduler,
        "_last_successful_load",
        {"tab_name": None, "header_map": None, "records": None},
    )
    monkeypatch.setattr(scheduler, "_next_sheet_load_after_utc", None)
    monkeypatch.setattr(
        scheduler,
        "_load_failure_state",
        {"key": None, "last_alert": 0.0, "failures": 0, "alert_sent": False},
    )


def test_schedule_reset_jobs_is_idempotent(monkeypatch) -> None:
    runtime = SimpleNamespace(bot=SimpleNamespace(), scheduler=_FakeScheduler())
    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)

    scheduler.schedule_reset_reminder_jobs(runtime)
    scheduler.schedule_reset_reminder_jobs(runtime)

    assert len(runtime.scheduler.jobs) == 1
    assert runtime.scheduler.jobs[0].name == "reset_reminders_reconcile"


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
    assert runtime.scheduler.jobs[0].name == "reset_reminders_reconcile"


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
        next_scheduled_post_utc=dt.datetime(2026, 5, 22, 23, 0, tzinfo=dt.timezone.utc),
        last_message_id=111,
    )
    record = scheduler._ResetReminderRecord(row_number=7, reminder=reminder)
    updates = []

    async def _load(*, active_only: bool):
        return (
            "ResetTab",
            {
                "last_sent_for_reset_utc": 14,
                "next_scheduled_post_utc": 15,
                "last_message_id": 16,
            },
            [record],
        )

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
    assert channel.sent[0]["allowed_mentions"].everyone is False
    assert channel.sent[0]["allowed_mentions"].roles is True
    assert channel.sent[0]["allowed_mentions"].users is False
    assert len(updates) == 1
    assert updates[0]["row_number"] == 7


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
    monkeypatch.setattr(
        scheduler,
        "_load_reset_reminder_records",
        lambda *, active_only: asyncio.sleep(
            0,
            result=(
                "ResetTab",
                {
                    "last_sent_for_reset_utc": 14,
                    "next_scheduled_post_utc": 15,
                    "last_message_id": 16,
                },
                [record],
            ),
        ),
    )
    monkeypatch.setattr(
        scheduler,
        "_resolve_target_channel",
        lambda *_args: asyncio.sleep(0, result=channel),
    )

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
        next_scheduled_post_utc=dt.datetime(2026, 5, 22, 23, 0, tzinfo=dt.timezone.utc),
        last_message_id=111,
    )
    record = scheduler._ResetReminderRecord(row_number=7, reminder=reminder)
    updates = []
    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)
    monkeypatch.setattr(
        scheduler,
        "_load_reset_reminder_records",
        lambda *, active_only: asyncio.sleep(
            0,
            result=(
                "ResetTab",
                {
                    "last_sent_for_reset_utc": 14,
                    "next_scheduled_post_utc": 15,
                    "last_message_id": 16,
                },
                [record],
            ),
        ),
    )

    async def _update(**kwargs):
        updates.append(kwargs)

    monkeypatch.setattr(scheduler, "_update_row_after_send", _update)
    monkeypatch.setattr(scheduler, "_update_next_scheduled_post", _update)
    channel = _DeleteFailChannel()
    monkeypatch.setattr(
        scheduler,
        "_resolve_target_channel",
        lambda *_args: asyncio.sleep(0, result=channel),
    )
    bot = _DummyBot(channel)
    asyncio.run(scheduler.process_reset_reminders(bot, now=now))
    assert len(channel.sent) == 1
    assert len(updates) == 1


def test_invalid_rows_skipped_without_crash(monkeypatch) -> None:
    async def _fetch_values(_sheet_id, _tab):
        return [
            [
                "reset_id",
                "label",
                "status",
                "reference_date_utc",
                "cycle_days",
                "lead_minutes",
                "role_id",
                "channel_id",
                "thread_id",
                "embed_title",
                "embed_description",
                "embed_footer",
                "button_label_opt_in",
                "button_label_opt_out",
                "last_sent_for_reset_utc",
                "next_scheduled_post_utc",
                "last_message_id",
                "EmojiNameOrId",
            ],
            [
                "doom",
                "Doom",
                "active",
                "not-a-date",
                "30",
                "60",
                "1",
                "2",
                "",
                "",
                "",
                "",
                "Opt in",
                "Opt out",
                "",
                "",
                "",
                "",
            ],
            [
                "grim",
                "Grim",
                "active",
                "2026-01-01T00:00:00Z",
                "30",
                "60",
                "1",
                "2",
                "",
                "",
                "",
                "",
                "Opt in",
                "Opt out",
                "",
                "",
                "",
                "",
            ],
        ]

    monkeypatch.setattr(scheduler, "afetch_values", _fetch_values)
    monkeypatch.setattr(
        scheduler, "_tab_name", lambda: asyncio.sleep(0, result="ResetTab")
    )
    monkeypatch.setattr(scheduler, "_sheet_id", lambda: "sheet")
    tab, header, records = asyncio.run(
        scheduler._load_reset_reminder_records(active_only=True)
    )
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
        lambda *, active_only: asyncio.sleep(
            0, result=("ResetTab", {"reset_id": 0}, [record])
        ),
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


def test_due_job_catches_up_once_then_backs_off_if_target_remains_missing(
    monkeypatch,
) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    reminder = _make_reset_reminder(
        next_scheduled_post_utc=now - dt.timedelta(minutes=5)
    )
    scheduler._last_successful_load["records"] = [
        scheduler._ResetReminderRecord(row_number=7, reminder=reminder)
    ]
    runtime = SimpleNamespace(bot=SimpleNamespace(), scheduler=_FakeScheduler())
    process = 0

    async def _no_op(_bot):
        nonlocal process
        process += 1

    monkeypatch.setattr(scheduler, "process_reset_reminders", _no_op)

    asyncio.run(scheduler.reconcile_reset_reminder_jobs(runtime))
    job = next(job for job in runtime.scheduler.jobs if job.name == "reset_reminders")
    assert job.next_run <= dt.datetime.now(dt.timezone.utc)
    asyncio.run(job._runner())

    # Reconcile/load and one immediate due attempt; a missing target or send
    # failure leaves the cached timestamp overdue, but cannot hot-loop.
    assert process == 2
    assert job.next_run >= dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        minutes=14, seconds=59
    )


def test_due_job_callback_failure_rearms_instead_of_disarming(monkeypatch) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    reminder = _make_reset_reminder(next_scheduled_post_utc=now)
    scheduler._last_successful_load["records"] = [
        scheduler._ResetReminderRecord(row_number=7, reminder=reminder)
    ]
    runtime = SimpleNamespace(bot=SimpleNamespace(), scheduler=_FakeScheduler())
    calls = 0

    async def _process(_bot):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("unexpected")

    monkeypatch.setattr(scheduler, "process_reset_reminders", _process)
    asyncio.run(scheduler.reconcile_reset_reminder_jobs(runtime))
    job = next(job for job in runtime.scheduler.jobs if job.name == "reset_reminders")

    with pytest.raises(RuntimeError, match="unexpected"):
        asyncio.run(job._runner())

    assert job.next_run is not None
    assert job.next_run >= dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        minutes=14, seconds=59
    )


def _run_reset_process(monkeypatch, reminder, now):
    record = scheduler._ResetReminderRecord(row_number=7, reminder=reminder)
    channel = _DummyChannel()
    updates = []

    async def _load(*, active_only: bool):
        return (
            "ResetTab",
            {
                "last_sent_for_reset_utc": 14,
                "next_scheduled_post_utc": 15,
                "last_message_id": 16,
            },
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
            "header_map": {
                "last_sent_for_reset_utc": 14,
                "next_scheduled_post_utc": 15,
                "last_message_id": 16,
            },
            "row_number": 7,
            "reminder_time": dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc),
        }
    ]


def test_lead_zero_at_reset_time_is_stale_and_advances(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference, lead_minutes=0, next_scheduled_post_utc=reference
    )

    channel, updates = _run_reset_process(monkeypatch, reminder, reference)

    assert channel.sent == []
    assert updates[-1]["reminder_time"] == dt.datetime(
        2026, 6, 26, 10, 0, tzinfo=dt.timezone.utc
    )


def test_lead_zero_after_reset_time_is_stale_and_advances(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference, lead_minutes=0, next_scheduled_post_utc=reference
    )

    channel, updates = _run_reset_process(
        monkeypatch,
        reminder,
        dt.datetime(2026, 5, 29, 10, 5, tzinfo=dt.timezone.utc),
    )

    assert channel.sent == []
    assert updates[-1]["reminder_time"] == dt.datetime(
        2026, 6, 26, 10, 0, tzinfo=dt.timezone.utc
    )


def test_positive_lead_posts_before_reset_time(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder_time = dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference,
        lead_minutes=60,
        next_scheduled_post_utc=reminder_time,
    )

    channel, updates = _run_reset_process(
        monkeypatch,
        reminder,
        dt.datetime(2026, 5, 29, 9, 30, tzinfo=dt.timezone.utc),
    )

    assert len(channel.sent) == 1
    assert updates[-1]["reset_time"] == reference
    assert updates[-1]["next_scheduled_post"] == dt.datetime(
        2026, 6, 26, 9, 0, tzinfo=dt.timezone.utc
    )


def test_successful_post_updates_last_sent_and_next_scheduled(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference,
        lead_minutes=60,
        next_scheduled_post_utc=dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc),
    )

    _channel, updates = _run_reset_process(
        monkeypatch, reminder, dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc)
    )

    assert updates[-1]["reset_time"] == reference
    assert updates[-1]["next_scheduled_post"] == dt.datetime(
        2026, 6, 26, 9, 0, tzinfo=dt.timezone.utc
    )
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

    channel, updates = _run_reset_process(
        monkeypatch, reminder, dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc)
    )

    assert channel.sent == []
    assert updates == []


def test_older_last_sent_does_not_suppress_current_reset(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference,
        lead_minutes=60,
        last_sent_for_reset_utc=dt.datetime(2026, 5, 1, 10, 0, tzinfo=dt.timezone.utc),
        next_scheduled_post_utc=dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc),
    )

    channel, updates = _run_reset_process(
        monkeypatch, reminder, dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc)
    )

    assert len(channel.sent) == 1
    assert updates[-1]["reset_time"] == reference


def test_empty_thread_id_posts_to_channel_id(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference,
        lead_minutes=60,
        thread_id=None,
        next_scheduled_post_utc=dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc),
    )

    channel, updates = _run_reset_process(
        monkeypatch, reminder, dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc)
    )

    assert len(channel.sent) == 1
    assert updates[-1]["reset_time"] == reference


def test_next_scheduled_post_present_future_does_not_send(monkeypatch) -> None:
    reference = dt.datetime(2026, 6, 8, 7, 30, tzinfo=dt.timezone.utc)
    scheduled = dt.datetime(2026, 7, 8, 3, 30, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reset_id="doom_tower",
        reference_date_utc=reference,
        cycle_days=30,
        lead_minutes=240,
        next_scheduled_post_utc=scheduled,
    )

    channel, updates = _run_reset_process(
        monkeypatch, reminder, dt.datetime(2026, 7, 8, 3, 29, tzinfo=dt.timezone.utc)
    )

    assert channel.sent == []
    assert updates == []


def test_next_scheduled_post_present_due_displays_derived_reset_time(
    monkeypatch,
) -> None:
    reference = dt.datetime(2026, 6, 8, 7, 30, tzinfo=dt.timezone.utc)
    scheduled = dt.datetime(2026, 7, 8, 3, 30, tzinfo=dt.timezone.utc)
    reset_time = dt.datetime(2026, 7, 8, 7, 30, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reset_id="doom_tower",
        reference_date_utc=reference,
        cycle_days=30,
        lead_minutes=240,
        next_scheduled_post_utc=scheduled,
    )

    channel, updates = _run_reset_process(monkeypatch, reminder, scheduled)

    assert len(channel.sent) == 1
    assert (
        f"<t:{int(reset_time.timestamp())}:F>" in channel.sent[0]["embed"].description
    )
    assert (
        f"<t:{int(reference.timestamp())}:F>"
        not in channel.sent[0]["embed"].description
    )
    assert updates[-1]["reset_time"] == reset_time
    assert updates[-1]["next_scheduled_post"] == dt.datetime(
        2026, 8, 7, 3, 30, tzinfo=dt.timezone.utc
    )


def test_next_scheduled_post_blank_computes_from_anchor_and_does_not_send_stale(
    monkeypatch,
) -> None:
    reference = dt.datetime(2026, 6, 8, 7, 30, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reset_id="doom_tower",
        reference_date_utc=reference,
        cycle_days=30,
        lead_minutes=240,
        next_scheduled_post_utc=None,
    )

    channel, updates = _run_reset_process(
        monkeypatch, reminder, dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc)
    )

    assert channel.sent == []
    assert updates == [
        {
            "tab_name": "ResetTab",
            "header_map": {
                "last_sent_for_reset_utc": 14,
                "next_scheduled_post_utc": 15,
                "last_message_id": 16,
            },
            "row_number": 7,
            "reminder_time": dt.datetime(2026, 7, 8, 3, 30, tzinfo=dt.timezone.utc),
        }
    ]


def test_blank_next_scheduled_cached_tick_does_not_rewrite(monkeypatch) -> None:
    now = dt.datetime(2026, 7, 1, 12, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reset_id="doom_tower",
        reference_date_utc=dt.datetime(2026, 6, 8, 7, 30, tzinfo=dt.timezone.utc),
        cycle_days=30,
        lead_minutes=240,
        next_scheduled_post_utc=None,
    )
    record = scheduler._ResetReminderRecord(row_number=7, reminder=reminder)
    updates = []
    load_calls = {"count": 0}

    async def _load(*, active_only: bool):
        load_calls["count"] += 1
        return (
            "ResetTab",
            {
                "last_sent_for_reset_utc": 14,
                "next_scheduled_post_utc": 15,
                "last_message_id": 16,
            },
            [record],
        )

    async def _update(**kwargs):
        updates.append(kwargs)

    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)
    monkeypatch.setattr(scheduler, "_load_reset_reminder_records", _load)
    monkeypatch.setattr(scheduler, "_update_next_scheduled_post", _update)
    monkeypatch.setattr(scheduler, "_update_row_after_send", _update)

    bot = _DummyBot(_DummyChannel())
    asyncio.run(scheduler.process_reset_reminders(bot, now=now))
    asyncio.run(
        scheduler.process_reset_reminders(bot, now=now + dt.timedelta(minutes=1))
    )

    assert load_calls["count"] == 1
    assert len(updates) == 1
    assert updates[0]["reminder_time"] == dt.datetime(
        2026, 7, 8, 3, 30, tzinfo=dt.timezone.utc
    )


def test_due_reminder_cached_tick_does_not_resend(monkeypatch) -> None:
    reminder_time = dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc)
    reset_time = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reset_id="doom_tower",
        reference_date_utc=reset_time,
        cycle_days=28,
        lead_minutes=60,
        next_scheduled_post_utc=reminder_time,
        last_sent_for_reset_utc=None,
        last_message_id=111,
    )
    record = scheduler._ResetReminderRecord(row_number=7, reminder=reminder)
    updates = []
    load_calls = {"count": 0}
    channel = _DummyChannel()

    async def _load(*, active_only: bool):
        load_calls["count"] += 1
        return (
            "ResetTab",
            {
                "last_sent_for_reset_utc": 14,
                "next_scheduled_post_utc": 15,
                "last_message_id": 16,
            },
            [record],
        )

    async def _update(**kwargs):
        updates.append(kwargs)

    async def _resolve_target(_bot, _reminder):
        return channel

    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)
    monkeypatch.setattr(scheduler, "_load_reset_reminder_records", _load)
    monkeypatch.setattr(scheduler, "_update_next_scheduled_post", _update)
    monkeypatch.setattr(scheduler, "_update_row_after_send", _update)
    monkeypatch.setattr(scheduler, "_resolve_target_channel", _resolve_target)

    bot = _DummyBot(channel)
    asyncio.run(scheduler.process_reset_reminders(bot, now=reminder_time))
    asyncio.run(
        scheduler.process_reset_reminders(
            bot, now=reminder_time + dt.timedelta(minutes=1)
        )
    )

    assert load_calls["count"] == 1
    assert len(channel.sent) == 1
    assert len(updates) == 1
    assert updates[0]["reset_time"] == reset_time
    cached = scheduler._last_successful_load["records"][0].reminder
    assert cached.last_sent_for_reset_utc == reset_time
    assert cached.next_scheduled_post_utc == dt.datetime(
        2026, 6, 26, 9, 0, tzinfo=dt.timezone.utc
    )
    assert cached.last_message_id == 222


def test_due_reminder_sheet_update_failure_still_suppresses_cached_resend(
    monkeypatch,
) -> None:
    reminder_time = dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc)
    reset_time = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reset_id="doom_tower",
        reference_date_utc=reset_time,
        cycle_days=28,
        lead_minutes=60,
        next_scheduled_post_utc=reminder_time,
        last_sent_for_reset_utc=None,
        last_message_id=111,
    )
    record = scheduler._ResetReminderRecord(row_number=7, reminder=reminder)
    load_calls = {"count": 0}
    update_attempts = {"count": 0}
    ops_logs: list[str] = []
    channel = _DummyChannel()

    async def _load(*, active_only: bool):
        load_calls["count"] += 1
        return (
            "ResetTab",
            {
                "last_sent_for_reset_utc": 14,
                "next_scheduled_post_utc": 15,
                "last_message_id": 16,
            },
            [record],
        )

    async def _update(**_kwargs):
        update_attempts["count"] += 1
        raise RuntimeError("sheet quota")

    async def _resolve_target(_bot, _reminder):
        return channel

    async def _ops_log(message: str):
        ops_logs.append(message)

    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)
    monkeypatch.setattr(scheduler, "_load_reset_reminder_records", _load)
    monkeypatch.setattr(scheduler, "_update_row_after_send", _update)
    monkeypatch.setattr(scheduler, "_resolve_target_channel", _resolve_target)
    monkeypatch.setattr(scheduler, "_send_ops_log", _ops_log)

    bot = _DummyBot(channel)
    asyncio.run(scheduler.process_reset_reminders(bot, now=reminder_time))
    asyncio.run(
        scheduler.process_reset_reminders(
            bot, now=reminder_time + dt.timedelta(minutes=1)
        )
    )

    assert load_calls["count"] == 1
    assert len(channel.sent) == 1
    assert update_attempts["count"] == 1
    cached = scheduler._last_successful_load["records"][0].reminder
    assert cached.last_sent_for_reset_utc == reset_time
    assert cached.next_scheduled_post_utc == dt.datetime(
        2026, 6, 26, 9, 0, tzinfo=dt.timezone.utc
    )
    assert cached.last_message_id == 222
    assert ops_logs and "posted but sheet update failed" in ops_logs[0]


def test_same_last_sent_cached_tick_advances_once(monkeypatch) -> None:
    reminder_time = dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc)
    reset_time = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reset_time,
        cycle_days=28,
        lead_minutes=60,
        last_sent_for_reset_utc=reset_time,
        next_scheduled_post_utc=reminder_time,
    )
    record = scheduler._ResetReminderRecord(row_number=7, reminder=reminder)
    updates = []
    load_calls = {"count": 0}

    async def _load(*, active_only: bool):
        load_calls["count"] += 1
        return (
            "ResetTab",
            {
                "last_sent_for_reset_utc": 14,
                "next_scheduled_post_utc": 15,
                "last_message_id": 16,
            },
            [record],
        )

    async def _update(**kwargs):
        updates.append(kwargs)

    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)
    monkeypatch.setattr(scheduler, "_load_reset_reminder_records", _load)
    monkeypatch.setattr(scheduler, "_update_next_scheduled_post", _update)
    monkeypatch.setattr(scheduler, "_update_row_after_send", _update)

    bot = _DummyBot(_DummyChannel())
    asyncio.run(scheduler.process_reset_reminders(bot, now=reminder_time))
    asyncio.run(
        scheduler.process_reset_reminders(
            bot, now=reminder_time + dt.timedelta(minutes=1)
        )
    )

    assert load_calls["count"] == 1
    assert len(updates) == 1
    assert updates[0]["reminder_time"] == dt.datetime(
        2026, 6, 26, 9, 0, tzinfo=dt.timezone.utc
    )


def test_next_scheduled_post_due_but_reset_past_skips_and_advances(
    monkeypatch, caplog
) -> None:
    reference = dt.datetime(2026, 6, 8, 7, 30, tzinfo=dt.timezone.utc)
    scheduled = dt.datetime(2026, 7, 8, 3, 30, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reset_id="doom_tower",
        reference_date_utc=reference,
        cycle_days=30,
        lead_minutes=240,
        next_scheduled_post_utc=scheduled,
    )

    caplog.set_level("WARNING", logger="c1c.community.reset_reminders.scheduler")
    channel, updates = _run_reset_process(
        monkeypatch, reminder, dt.datetime(2026, 7, 8, 7, 30, tzinfo=dt.timezone.utc)
    )

    assert channel.sent == []
    assert updates[-1]["reminder_time"] == dt.datetime(
        2026, 8, 7, 3, 30, tzinfo=dt.timezone.utc
    )
    assert "stale reset reminder skipped; next scheduled post advanced" in caplog.text


def test_doom_tower_regression_uses_next_scheduled_post_not_reference_date(
    monkeypatch,
) -> None:
    reference = dt.datetime(2026, 6, 8, 7, 30, tzinfo=dt.timezone.utc)
    scheduled = dt.datetime(2026, 7, 8, 3, 30, tzinfo=dt.timezone.utc)
    reset_time = dt.datetime(2026, 7, 8, 7, 30, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reset_id="doom_tower",
        label="Doom Tower",
        reference_date_utc=reference,
        cycle_days=30,
        lead_minutes=240,
        next_scheduled_post_utc=scheduled,
    )

    channel, updates = _run_reset_process(monkeypatch, reminder, scheduled)

    assert len(channel.sent) == 1
    embed = channel.sent[0]["embed"]
    assert embed.timestamp is None
    assert f"<t:{int(reset_time.timestamp())}:R>" in embed.description
    assert embed.footer.text == "footer • Following cycle reset: 2026-08-07 07:30 UTC"
    assert updates[-1]["next_scheduled_post"] == dt.datetime(
        2026, 8, 7, 3, 30, tzinfo=dt.timezone.utc
    )


def test_reset_reminder_load_timeout_ops_log_is_rate_limited_and_recovers(
    monkeypatch,
) -> None:
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
    monkeypatch.setattr(
        scheduler,
        "_load_failure_state",
        {"key": None, "last_alert": 0.0, "failures": 0},
    )
    monkeypatch.setattr(scheduler, "time", SimpleNamespace(monotonic=lambda: 100.0))

    bot = _DummyBot(_DummyChannel())
    asyncio.run(scheduler.process_reset_reminders(bot, now=now))
    asyncio.run(scheduler.process_reset_reminders(bot, now=now))
    asyncio.run(scheduler.process_reset_reminders(bot, now=now))

    # A short transient outage is retried and no longer sends noisy Discord
    # failure/recovery alerts unless the warning threshold is reached.
    assert sent_logs == []


def test_reset_reminder_scheduler_runner_reconciles_daily(monkeypatch) -> None:
    runtime = SimpleNamespace(
        bot=_DummyBot(_DummyChannel()), scheduler=_FakeScheduler()
    )
    calls = {"count": 0}

    async def _process(_bot):
        calls["count"] += 1

    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)
    monkeypatch.setattr(scheduler, "process_reset_reminders", _process)

    scheduler.schedule_reset_reminder_jobs(runtime)

    async def _reconcile(_runtime):
        calls["count"] += 1

    monkeypatch.setattr(scheduler, "reconcile_reset_reminder_jobs", _reconcile)
    asyncio.run(runtime.scheduler.jobs[0]._runner())
    asyncio.run(runtime.scheduler.jobs[0]._runner())

    assert calls["count"] == 2


class _FakeEmoji:
    def __init__(self, emoji_id: int, name: str, animated: bool = False) -> None:
        self.id = emoji_id
        self.name = name
        self.animated = animated

    async def read(self) -> bytes:
        return b"fake-image-bytes"

    def __str__(self) -> str:
        prefix = "a" if self.animated else ""
        return f"<{prefix}:{self.name}:{self.id}>"


class _GuildChannel(_DummyChannel):
    def __init__(self, emojis) -> None:
        super().__init__()
        self.guild = SimpleNamespace(emojis=emojis)


def test_missing_emoji_name_or_id_column_is_schema_error(monkeypatch) -> None:
    async def _fetch_values(_sheet_id, _tab):
        return [
            [
                "reset_id",
                "label",
                "status",
                "reference_date_utc",
                "cycle_days",
                "lead_minutes",
                "role_id",
                "channel_id",
                "thread_id",
                "embed_title",
                "embed_description",
                "embed_footer",
                "button_label_opt_in",
                "button_label_opt_out",
                "last_sent_for_reset_utc",
                "next_scheduled_post_utc",
                "last_message_id",
            ],
            [
                "doom",
                "Doom",
                "active",
                "2026-01-01T00:00:00Z",
                "30",
                "60",
                "1",
                "2",
                "",
                "",
                "Body",
                "",
                "Opt in",
                "Opt out",
                "",
                "",
                "",
            ],
        ]

    monkeypatch.setattr(scheduler, "afetch_values", _fetch_values)
    monkeypatch.setattr(
        scheduler, "_tab_name", lambda: asyncio.sleep(0, result="ResetTab")
    )
    monkeypatch.setattr(scheduler, "_sheet_id", lambda: "sheet")
    with pytest.raises(RuntimeError, match="emojinameorid"):
        asyncio.run(scheduler._load_reset_reminder_records(active_only=True))


def test_exact_emoji_name_or_id_sheet_header_is_parsed(monkeypatch) -> None:
    async def _fetch_values(_sheet_id, _tab):
        return [
            [
                "reset_id",
                "label",
                "status",
                "reference_date_utc",
                "cycle_days",
                "lead_minutes",
                "role_id",
                "channel_id",
                "thread_id",
                "embed_title",
                "embed_description",
                "embed_footer",
                "button_label_opt_in",
                "button_label_opt_out",
                "last_sent_for_reset_utc",
                "next_scheduled_post_utc",
                "last_message_id",
                "EmojiNameOrId",
            ],
            [
                "doom",
                "Doom",
                "active",
                "2026-01-01T00:00:00Z",
                "30",
                "60",
                "1",
                "2",
                "",
                "",
                "Body",
                "",
                "Opt in",
                "Opt out",
                "",
                "",
                "",
                "doom_tower_icon",
            ],
        ]

    monkeypatch.setattr(scheduler, "afetch_values", _fetch_values)
    monkeypatch.setattr(
        scheduler, "_tab_name", lambda: asyncio.sleep(0, result="ResetTab")
    )
    monkeypatch.setattr(scheduler, "_sheet_id", lambda: "sheet")

    _tab, header, records = asyncio.run(
        scheduler._load_reset_reminder_records(active_only=True)
    )

    assert "emojinameorid" in header
    assert records[0].reminder.emoji_name_or_id == "doom_tower_icon"


def test_empty_emoji_name_or_id_keeps_message_content_unchanged(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference,
        lead_minutes=60,
        next_scheduled_post_utc=dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc),
        emoji_name_or_id=" ",
    )
    channel, _updates = _run_reset_process(
        monkeypatch, reminder, dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc)
    )
    assert channel.sent[0]["content"] == "<@&999>"
    assert channel.sent[0]["files"] == []


def test_numeric_emoji_id_resolves_into_standalone_file(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference,
        lead_minutes=60,
        next_scheduled_post_utc=dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc),
        emoji_name_or_id=" 1413557246297637025 ",
    )
    channel = _GuildChannel([_FakeEmoji(1413557246297637025, "doom_tower_icon")])
    monkeypatch.setattr(
        scheduler,
        "_resolve_target_channel",
        lambda *_args: asyncio.sleep(0, result=channel),
    )
    record = scheduler._ResetReminderRecord(row_number=7, reminder=reminder)
    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)
    monkeypatch.setattr(
        scheduler,
        "_load_reset_reminder_records",
        lambda *, active_only: asyncio.sleep(
            0,
            result=(
                "ResetTab",
                {
                    "last_sent_for_reset_utc": 14,
                    "next_scheduled_post_utc": 15,
                    "last_message_id": 16,
                },
                [record],
            ),
        ),
    )
    monkeypatch.setattr(
        scheduler, "_update_next_scheduled_post", lambda **kwargs: asyncio.sleep(0)
    )
    monkeypatch.setattr(
        scheduler, "_update_row_after_send", lambda **kwargs: asyncio.sleep(0)
    )
    asyncio.run(
        scheduler.process_reset_reminders(
            _DummyBot(channel),
            now=dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc),
        )
    )
    assert channel.sent[0]["content"] == "<@&999>"
    assert len(channel.sent[0]["files"]) == 1
    assert channel.sent[0]["files"][0].filename == "cursed_city_icon.png"


def test_emoji_name_resolves_to_file_and_is_not_embed_thumbnail(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference,
        lead_minutes=60,
        next_scheduled_post_utc=dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc),
        emoji_name_or_id="doom_tower_icon",
    )
    channel = _GuildChannel([_FakeEmoji(12345, "doom_tower_icon")])
    monkeypatch.setattr(
        scheduler,
        "_resolve_target_channel",
        lambda *_args: asyncio.sleep(0, result=channel),
    )
    record = scheduler._ResetReminderRecord(row_number=7, reminder=reminder)
    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)
    monkeypatch.setattr(
        scheduler,
        "_load_reset_reminder_records",
        lambda *, active_only: asyncio.sleep(
            0,
            result=(
                "ResetTab",
                {
                    "last_sent_for_reset_utc": 14,
                    "next_scheduled_post_utc": 15,
                    "last_message_id": 16,
                },
                [record],
            ),
        ),
    )
    monkeypatch.setattr(
        scheduler, "_update_next_scheduled_post", lambda **kwargs: asyncio.sleep(0)
    )
    monkeypatch.setattr(
        scheduler, "_update_row_after_send", lambda **kwargs: asyncio.sleep(0)
    )
    asyncio.run(
        scheduler.process_reset_reminders(
            _DummyBot(channel),
            now=dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc),
        )
    )
    sent = channel.sent[0]
    assert sent["content"] == "<@&999>"
    assert len(sent["files"]) == 1
    assert sent["files"][0].filename == "cursed_city_icon.png"
    assert sent["embed"].thumbnail.url is None


def test_direct_image_url_resolves_into_standalone_file(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference,
        lead_minutes=60,
        next_scheduled_post_utc=dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc),
        emoji_name_or_id="https://cdn.example.invalid/reset.png",
    )

    async def _download(url: str, *, reminder: scheduler.ResetReminder):
        assert url == "https://cdn.example.invalid/reset.png"
        return discord.File(io.BytesIO(b"png"), filename="direct_reset_icon.png")

    monkeypatch.setattr(scheduler, "_download_image_url_to_file", _download)
    channel, _updates = _run_reset_process(
        monkeypatch, reminder, dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc)
    )

    sent = channel.sent[0]
    assert sent["content"] == "<@&999>"
    assert len(sent["files"]) == 1
    assert sent["files"][0].filename == "direct_reset_icon.png"
    assert sent["embed"].thumbnail.url is None


def test_direct_image_url_rejects_non_image_content_type_and_sends_without_file(
    monkeypatch, caplog
) -> None:
    class _Content:
        async def read(self, _limit: int) -> bytes:
            return b"not an image"

    class _Response:
        status = 200
        headers = {"Content-Type": "text/html; charset=utf-8"}
        content = _Content()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    class _Session:
        def get(self, url: str):
            assert url == "https://cdn.example.invalid/not-image"
            return _Response()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

    monkeypatch.setattr(scheduler.aiohttp, "ClientSession", lambda timeout: _Session())
    reminder = _make_reset_reminder(
        lead_minutes=60,
        next_scheduled_post_utc=dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc),
        emoji_name_or_id="https://cdn.example.invalid/not-image",
    )

    channel, _updates = _run_reset_process(
        monkeypatch, reminder, dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc)
    )

    assert channel.sent[0]["content"] == "<@&999>"
    assert channel.sent[0]["files"] == []
    assert "non-image or unsupported content type" in caplog.text


def test_missing_emoji_logs_warning_and_posts_without_icon(monkeypatch, caplog) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference,
        lead_minutes=60,
        next_scheduled_post_utc=dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc),
        emoji_name_or_id="missing_icon",
    )
    channel, _updates = _run_reset_process(
        monkeypatch, reminder, dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc)
    )
    assert channel.sent[0]["content"] == "<@&999>"
    assert channel.sent[0]["files"] == []
    assert "reset reminder image emoji could not be resolved" in caplog.text


def test_embed_includes_current_cycle_reset_timestamps_inside_description(
    monkeypatch,
) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference,
        lead_minutes=60,
        next_scheduled_post_utc=dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc),
        embed_description="Reset incoming",
    )
    channel, _updates = _run_reset_process(
        monkeypatch, reminder, dt.datetime(2026, 5, 29, 9, 5, tzinfo=dt.timezone.utc)
    )
    unix_seconds = int(reference.timestamp())
    desc = channel.sent[0]["embed"].description
    assert (
        desc
        == f"Reset incoming\n\nCurrent cycle resets at: <t:{unix_seconds}:F>\nTime left: <t:{unix_seconds}:R>"
    )
    assert f"<t:{unix_seconds}:R>" not in channel.sent[0]["content"]


def test_description_uses_reset_time_not_reminder_time(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference,
        lead_minutes=120,
        next_scheduled_post_utc=dt.datetime(2026, 5, 29, 8, 0, tzinfo=dt.timezone.utc),
    )
    channel, _updates = _run_reset_process(
        monkeypatch, reminder, dt.datetime(2026, 5, 29, 8, 0, tzinfo=dt.timezone.utc)
    )
    reset_ts = int(reference.timestamp())
    reminder_ts = int(
        dt.datetime(2026, 5, 29, 8, 0, tzinfo=dt.timezone.utc).timestamp()
    )
    desc = channel.sent[0]["embed"].description
    assert f"<t:{reset_ts}:F>" in desc
    assert f"<t:{reminder_ts}:F>" not in desc


def test_emoji_resolution_ignores_unrelated_bot_guilds(monkeypatch, caplog) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference,
        lead_minutes=60,
        next_scheduled_post_utc=dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc),
        emoji_name_or_id="shared_icon",
    )
    channel = _GuildChannel([])
    bot = _DummyBot(channel)
    bot.guilds = [SimpleNamespace(emojis=[_FakeEmoji(54321, "shared_icon")])]
    record = scheduler._ResetReminderRecord(row_number=7, reminder=reminder)

    monkeypatch.setattr(
        scheduler,
        "_resolve_target_channel",
        lambda *_args: asyncio.sleep(0, result=channel),
    )
    monkeypatch.setattr(scheduler, "_is_feature_enabled", lambda: True)
    monkeypatch.setattr(
        scheduler,
        "_load_reset_reminder_records",
        lambda *, active_only: asyncio.sleep(
            0,
            result=(
                "ResetTab",
                {
                    "last_sent_for_reset_utc": 14,
                    "next_scheduled_post_utc": 15,
                    "last_message_id": 16,
                },
                [record],
            ),
        ),
    )
    monkeypatch.setattr(
        scheduler, "_update_next_scheduled_post", lambda **kwargs: asyncio.sleep(0)
    )
    monkeypatch.setattr(
        scheduler, "_update_row_after_send", lambda **kwargs: asyncio.sleep(0)
    )

    asyncio.run(
        scheduler.process_reset_reminders(
            bot, now=dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc)
        )
    )

    assert channel.sent[0]["content"] == "<@&999>"
    assert channel.sent[0]["files"] == []
    assert "reset reminder image emoji could not be resolved" in caplog.text


def test_configured_emoji_with_missing_target_guild_logs_warning(
    monkeypatch, caplog
) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference,
        lead_minutes=60,
        next_scheduled_post_utc=dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc),
        emoji_name_or_id="doom_tower_icon",
    )
    channel, _updates = _run_reset_process(
        monkeypatch, reminder, dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc)
    )

    assert channel.sent[0]["content"] == "<@&999>"
    assert "target guild unavailable" in caplog.text


def test_footer_uses_following_cycle_reset_without_embed_timestamp(monkeypatch) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference,
        cycle_days=14,
        lead_minutes=60,
        next_scheduled_post_utc=dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc),
        embed_footer="Configured footer",
    )

    channel, _updates = _run_reset_process(
        monkeypatch, reminder, dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc)
    )

    embed = channel.sent[0]["embed"]
    assert embed.timestamp is None
    assert (
        embed.footer.text
        == "Configured footer • Following cycle reset: 2026-06-12 10:00 UTC"
    )


def test_footer_uses_following_cycle_reset_when_configured_footer_blank(
    monkeypatch,
) -> None:
    reference = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    reminder = _make_reset_reminder(
        reference_date_utc=reference,
        cycle_days=14,
        lead_minutes=60,
        next_scheduled_post_utc=dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc),
        embed_footer="",
    )

    channel, _updates = _run_reset_process(
        monkeypatch, reminder, dt.datetime(2026, 5, 29, 9, 0, tzinfo=dt.timezone.utc)
    )

    embed = channel.sent[0]["embed"]
    assert embed.timestamp is None
    assert embed.footer.text == "Following cycle reset: 2026-06-12 10:00 UTC"
