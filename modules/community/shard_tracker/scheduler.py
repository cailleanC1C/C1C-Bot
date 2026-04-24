from __future__ import annotations

import datetime as dt
import logging
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modules.common.runtime import Runtime

log = logging.getLogger("c1c.shards.scheduler")


def _log_scheduler_task_exit(task: asyncio.Task[None]) -> None:
    try:
        exc = task.exception()
    except asyncio.CancelledError:
        log.info("shard reminder scheduler cancelled")
        return
    except Exception:
        log.exception("shard reminder scheduler failed while reading task exception")
        return

    if exc is not None:
        log.exception("shard reminder scheduler failed", exc_info=exc)
        return

    log.warning("shard reminder scheduler stopped")


def schedule_shard_jobs(runtime: "Runtime") -> None:
    if any(getattr(job, "name", None) == "shard_weekly_reminders" for job in runtime.scheduler.jobs):
        log.info("shard reminder scheduler already registered; skipping duplicate job")
        return

    job = runtime.scheduler.every(minutes=30.0, tag="shards", name="shard_weekly_reminders")
    next_run = getattr(job, "next_run", None)
    log.info(
        "shard reminder scheduler started",
        extra={
            "job_name": getattr(job, "name", "shard_weekly_reminders"),
            "interval_seconds": int(job.interval.total_seconds()),
            "next_run": next_run.isoformat() if hasattr(next_run, "isoformat") else None,
            "job_count": len(runtime.scheduler.jobs),
        },
    )

    async def _runner() -> None:
        log.info("shard reminder scheduler tick started")
        if runtime.bot.is_closed() or not runtime.bot.is_ready():
            return
        cog = runtime.bot.get_cog("ShardTracker")
        if cog is None:
            return
        try:
            await cog.process_weekly_clan_reminders(
                now=dt.datetime.now(dt.timezone.utc),
                source="scheduler",
            )
        except Exception:
            log.exception("shard weekly reminder job failed")

    task = job.do(_runner)
    log.info(
        "shard reminder scheduler registration sanity",
        extra={
            "job_name": getattr(job, "name", "shard_weekly_reminders"),
            "interval_seconds": int(job.interval.total_seconds()),
            "next_run": job.next_run.isoformat() if job.next_run else None,
            "job_count": len(runtime.scheduler.jobs),
        },
    )
    if isinstance(task, asyncio.Task):
        task.add_done_callback(_log_scheduler_task_exit)
