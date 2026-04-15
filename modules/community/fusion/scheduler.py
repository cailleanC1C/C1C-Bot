from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

from modules.community.fusion.announcement_refresh import process_fusion_announcement_refreshes
from modules.community.fusion.reminders import process_fusion_reminders
from modules.community.fusion.role_cleanup import process_ended_fusion_role_cleanup

if TYPE_CHECKING:
    from modules.common.runtime import Runtime

log = logging.getLogger("c1c.community.fusion.scheduler")
_NOT_READY_LOG_INTERVAL_SEC = 300.0
_last_not_ready_log_at: dt.datetime | None = None


def _should_log_not_ready(now: dt.datetime) -> bool:
    global _last_not_ready_log_at
    if _last_not_ready_log_at is None:
        _last_not_ready_log_at = now
        return True
    if (now - _last_not_ready_log_at).total_seconds() >= _NOT_READY_LOG_INTERVAL_SEC:
        _last_not_ready_log_at = now
        return True
    return False


def schedule_fusion_jobs(runtime: "Runtime") -> None:
    if any(getattr(job, "name", None) == "fusion_reminders" for job in runtime.scheduler.jobs):
        log.info("fusion scheduler already registered; skipping duplicate job")
        return

    job = runtime.scheduler.every(minutes=1.0, tag="fusion", name="fusion_reminders")

    async def _runner() -> None:
        if runtime.bot.is_closed() or not runtime.bot.is_ready():
            now = dt.datetime.now(dt.timezone.utc)
            if _should_log_not_ready(now):
                log.info("fusion scheduler paused; bot not ready")
            return
        try:
            await process_fusion_reminders(runtime.bot)
            await process_fusion_announcement_refreshes(runtime.bot)
            await process_ended_fusion_role_cleanup(runtime.bot)
        except Exception:
            log.exception("fusion reminder scheduler tick failed")

    job.do(_runner)
