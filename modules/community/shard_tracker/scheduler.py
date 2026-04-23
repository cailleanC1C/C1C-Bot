from __future__ import annotations

import datetime as dt
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from modules.common.runtime import Runtime

log = logging.getLogger("c1c.shards.scheduler")


def schedule_shard_jobs(runtime: "Runtime") -> None:
    if any(getattr(job, "name", None) == "shard_weekly_reminders" for job in runtime.scheduler.jobs):
        return

    job = runtime.scheduler.every(minutes=30.0, tag="shards", name="shard_weekly_reminders")

    async def _runner() -> None:
        if runtime.bot.is_closed() or not runtime.bot.is_ready():
            return
        cog = runtime.bot.get_cog("ShardTracker")
        if cog is None:
            return
        try:
            await cog.process_weekly_clan_reminders(now=dt.datetime.now(dt.timezone.utc))
        except Exception:
            log.exception("shard weekly reminder job failed")

    job.do(_runner)
