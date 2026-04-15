import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

from modules.community.fusion import scheduler as fusion_scheduler


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


def test_schedule_fusion_jobs_is_idempotent() -> None:
    runtime = SimpleNamespace(bot=SimpleNamespace(), scheduler=_FakeScheduler())

    fusion_scheduler.schedule_fusion_jobs(runtime)
    fusion_scheduler.schedule_fusion_jobs(runtime)

    assert len(runtime.scheduler.jobs) == 1
    assert runtime.scheduler.jobs[0].name == "fusion_reminders"


def test_fusion_scheduler_pauses_when_bot_not_ready(
    monkeypatch,
    caplog,
) -> None:
    runtime = SimpleNamespace(
        bot=SimpleNamespace(is_closed=lambda: False, is_ready=lambda: False),
        scheduler=_FakeScheduler(),
    )
    reminders = AsyncMock()
    refresh = AsyncMock()
    cleanup = AsyncMock()
    monkeypatch.setattr(fusion_scheduler, "process_fusion_reminders", reminders)
    monkeypatch.setattr(fusion_scheduler, "process_fusion_announcement_refreshes", refresh)
    monkeypatch.setattr(fusion_scheduler, "process_ended_fusion_role_cleanup", cleanup)
    monkeypatch.setattr(fusion_scheduler, "_last_not_ready_log_at", None)

    fusion_scheduler.schedule_fusion_jobs(runtime)
    runner = runtime.scheduler.jobs[0]._runner
    assert runner is not None

    caplog.set_level(logging.INFO, logger="c1c.community.fusion.scheduler")
    asyncio.run(runner())

    reminders.assert_not_awaited()
    refresh.assert_not_awaited()
    cleanup.assert_not_awaited()
    assert "fusion scheduler paused; bot not ready" in caplog.text
