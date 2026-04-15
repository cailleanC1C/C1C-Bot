from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from modules.community.fusion.reminders import process_fusion_reminders

if TYPE_CHECKING:
    from modules.common.runtime import Runtime

log = logging.getLogger("c1c.community.fusion.scheduler")


def schedule_fusion_jobs(runtime: "Runtime") -> None:
    job = runtime.scheduler.every(minutes=1.0, tag="fusion", name="fusion_reminders")

    async def _runner() -> None:
        try:
            await process_fusion_reminders(runtime.bot)
        except Exception:
            log.exception("fusion reminder scheduler tick failed")

    job.do(_runner)
