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

    def spawn(self, coro, **_kwargs):
        coro.close()


def test_schedule_fusion_jobs_is_idempotent() -> None:
    runtime = SimpleNamespace(bot=SimpleNamespace(), scheduler=_FakeScheduler())

    fusion_scheduler.schedule_fusion_jobs(runtime)
    fusion_scheduler.schedule_fusion_jobs(runtime)

    assert len(runtime.scheduler.jobs) == 1
    assert runtime.scheduler.jobs[0].name == "fusion_daily_reconcile"
    assert runtime.scheduler.jobs[0].name != "fusion_reminders"


def test_fusion_scheduler_registers_daily_reconciliation(
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
    monkeypatch.setattr(
        fusion_scheduler, "process_fusion_announcement_refreshes", refresh
    )
    monkeypatch.setattr(fusion_scheduler, "process_ended_fusion_role_cleanup", cleanup)
    fusion_scheduler.schedule_fusion_jobs(runtime)
    assert runtime.scheduler.jobs[0].name == "fusion_daily_reconcile"
    assert runtime.scheduler.jobs[0].cadence_label == "daily reconcile"
