from __future__ import annotations

import asyncio
import datetime as dt
import logging
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from c1c_coreops.cronlog import cron_task

from shared.cache import telemetry as cache_telemetry
from shared.cache.telemetry import RefreshResult
from shared.config import get_config_snapshot
from c1c_coreops.cog import resolve_ops_log_channel_id
from shared.sheets.runtime import register_default_cache_buckets

from shared.sheets.cache_service import cache
from modules.common import runtime as rt
from shared.logfmt import LogTemplates, human_reason

UTC = dt.timezone.utc
log = logging.getLogger("c1c.cache.scheduler")


@dataclass(frozen=True)
class _JobSpec:
    bucket: str
    interval: dt.timedelta
    cadence_label: str


_SPEC_BY_BUCKET: Dict[str, _JobSpec] = {}
_REGISTERED: Dict[str, Tuple[Any, asyncio.Task]] = {}

STARTUP_BUCKETS: tuple[tuple[str, float], ...] = (
    ("clans", 0.0),
    ("templates", 6.0),
    ("clan_tags", 12.0),
    ("onboarding_questions", 20.0),
    ("reaction_roles", 28.0),
    ("fusion", 40.0),
    ("fusion_events", 52.0),
)


def _format_exception(exc: BaseException) -> str:
    message = str(exc).strip().strip("\"")
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


def ensure_cache_registration() -> None:
    """Ensure cache buckets from Sheets modules are registered."""

    register_default_cache_buckets()
 

