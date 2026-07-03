from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Tuple

if TYPE_CHECKING:
    from modules.common.runtime import Runtime

log = logging.getLogger("c1c.community.leagues.scheduler")


def _parse_time(value: str | None, *, default: Tuple[int, int]) -> Tuple[int, int]:
    if not value:
        return default
    token = value.strip()
    if not token:
        return default
    parts = token.split(":", 1)
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) > 1 else 0
    except (TypeError, ValueError):
        return default
    hour = max(0, min(23, hour))
    minute = max(0, min(59, minute))
    return hour, minute


def _build_cron(env_key: str, fallback: Tuple[int, int], weekday: str) -> str:
    hour, minute = _parse_time(os.getenv(env_key), default=fallback)
    return f"{minute} {hour} * * {weekday}"


def schedule_leagues_jobs(runtime: "Runtime") -> None:
    monday_cron = _build_cron("LEAGUES_REMINDER_MONDAY_UTC", (13, 0), "1")
    wednesday_cron = _build_cron("LEAGUES_REMINDER_WEDNESDAY_UTC", (13, 0), "3")

    def _resolve_cog():
        return runtime.bot.get_cog("LeaguesCog")

    log.info("league reminder scheduled", extra={"weekday": "monday", "cron": monday_cron})
    log.info("league approval prompt scheduled", extra={"weekday": "wednesday", "cron": wednesday_cron})
    monday_job = runtime.scheduler.cron(
        monday_cron, tag="leagues_reminder", name="leagues_reminder_monday"
    )
    wednesday_job = runtime.scheduler.cron(
        wednesday_cron, tag="leagues_reminder", name="leagues_reminder_wednesday"
    )

    async def monday_runner() -> None:
        cog = _resolve_cog()
        if cog is None:
            log.warning("Leagues cog missing; Monday reminder skipped")
            return
        await cog.send_monday_reminder()

    async def wednesday_runner() -> None:
        cog = _resolve_cog()
        if cog is None:
            log.warning("Leagues cog missing; Wednesday reminder skipped")
            return
        await cog.send_wednesday_reminder()

    monday_job.do(monday_runner)
    wednesday_job.do(wednesday_runner)
