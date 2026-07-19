from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING, Awaitable, Callable

from modules.community.fusion.announcement_refresh import (
    process_fusion_announcement_refreshes,
)
from modules.community.fusion.reminders import (
    _load_grouped_sent_keys,
    _next_grouped_due,
    _parse_grouped_post_time_utc,
    process_fusion_reminders,
)
from modules.community.fusion.role_cleanup import process_ended_fusion_role_cleanup
from shared.sheets import fusion as fusion_sheets

if TYPE_CHECKING:
    from modules.common.runtime import Runtime

log = logging.getLogger("c1c.community.fusion.scheduler")
_DUE_JOB_RETRY_DELAY = dt.timedelta(minutes=15)


def _next_daily(now: dt.datetime, *, hour: int = 0, minute: int = 15) -> dt.datetime:
    candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return candidate if candidate > now else candidate + dt.timedelta(days=1)


async def reconcile_fusion_jobs(
    runtime: "Runtime", *, include_cleanup_catchup: bool = True
) -> None:
    """Rebuild event-driven Fusion due times from the current cached sheet data."""
    now = dt.datetime.now(dt.timezone.utc)
    target = await fusion_sheets.get_publishable_fusion()
    published = await fusion_sheets.get_published_fusions()

    specs: list[tuple[str, dt.datetime | None, str, Callable[[], Awaitable[None]]]] = []
    if target is not None:
        settings = await fusion_sheets.get_fusion_reminder_settings(now=now)
        post_time = _parse_grouped_post_time_utc(settings.grouped_post_time_utc)
        if settings.group_events and post_time is not None and target.end_at_utc > now:
            sent_keys, _ = await _load_grouped_sent_keys(target)
            if sent_keys is not None:
                due = _next_grouped_due(
                    now=now, post_time=post_time, sent_keys=sent_keys
                )
                if target.start_at_utc <= due <= target.end_at_utc:
                    specs.append(
                        (
                            "fusion_grouped_reminders",
                            due,
                            "grouped daily",
                            lambda: process_fusion_reminders(runtime.bot),
                        )
                    )

    if published:
        event_boundaries: list[dt.datetime] = []
        for row in published:
            events = await fusion_sheets.get_fusion_events(row.fusion_id)
            for event in events:
                timing = fusion_sheets.get_valid_event_timing(
                    event, for_helper="fusion_announcement_refresh_scheduler"
                )
                if timing is not None:
                    event_boundaries.extend(value for value in timing if value > now)
        refresh_due = min(event_boundaries) if event_boundaries else _next_daily(now)
        specs.append(
            (
                "fusion_announcement_refresh",
                refresh_due,
                "daily/event boundary",
                lambda: process_fusion_announcement_refreshes(runtime.bot),
            )
        )
        ended = [row for row in published if row.end_at_utc <= now]
        future_end_times = [row.end_at_utc for row in published if row.end_at_utc > now]
        cleanup_due = (
            now
            if ended and include_cleanup_catchup
            else (min(future_end_times) if future_end_times else None)
        )
        if cleanup_due is not None:
            specs.append(
                (
                    "fusion_role_cleanup",
                    cleanup_due,
                    "event end",
                    lambda: process_ended_fusion_role_cleanup(runtime.bot),
                )
            )

    for name, due, label, callback in specs:
        job = runtime.scheduler.at(
            due, tag="fusion", component="community", name=name, cadence_label=label
        )

        async def runner(job=job, callback=callback, name=name) -> None:
            await callback()
            await reconcile_fusion_jobs(
                runtime,
                include_cleanup_catchup=name != "fusion_role_cleanup",
            )
            if name == "fusion_grouped_reminders":
                completed_at = dt.datetime.now(dt.timezone.utc)
                next_due = job.next_run
                if next_due is not None and next_due <= completed_at:
                    job.reschedule(completed_at + _DUE_JOB_RETRY_DELAY)

        job.do(runner)


def schedule_fusion_jobs(runtime: "Runtime") -> None:
    """Register daily reconciliation and asynchronously discover relevant jobs."""
    if any(
        getattr(job, "name", None) == "fusion_daily_reconcile"
        for job in runtime.scheduler.jobs
    ):
        return
    reconcile = runtime.scheduler.every(
        hours=24, tag="fusion", name="fusion_daily_reconcile", component="community"
    )
    reconcile.cadence_label = "daily reconcile"
    reconcile.do(lambda: reconcile_fusion_jobs(runtime))
    runtime.scheduler.spawn(
        reconcile_fusion_jobs(runtime), name="fusion_initial_reconcile"
    )


__all__ = ["schedule_fusion_jobs", "reconcile_fusion_jobs"]
