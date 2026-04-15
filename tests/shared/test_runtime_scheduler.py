import asyncio
import logging
import os

import pytest


os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("GSPREAD_CREDENTIALS", "{}")
os.environ.setdefault("RECRUITMENT_SHEET_ID", "sheet-id")


from modules.common import runtime


def test_scheduler_job_exception_does_not_cancel(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def runner() -> None:
        scheduler = runtime.Scheduler()

        attempt = {"count": 0}

        async def maybe_fail() -> None:
            attempt["count"] += 1
            if attempt["count"] == 1:
                raise RuntimeError("boom")

        def fast_next_run(self, reference=None):
            now = reference or runtime.datetime.now(runtime.timezone.utc)
            return now + runtime.timedelta(milliseconds=10)

        monkeypatch.setattr(runtime._RecurringJob, "_compute_next_run", fast_next_run)

        caplog.set_level(logging.ERROR, logger="c1c.runtime")

        scheduler.every(seconds=1, name="test_job", tag="test").do(maybe_fail)

        await asyncio.sleep(0.05)
        await scheduler.shutdown()

        assert attempt["count"] >= 2
        assert any("recurring job error" in record.getMessage() for record in caplog.records)

    asyncio.run(runner())


def test_scheduler_every_dedupes_by_job_name() -> None:
    scheduler = runtime.Scheduler()
    try:
        first = scheduler.every(seconds=30, name="dupe_job", tag="test")
        second = scheduler.every(seconds=45, name="dupe_job", tag="test")
        assert first is second
    finally:
        asyncio.run(scheduler.shutdown())


def test_scheduler_spawn_dedupes_active_named_task() -> None:
    async def runner() -> None:
        scheduler = runtime.Scheduler()
        started = asyncio.Event()
        release = asyncio.Event()

        async def worker() -> None:
            started.set()
            await release.wait()

        task1 = scheduler.spawn(worker(), name="dupe_task")
        await started.wait()
        task2 = scheduler.spawn(worker(), name="dupe_task")
        assert task1 is task2
        release.set()
        await scheduler.shutdown()

    asyncio.run(runner())
