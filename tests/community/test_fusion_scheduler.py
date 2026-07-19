import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.community.fusion import scheduler as fusion_scheduler


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


def _runtime():
    return SimpleNamespace(bot=SimpleNamespace(), scheduler=_FakeScheduler())


def _fusion(now, *, fusion_id="fusion", start_delta=-1, end_delta=1):
    return SimpleNamespace(
        fusion_id=fusion_id,
        start_at_utc=now + dt.timedelta(hours=start_delta),
        end_at_utc=now + dt.timedelta(hours=end_delta),
    )


def _patch_fusion_data(monkeypatch, *, target=None, published=(), events=None):
    async def get_target():
        return target

    async def get_published():
        return list(published)

    async def get_events(_fusion_id):
        return list(events or [])

    monkeypatch.setattr(
        fusion_scheduler.fusion_sheets, "get_publishable_fusion", get_target
    )
    monkeypatch.setattr(
        fusion_scheduler.fusion_sheets, "get_published_fusions", get_published
    )
    monkeypatch.setattr(fusion_scheduler.fusion_sheets, "get_fusion_events", get_events)


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


def test_grouped_overdue_catchup_backs_off_when_still_due(monkeypatch) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    target = _fusion(now)
    runtime = _runtime()
    _patch_fusion_data(monkeypatch, target=target)

    async def settings(**_kwargs):
        return SimpleNamespace(group_events=True, grouped_post_time_utc="00:00")

    async def sent_keys(_target):
        return set(), True

    monkeypatch.setattr(
        fusion_scheduler.fusion_sheets, "get_fusion_reminder_settings", settings
    )
    monkeypatch.setattr(fusion_scheduler, "_load_grouped_sent_keys", sent_keys)
    monkeypatch.setattr(
        fusion_scheduler,
        "_next_grouped_due",
        lambda **_kwargs: dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1),
    )
    process = AsyncMock()
    monkeypatch.setattr(fusion_scheduler, "process_fusion_reminders", process)

    asyncio.run(fusion_scheduler.reconcile_fusion_jobs(runtime))
    job = next(
        job for job in runtime.scheduler.jobs if job.name == "fusion_grouped_reminders"
    )
    assert job.next_run <= dt.datetime.now(dt.timezone.utc)
    asyncio.run(job._runner())

    process.assert_awaited_once()
    assert job.next_run >= dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        minutes=14, seconds=59
    )


def test_cleanup_catches_up_ended_then_schedules_future_end(monkeypatch) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    ended = _fusion(now, fusion_id="ended", start_delta=-2, end_delta=-1)
    future = _fusion(now, fusion_id="future", end_delta=2)
    runtime = _runtime()
    _patch_fusion_data(monkeypatch, published=(ended, future))
    cleanup = AsyncMock()
    monkeypatch.setattr(fusion_scheduler, "process_ended_fusion_role_cleanup", cleanup)

    asyncio.run(fusion_scheduler.reconcile_fusion_jobs(runtime))
    job = next(
        job for job in runtime.scheduler.jobs if job.name == "fusion_role_cleanup"
    )
    assert job.next_run <= dt.datetime.now(dt.timezone.utc)
    asyncio.run(job._runner())

    cleanup.assert_awaited_once()
    assert job.next_run == future.end_at_utc


def test_announcement_refresh_uses_event_boundaries(monkeypatch) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    parent = _fusion(now, start_delta=-24, end_delta=24)
    event_start = now + dt.timedelta(hours=2)
    event_end = now + dt.timedelta(hours=3)
    event = SimpleNamespace()
    runtime = _runtime()
    _patch_fusion_data(monkeypatch, published=(parent,), events=(event,))
    monkeypatch.setattr(
        fusion_scheduler.fusion_sheets,
        "get_valid_event_timing",
        lambda _event, **_kwargs: (event_start, event_end),
    )

    asyncio.run(fusion_scheduler.reconcile_fusion_jobs(runtime))
    job = next(
        job
        for job in runtime.scheduler.jobs
        if job.name == "fusion_announcement_refresh"
    )
    assert job.next_run == event_start
    assert job.next_run not in {parent.start_at_utc, parent.end_at_utc}


def test_announcement_refresh_keeps_earlier_daily_refresh(monkeypatch) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    parent = _fusion(now, start_delta=-24, end_delta=72)
    event = SimpleNamespace()
    event_start = now + dt.timedelta(days=2)
    event_end = event_start + dt.timedelta(hours=1)
    runtime = _runtime()
    _patch_fusion_data(monkeypatch, published=(parent,), events=(event,))
    monkeypatch.setattr(
        fusion_scheduler.fusion_sheets,
        "get_valid_event_timing",
        lambda _event, **_kwargs: (event_start, event_end),
    )

    asyncio.run(fusion_scheduler.reconcile_fusion_jobs(runtime))
    job = next(
        job
        for job in runtime.scheduler.jobs
        if job.name == "fusion_announcement_refresh"
    )
    assert job.next_run == fusion_scheduler._next_daily(now)


def test_grouped_callback_failure_rearms_due_job(monkeypatch) -> None:
    now = dt.datetime.now(dt.timezone.utc)
    target = _fusion(now)
    runtime = _runtime()
    _patch_fusion_data(monkeypatch, target=target)

    async def settings(**_kwargs):
        return SimpleNamespace(group_events=True, grouped_post_time_utc="00:00")

    async def sent_keys(_target):
        return set(), True

    monkeypatch.setattr(
        fusion_scheduler.fusion_sheets, "get_fusion_reminder_settings", settings
    )
    monkeypatch.setattr(fusion_scheduler, "_load_grouped_sent_keys", sent_keys)
    monkeypatch.setattr(fusion_scheduler, "_next_grouped_due", lambda **_kwargs: now)
    monkeypatch.setattr(
        fusion_scheduler,
        "process_fusion_reminders",
        AsyncMock(side_effect=RuntimeError("unexpected")),
    )

    asyncio.run(fusion_scheduler.reconcile_fusion_jobs(runtime))
    job = next(
        job for job in runtime.scheduler.jobs if job.name == "fusion_grouped_reminders"
    )
    with pytest.raises(RuntimeError, match="unexpected"):
        asyncio.run(job._runner())

    assert job.next_run is not None
    assert job.next_run >= dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        minutes=14, seconds=59
    )