def _safe_bucket(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("bucket name must be non-empty")
    return cleaned


async def _send_ops_message(runtime: "rt.Runtime", message: str) -> None:
    content = message.strip()
    if not content:
        return
    snapshot = get_config_snapshot()
    channel_id = resolve_ops_log_channel_id(bot=runtime.bot, snapshot=snapshot)
    if not channel_id:
        log.info("[cron] ops channel missing: %s", content)
        return
    try:
        await runtime.bot.wait_until_ready()
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    channel = runtime.bot.get_channel(channel_id)
    if channel is None:
        try:
            channel = await runtime.bot.fetch_channel(channel_id)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("[cron] ops channel fetch failed: %s", _format_exception(exc))
            channel = None
    if channel is None:
        log.info("[cron] ops channel unavailable: %s", content)
        return
    try:
        await channel.send(content)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log.warning("[cron] ops channel send failed: %s", _format_exception(exc))


def _format_cache_message(bucket: str, result: RefreshResult) -> str:
    duration_ms = result.duration_ms or 0
    duration_s = duration_ms / 1000.0
    retries = result.retries if (result.retries or 0) > 0 else None
    reason = human_reason(result.error) if not result.ok else None
    return LogTemplates.cache(
        bucket=bucket,
        ok=result.ok,
        duration_s=duration_s,
        retries=retries,
        reason=reason,
    )


async def _run_refresh(runtime: "rt.Runtime", spec: _JobSpec) -> None:
    bucket = _safe_bucket(spec.bucket)
    try:
        result = await cache_telemetry.refresh_now(name=bucket, actor="scheduler")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - defensive guard
        log.exception("[cron] refresh job crashed", extra={"bucket": bucket})
        await _send_ops_message(
            runtime,
            LogTemplates.cache(
                bucket=bucket,
                ok=False,
                duration_s=0.0,
                retries=None,
                reason=human_reason(exc),
            ),
        )
    else:
        await _send_ops_message(runtime, _format_cache_message(bucket, result))


@cron_task("refresh_clans")
async def _cron_refresh_clans(runtime: "rt.Runtime") -> None:
    await _run_refresh(runtime, _SPEC_BY_BUCKET["clans"])


@cron_task("refresh_templates")
async def _cron_refresh_templates(runtime: "rt.Runtime") -> None:
    await _run_refresh(runtime, _SPEC_BY_BUCKET["templates"])


@cron_task("refresh_clan_tags")
async def _cron_refresh_clan_tags(runtime: "rt.Runtime") -> None:
    await _run_refresh(runtime, _SPEC_BY_BUCKET["clan_tags"])


@cron_task("refresh_onboarding_questions")
async def _cron_refresh_onboarding_questions(runtime: "rt.Runtime") -> None:
    await _run_refresh(runtime, _SPEC_BY_BUCKET["onboarding_questions"])


_JOB_HANDLERS = {
    "clans": _cron_refresh_clans,
    "templates": _cron_refresh_templates,
    "clan_tags": _cron_refresh_clan_tags,
    "onboarding_questions": _cron_refresh_onboarding_questions,
}


def _ensure_job(runtime: "rt.Runtime", spec: _JobSpec):
    bucket = _safe_bucket(spec.bucket)
    existing = _REGISTERED.get(bucket)
    if existing:
        job, task = existing
        if task.done():
            _REGISTERED.pop(bucket, None)
        else:
            return job
    interval_seconds = spec.interval.total_seconds()
    job = runtime.scheduler.every(
        seconds=interval_seconds,
        tag="cache",
        name=f"cache_refresh:{bucket}",
    )

    handler = _JOB_HANDLERS[bucket]

    async def runner() -> None:
        await handler(runtime)

    task = job.do(runner)
    _REGISTERED[bucket] = (job, task)
    log.info("[cron] registered job: %s interval=%s", job.name or bucket, spec.cadence_label)
    return job


def register_refresh_job(
    runtime: "rt.Runtime",
    *,
    bucket: str,
    interval: dt.timedelta,
    cadence_label: str,
) -> Tuple[_JobSpec, Any]:
    spec = _JobSpec(bucket=_safe_bucket(bucket), interval=interval, cadence_label=cadence_label)
    _SPEC_BY_BUCKET[spec.bucket] = spec
    job = _ensure_job(runtime, spec)
    return spec, job


def _format_next_run(job: Any) -> str:
    next_run = getattr(job, "next_run", None)
    if next_run is None:
        return "pending"
    try:
        as_utc = next_run.astimezone(UTC)
    except Exception:
        return "pending"
    return as_utc.replace(second=0, microsecond=0).strftime("%Y-%m-%d %H:%M UTC")


async def emit_schedule_log(
    runtime: "rt.Runtime",
    successes: Iterable[Tuple[_JobSpec, Any]],
    failure: Optional[Tuple[str, BaseException]],
) -> None:
    try:
        if failure is not None:
            bucket, exc = failure
            message = LogTemplates.scheduler_failure(
                job=bucket,
                reason=human_reason(exc),
            )
            log.warning(message)
            await _send_ops_message(runtime, message)
            return
        success_list = list(successes)
        if not success_list:
            log.info("[cron] no cache refresh jobs registered")
            return
        intervals = {spec.bucket: spec.cadence_label for spec, _ in success_list}
        upcoming = {spec.bucket: _format_next_run(job) for spec, job in success_list}
        message = LogTemplates.scheduler(intervals=intervals, upcoming=upcoming)
        log.info(message)
        await _send_ops_message(runtime, message)
    except asyncio.CancelledError:
        raise
    except Exception:  # pragma: no cover - defensive guard
        log.exception("failed to emit cron schedule log")


def schedule_default_jobs(runtime: "rt.Runtime") -> None:
    """Legacy helper that schedules the standard cache refresh jobs."""

    ensure_cache_registration()
    specs = (
        _JobSpec(bucket="clans", interval=dt.timedelta(hours=3), cadence_label="3h"),
        _JobSpec(bucket="templates", interval=dt.timedelta(days=7), cadence_label="7d"),
        _JobSpec(bucket="clan_tags", interval=dt.timedelta(days=7), cadence_label="7d"),
        _JobSpec(
            bucket="onboarding_questions",
            interval=dt.timedelta(days=7),
            cadence_label="7d",
        ),
    )
    successes: List[Tuple[_JobSpec, Any]] = []
    failure: Optional[Tuple[str, BaseException]] = None
    for spec in specs:
        try:
            _SPEC_BY_BUCKET[spec.bucket] = spec
            job = _ensure_job(runtime, spec)
        except Exception as exc:  # pragma: no cover - defensive guard
            log.exception("failed to schedule cache refresh", extra={"bucket": spec.bucket})
            if failure is None:
                failure = (spec.bucket, exc)
            continue
        successes.append((spec, job))
    runtime.scheduler.spawn(
        emit_schedule_log(runtime, successes, failure),
        name="cache_refresh_schedule_log",
    )


async def preload_on_startup() -> None:
    """Synchronously refresh core cache buckets during startup."""

    ensure_cache_registration()
    last_delay = 0.0
    for name, delay_sec in STARTUP_BUCKETS:
        bucket = _safe_bucket(name)
        wait_sec = max(0.0, delay_sec - last_delay)
        if wait_sec > 0:
            await asyncio.sleep(wait_sec)
        last_delay = max(last_delay, delay_sec)
        try:
            await cache.refresh_now(bucket, actor="startup")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive guard
            log.warning(
                LogTemplates.cache(
                    bucket=bucket,
                    ok=False,
                    duration_s=0.0,
                    retries=None,
                    reason=human_reason(exc),
                )
            )
