import asyncio
import logging
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

from modules.community.shard_tracker import scheduler as shard_scheduler


class _FakeJob:
    def __init__(self, *, name: str) -> None:
        self.name = name
        self._runner = None
        self.interval = timedelta(minutes=30)
        self.next_run = None

    def do(self, runner):
        self._runner = runner
        return None


class _FakeScheduler:
    def __init__(self) -> None:
        self.jobs = []

    def every(self, **kwargs):
        job = _FakeJob(name=kwargs.get("name", ""))
        self.jobs.append(job)
        return job


def test_schedule_shard_jobs_is_idempotent(caplog) -> None:
    runtime = SimpleNamespace(bot=SimpleNamespace(), scheduler=_FakeScheduler())

    caplog.set_level(logging.INFO, logger="c1c.shards.scheduler")
    shard_scheduler.schedule_shard_jobs(runtime)
    shard_scheduler.schedule_shard_jobs(runtime)

    assert len(runtime.scheduler.jobs) == 1
    assert runtime.scheduler.jobs[0].name == "shard_weekly_reminders"
    assert "shard reminder scheduler started" in caplog.text
    assert "shard reminder scheduler already registered" in caplog.text


def test_shard_scheduler_tick_runs_weekly_reminder(caplog) -> None:
    reminder = AsyncMock()
    runtime = SimpleNamespace(
        bot=SimpleNamespace(
            is_closed=lambda: False,
            is_ready=lambda: True,
            get_cog=lambda _name: SimpleNamespace(process_weekly_clan_reminders=reminder),
        ),
        scheduler=_FakeScheduler(),
    )

    shard_scheduler.schedule_shard_jobs(runtime)
    runner = runtime.scheduler.jobs[0]._runner
    assert runner is not None

    caplog.set_level(logging.INFO, logger="c1c.shards.scheduler")
    asyncio.run(runner())

    reminder.assert_awaited_once()
    _, kwargs = reminder.await_args
    assert kwargs.get("source") == "scheduler"
    assert "shard reminder scheduler tick started" in caplog.text


def test_log_scheduler_task_exit_logs_clean_stop(caplog) -> None:
    class _DoneTask:
        def exception(self):
            return None

    caplog.set_level(logging.WARNING, logger="c1c.shards.scheduler")
    shard_scheduler._log_scheduler_task_exit(_DoneTask())

    assert "shard reminder scheduler stopped" in caplog.text
