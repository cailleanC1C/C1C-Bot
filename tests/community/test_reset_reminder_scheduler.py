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


def test_schedule_reset_jobs_is_idempotent() -> None:
    runtime = SimpleNamespace(bot=SimpleNamespace(), scheduler=_FakeScheduler())

    scheduler.schedule_reset_reminder_jobs(runtime)
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
        last_message_id=111,
    )
    record = scheduler._ResetReminderRecord(row_number=7, reminder=reminder)
    updates = []

    async def _load(*, active_only: bool):
        return "ResetTab", {"last_sent_for_reset_utc": 14, "last_message_id": 15}, [record]

    async def _update(**kwargs):
        updates.append(kwargs)

    monkeypatch.setattr(scheduler, "_load_reset_reminder_records", _load)
    monkeypatch.setattr(scheduler, "_update_row_after_send", _update)

    channel = _DummyChannel()

    async def _resolve_target(_bot, _reminder):
        return channel

    monkeypatch.setattr(scheduler, "_resolve_target_channel", _resolve_target)
    bot = _DummyBot(channel)

    asyncio.run(scheduler.process_reset_reminders(bot, now=now))

    assert channel.last_message.deleted is True
    assert len(channel.sent) == 1
    assert channel.sent[0]["content"] == "<@&999>"
    assert len(updates) == 1
    assert updates[0]["row_number"] == 7
