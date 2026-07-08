"""Application runtime scaffolding for the unified bot process."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import logging
import math
import os
import random
import re
import time
from datetime import datetime, time as dt_time, timedelta, timezone
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Iterable, Optional, Sequence

import discord
from aiohttp import web
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from discord.ext import commands
from shared.sheets.core import is_rate_limited_error as is_sheets_rate_limited_error

from shared import health as healthmod
from shared import socket_heartbeat as hb
from shared import watchdog as watchdog_loop
from shared.sheets import core as sheets_core
from modules.common.logs import channel_label, log as human_log
from shared import config as shared_config
from shared.config import (
    get_env_name,
    get_bot_name,
    get_watchdog_check_sec,
    get_watchdog_stall_sec,
    get_watchdog_disconnect_grace_sec,
    get_log_channel_id,
    get_refresh_times,
    get_refresh_timezone,
    get_strict_emoji_proxy,
)
from shared.logfmt import fmt_duration
from shared.ports import get_port
from shared.logging import get_trace_id, set_trace_id, setup_logging
from shared.obs.events import refresh_bucket_results
from c1c_coreops.helpers import audit_tiers, rehydrate_tiers
from shared.web_routes import mount_emoji_pad
from . import keepalive

import modules.onboarding as onboarding_pkg
from modules.community import COMMUNITY_EXTENSIONS

log = logging.getLogger("c1c.runtime")

_ACTIVE_RUNTIME: "Runtime | None" = None


@dataclass(slots=True)
class StartupPreloadReport:
    rows: list[dict[str, object]]
    total_s: float
    error: str | None = None


_PRELOAD_TASK: asyncio.Task[StartupPreloadReport] | None = None
_web_app: web.Application | None = None
_CF_RAY_RE = re.compile(r"Ray ID:\s*([A-Za-z0-9-]+)", re.IGNORECASE)

_DISCORD_LOGIN_RETRY_INITIAL_SEC = 60
_DISCORD_LOGIN_RETRY_CAP_SEC = 900
_DISCORD_LOGIN_RETRY_MAX_ATTEMPTS = 5
_DISCORD_LOGIN_RETRY_MAX_WINDOW_SEC = 1800
_DISCORD_LOGIN_RETRY_JITTER_RATIO = 0.2


class StartupPhaseError(RuntimeError):
    """Raised when a non-discord startup phase fails."""

    def __init__(self, phase: str, cause: BaseException) -> None:
        super().__init__(f"startup phase failed: {phase}: {cause}")
        self.phase = phase
        self.__cause__ = cause


async def create_app(*, runtime: "Runtime | None" = None) -> web.Application:
    """Create and configure the aiohttp application used by the runtime."""

    static_fields = {"env": get_env_name(), "bot": get_bot_name()}
    access_logger = setup_logging(
        static_fields=static_fields,
        access_logger_name="aiohttp.access",
        access_static_fields={"env": static_fields["env"], "bot": static_fields["bot"]},
    )

    healthmod.set_component("runtime", True)

    @web.middleware
    async def tracing_middleware(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        trace = set_trace_id()
        started = time.perf_counter()
        status = 500
        try:
            response = await handler(request)
            status = getattr(response, "status", status)
            try:
                response.headers["X-Trace-Id"] = trace
            except Exception:  # pragma: no cover - defensive guard
                pass
            return response
        finally:
            duration_ms = int((time.perf_counter() - started) * 1000)
            access_logger.info(
                "http_request",
                extra={
                    "trace": trace,
                    "path": request.path,
                    "method": request.method,
                    "status": status,
                    "ms": duration_ms,
                },
            )

    app = web.Application(middlewares=[tracing_middleware])

    mount_emoji_pad(app)
    strict_proxy_flag = "1" if get_strict_emoji_proxy() else "0"
    log.info("web: /emoji-pad mounted (STRICT_EMOJI_PROXY=%s)", strict_proxy_flag)

    async def root(_: web.Request) -> web.Response:
        payload = {
            "ok": True,
            "bot": get_bot_name(),
            "env": get_env_name(),
            "version": os.getenv("BOT_VERSION", "dev"),
            "trace": get_trace_id(),
        }
        return web.json_response(payload)

    async def ready(_: web.Request) -> web.Response:
        components = healthmod.components_snapshot()
        ok = healthmod.overall_ready()
        return web.json_response({"ok": ok, "components": components})

    async def _health_payload() -> tuple[dict[str, Any], bool]:
        if runtime is None:
            payload = {
                "ok": True,
                "bot": get_bot_name(),
                "env": get_env_name(),
                "version": os.getenv("BOT_VERSION", "dev"),
            }
            return payload, True
        return await runtime._health_payload()

    async def health(_: web.Request) -> web.Response:
        base_payload, healthy = await _health_payload()
        components = healthmod.components_snapshot()
        components_ok = all(item.get("ok", False) for item in components.values())
        ready_ok = healthmod.overall_ready()
        payload = dict(base_payload)
        payload.update(
            {
                "ok": bool(healthy and components_ok),
                "components": components,
                "ready": ready_ok,
                "endpoint": "health",
            }
        )
        status = 200 if payload["ok"] else 503
        return web.json_response(payload, status=status)

    async def healthz(_: web.Request) -> web.Response:
        payload, healthy = await _health_payload()
        payload = dict(payload)
        payload["endpoint"] = "healthz"
        status = 200 if healthy else 503
        return web.json_response(payload, status=status)

    async def _keepalive_handler(_: web.Request) -> web.Response:
        return web.Response(text="ok", status=200)

    app.router.add_get("/", root)
    app.router.add_get("/ready", ready)
    app.router.add_get("/health", health)
    app.router.add_get("/healthz", healthz)
    app.router.add_get(keepalive.route_path(), _keepalive_handler)

    return app


async def _startup_preload(bot: commands.Bot | None = None) -> StartupPreloadReport:
    await asyncio.sleep(15)

    runtime = get_active_runtime()
    if bot is None and runtime is not None:
        bot = runtime.bot

    if bot is None:  # pragma: no cover - defensive guard
        log.warning("Cache preloader aborted: bot unavailable")
        return StartupPreloadReport(rows=[], total_s=0.0, error="bot unavailable")

    from shared.cache import telemetry as cache_telemetry
    from c1c_coreops.render import RefreshEmbedRow

    bucket_names = cache_telemetry.list_buckets()
    if not bucket_names:
        log.info("Cache preloader skipped: no cache buckets registered")
        return StartupPreloadReport(
            rows=[], total_s=0.0, error="no cache buckets registered"
        )

    rows: list[RefreshEmbedRow] = []
    total_ms = 0
    fallback_lines: list[str] = []
    refresh_results: list[cache_telemetry.RefreshResult] = []

    for index, name in enumerate(bucket_names):
        if index > 0:
            await asyncio.sleep(8)
        try:
            result = await cache_telemetry.refresh_now(
                name=name,
                actor="startup",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive guard
            quota = is_sheets_rate_limited_error(exc)
            log.exception(
                "startup preload refresh failed",
                extra={"bucket": name, "quota_exhausted": quota, "exception_type": type(exc).__name__, "exception_message": str(exc)},
            )
            await send_log_message(f"❌ Startup refresh failed for {name}: {'quota_exhausted' if quota else exc}")
            if quota:
                break
            continue

        snapshot = result.snapshot
        duration_ms = result.duration_ms or 0
        total_ms += duration_ms

        raw_result = snapshot.last_result or ("ok" if result.ok else "fail")
        display_result = raw_result.replace("_", " ").strip() or "-"
        normalized = raw_result.lower()
        retries_flag = "1" if normalized in {"retry_ok", "fail"} else "0"

        error_text = result.error or snapshot.last_error or "-"
        cleaned_error = " ".join(str(error_text).split()) if error_text else "-"
        if len(cleaned_error) > 70:
            cleaned_error = f"{cleaned_error[:67]}…"

        label = name or "-"
        ttl_display = "?"
        if snapshot.ttl_expired is True:
            ttl_display = "yes"
        elif snapshot.ttl_expired is False:
            ttl_display = "no"

        count_display = "-"
        if snapshot.item_count is not None:
            count_display = str(snapshot.item_count)

        refresh_results.append(result)

        rows.append(
            RefreshEmbedRow(
                bucket=label,
                duration=f"{duration_ms} ms",
                result=display_result,
                retries=retries_flag,
                ttl_expired=ttl_display,
                count=count_display,
                error=cleaned_error or "-",
            )
        )

        fallback_lines.append(
            f"{label}: {display_result} · {duration_ms} ms · error={cleaned_error or '-'}"
        )

    if not rows:
        log.info("Cache preloader completed with no rows")
        return StartupPreloadReport(rows=[], total_s=0.0, error="no refresh rows")

    startup_rows: list[dict[str, object]] = []
    for bucket in refresh_bucket_results(refresh_results):
        details: list[str] = [
            f"{bucket.duration_s:.1f}s",
            str(bucket.item_count if bucket.item_count is not None else "?"),
        ]
        if bucket.ttl_ok is True:
            details.append("ttl")
        if bucket.ttl_expired_before_refresh is True:
            details.append("ttl_expired")
        if bucket.currently_stale_after_refresh is True:
            details.append("refresh_failed")
        if bucket.cache_age_s is not None:
            details.append(f"age={fmt_duration(bucket.cache_age_s)}")
        if bucket.ttl_s is not None:
            details.append(f"ttl={fmt_duration(bucket.ttl_s)}")
        startup_rows.append(
            {
                "name": bucket.name,
                "status": bucket.status,
                "detail_parts": details,
            }
        )
    if bot is not None:
        setattr(bot, "_startup_refresh_rows", startup_rows)
        setattr(bot, "_startup_refresh_total_s", total_ms / 1000.0)

    log.info("Cache preloader completed")
    return StartupPreloadReport(rows=startup_rows, total_s=total_ms / 1000.0)


def set_active_runtime(runtime: "Runtime | None") -> None:
    """Set the active runtime used by module-level helpers."""

    global _ACTIVE_RUNTIME
    _ACTIVE_RUNTIME = runtime


def get_active_runtime() -> "Runtime | None":
    """Return the active runtime instance if one has been registered."""

    return _ACTIVE_RUNTIME


def schedule_startup_preload(
    bot: commands.Bot | None = None,
) -> asyncio.Task[StartupPreloadReport]:
    """Ensure the startup cache preload task has been scheduled and return it."""

    global _PRELOAD_TASK
    task = _PRELOAD_TASK
    if task is not None and not task.done():
        return task
    if task is not None and task.done():
        try:  # pragma: no cover - defensive logging
            task.result()
        except Exception:
            log.debug(
                "previous cache preloader task completed with error", exc_info=True
            )
    _PRELOAD_TASK = asyncio.create_task(
        _startup_preload(bot), name="cache_startup_preload"
    )
    return _PRELOAD_TASK


async def send_log_message(message: str) -> None:
    """Proxy to the active runtime's log channel helper, if available."""

    runtime = get_active_runtime()
    if runtime is None:
        return
    try:
        await runtime.send_log_message(message)
    except Exception:
        log.warning("failed to send log message (non-fatal)", exc_info=True)


async def recreate_http_app() -> None:
    """Restart the aiohttp application when an active runtime is available."""

    runtime = get_active_runtime()
    if runtime is None:
        log.debug("recreate_http_app skipped: no active runtime")
        return
    await runtime.shutdown_webserver()
    await runtime.start_webserver()


def monotonic_ms() -> int:
    """Return a monotonic millisecond timestamp for lightweight timing."""

    return int(time.monotonic() * 1000)


def _extract_cloudflare_ray_id(raw_text: object) -> str | None:
    text = str(raw_text or "")
    if not text:
        return None
    match = _CF_RAY_RE.search(text)
    if match:
        return match.group(1)
    return None


def _is_startup_rate_limited(exc: discord.HTTPException) -> tuple[bool, str]:
    status = getattr(exc, "status", None)
    text = str(getattr(exc, "text", "") or "")
    text_lower = text.lower()

    has_429 = status == 429 or "429" in text_lower or "too many requests" in text_lower
    has_1015 = "1015" in text_lower or "temporarily banned" in text_lower
    has_cloudflare = "cloudflare" in text_lower

    if not (has_429 or has_1015):
        return False, "-"

    ray_id = _extract_cloudflare_ray_id(text)
    if has_cloudflare or ray_id:
        return True, f"cloudflare_rate_limited(ray_id={ray_id or 'unknown'})"
    return True, "discord_rate_limited"


async def _sleep_with_shutdown_poll(bot: commands.Bot, delay_sec: int) -> None:
    remaining = max(0, int(delay_sec))
    while remaining > 0:
        if bot.is_closed():
            return
        step = min(5, remaining)
        await asyncio.sleep(step)
        remaining -= step


async def _sleep_startup_retry_backoff(delay_sec: int) -> None:
    await asyncio.sleep(max(0, int(delay_sec)))


def _startup_phase_log(phase: str, status: str, **extra: object) -> None:
    payload = {"phase": phase, "status": status}
    if extra:
        payload.update(extra)
    if status == "fail":
        log.error("startup phase %s %s", phase, status, extra=payload)
    else:
        log.info("startup phase %s %s", phase, status, extra=payload)


def _is_retryable_discord_start_failure(exc: BaseException) -> tuple[bool, str]:
    if isinstance(exc, discord.LoginFailure):
        return False, "login_failure"
    if isinstance(exc, asyncio.TimeoutError):
        return True, "timeout"
    if isinstance(exc, OSError):
        return True, "os_error"
    if isinstance(exc, discord.HTTPException):
        should_retry, detail = _is_startup_rate_limited(exc)
        if should_retry:
            if detail.startswith("cloudflare_rate_limited"):
                return False, detail
            return True, detail
        status = getattr(exc, "status", None)
        if status is not None and int(status) >= 500:
            return True, f"http_{status}"
        return False, f"http_{status or 'unknown'}"
    if isinstance(exc, discord.ConnectionClosed):
        return True, "connection_closed"
    if isinstance(exc, discord.GatewayNotFound):
        return True, "gateway_not_found"
    return False, exc.__class__.__name__


def _bot_http_session(bot: commands.Bot) -> Any | None:
    http = getattr(bot, "http", None)
    if http is None:
        return None
    return getattr(http, "_HTTPClient__session", None)


def _is_bot_http_session_closed(bot: commands.Bot) -> bool:
    session = _bot_http_session(bot)
    return bool(getattr(session, "closed", False))


def _parse_times(parts: Iterable[str]) -> list[dt_time]:
    times: list[dt_time] = []
    for raw in parts:
        item = (raw or "").strip()
        if not item:
            continue
        try:
            hour_str, minute_str = item.split(":", 1)
            hour = int(hour_str)
            minute = int(minute_str)
        except (ValueError, TypeError):
            log.warning("invalid refresh time entry skipped", extra={"entry": raw})
            continue
        if 0 <= hour < 24 and 0 <= minute < 60:
            times.append(dt_time(hour=hour, minute=minute))
        else:
            log.warning("refresh time out of range", extra={"entry": raw})
    # dedupe while preserving order
    seen: set[tuple[int, int]] = set()
    ordered: list[dt_time] = []
    for t in sorted(times):
        key = (t.hour, t.minute)
        if key in seen:
            continue
        seen.add(key)
        ordered.append(t)
    return ordered


def _resolve_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        log.warning("timezone not found; defaulting to UTC", extra={"timezone": name})
        return ZoneInfo("UTC")


def _next_run(now: datetime, schedule: Sequence[dt_time]) -> datetime:
    if not schedule:
        return now + timedelta(minutes=5)
    today = now.date()
    for entry in schedule:
        candidate = datetime.combine(today, entry, tzinfo=now.tzinfo)
        if candidate > now:
            return candidate
    tomorrow = today + timedelta(days=1)
    return datetime.combine(tomorrow, schedule[0], tzinfo=now.tzinfo)


def _trim_message(message: str, *, limit: int = 1800) -> str:
    message = message.strip()
    if len(message) <= limit:
        return message
    return f"{message[: limit - 1]}…"


async def resolve_configured_message_channel(
    ctx: commands.Context,
    *,
    bot: discord.Client,
    channel_id: int | str | None,
    expected_guild: discord.Guild | None = None,
) -> tuple[discord.abc.Messageable | object | None, bool]:
    """Resolve a configured channel with a safe fallback to the invoking context."""

    snowflake: int | None = None
    try:
        snowflake = int(channel_id) if channel_id is not None else None
    except (TypeError, ValueError):
        snowflake = None

    channel: object | None = None
    if snowflake:
        channel = bot.get_channel(snowflake)
        if channel is None:
            fetch_channel = getattr(bot, "fetch_channel", None)
            if callable(fetch_channel):
                try:
                    channel = await fetch_channel(snowflake)
                except discord.HTTPException:
                    channel = None
        if channel is not None and expected_guild is not None:
            channel_guild = getattr(channel, "guild", None)
            if channel_guild is not None and channel_guild != expected_guild:
                log.warning(
                    "configured channel belongs to another guild",
                    extra={
                        "channel_id": snowflake,
                        "channel_guild": getattr(channel_guild, "id", None),
                        "expected_guild": getattr(expected_guild, "id", None),
                    },
                )
                channel = None
        if channel is not None and not hasattr(channel, "send"):
            log.warning(
                "configured channel is not messageable",
                extra={"channel_id": snowflake, "type": type(channel).__name__},
            )
            channel = None

    if channel is not None:
        return channel, False

    fallback = getattr(ctx, "channel", None)
    if fallback is not None and hasattr(fallback, "send"):
        return fallback, True

    return None, True


async def resolve_configured_text_channel(
    bot: discord.Client,
    *,
    channel_id: int | str | None,
    logger: logging.Logger,
    context: str,
    invalid_reason: str = "invalid_channel",
) -> tuple[discord.TextChannel | None, str | None]:
    """Resolve a configured text channel using the strict server map pattern."""

    snowflake: int | None = None
    try:
        snowflake = int(channel_id) if channel_id is not None else None
    except (TypeError, ValueError):
        snowflake = None

    if not snowflake:
        return None, "missing_channel_id"

    try:
        channel = bot.get_channel(snowflake) or await bot.fetch_channel(snowflake)
    except discord.HTTPException:
        logger.exception(
            f"failed to resolve {context} channel", extra={"channel_id": snowflake}
        )
        return None, "channel_fetch_failed"

    if not isinstance(channel, discord.TextChannel):
        label = channel_label(getattr(channel, "guild", None), snowflake)
        logger.warning(
            f"{context} channel is not a text channel", extra={"channel": label}
        )
        return None, invalid_reason

    return channel, None


@dataclass(frozen=True)
class CronSchedule:
    minutes: set[int]
    hours: set[int]
    days: set[int]
    months: set[int]
    weekdays: set[int]
    dom_any: bool
    dow_any: bool


def _parse_cron_field(field: str, minimum: int, maximum: int) -> set[int]:
    allowed: set[int] = set()
    parts = [part.strip() for part in field.split(",") if part.strip()]
    if not parts:
        return set(range(minimum, maximum + 1))

    for part in parts:
        step = 1
        base = part
        if "/" in part:
            base, step_text = part.split("/", 1)
            try:
                step = max(1, int(step_text))
            except (TypeError, ValueError):
                continue
        base = base.strip()
        if base in {"*", ""}:
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            try:
                start, end = int(start_text), int(end_text)
            except (TypeError, ValueError):
                continue
        else:
            try:
                value = int(base)
            except (TypeError, ValueError):
                continue
            start = end = value

        start = max(minimum, start)
        end = min(maximum, end)
        if start > end:
            continue

        for value in range(start, end + 1, step):
            allowed.add(value)

    return allowed if allowed else set(range(minimum, maximum + 1))


def _parse_cron_expression(expression: str) -> CronSchedule:
    fields = [field for field in expression.split() if field]
    if len(fields) != 5:
        raise ValueError("cron expression must have exactly 5 fields")

    minutes = _parse_cron_field(fields[0], 0, 59)
    hours = _parse_cron_field(fields[1], 0, 23)
    days = _parse_cron_field(fields[2], 1, 31)
    months = _parse_cron_field(fields[3], 1, 12)
    weekdays = _parse_cron_field(fields[4], 0, 7)
    dom_any = fields[2].strip() == "*"
    dow_any = fields[4].strip() == "*"
    if 7 in weekdays:
        weekdays.add(0)
        weekdays.discard(7)
    return CronSchedule(
        minutes=minutes,
        hours=hours,
        days=days,
        months=months,
        weekdays=weekdays,
        dom_any=dom_any,
        dow_any=dow_any,
    )


def _cron_matches(candidate: datetime, schedule: CronSchedule) -> bool:
    weekday = (candidate.weekday() + 1) % 7
    minute_match = candidate.minute in schedule.minutes
    hour_match = candidate.hour in schedule.hours
    month_match = candidate.month in schedule.months

    dom_match = candidate.day in schedule.days
    dow_match = weekday in schedule.weekdays

    if schedule.dom_any and schedule.dow_any:
        day_ok = True
    elif schedule.dom_any and not schedule.dow_any:
        day_ok = dow_match
    elif schedule.dow_any and not schedule.dom_any:
        day_ok = dom_match
    else:
        day_ok = dom_match or dow_match

    return minute_match and hour_match and month_match and day_ok


def _next_cron_run(
    schedule: CronSchedule,
    reference: datetime | None = None,
) -> datetime:
    cursor = (reference or datetime.now(timezone.utc)).replace(second=0, microsecond=0)
    cursor = cursor + timedelta(minutes=1)
    deadline = cursor + timedelta(days=366)

    while cursor <= deadline:
        if _cron_matches(cursor, schedule):
            return cursor
        cursor += timedelta(minutes=1)

    raise ValueError("unable to compute next cron run within 1 year")


class _RecurringJob:
    def __init__(
        self,
        scheduler: "Scheduler",
        *,
        interval: timedelta,
        jitter: str | float | None = None,
        tag: str | None = None,
        name: str | None = None,
        component: str | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._interval = interval
        self._jitter = jitter
        self.tag = tag
        self.name = name
        self.component = component or "default"
        self.next_run: datetime | None = None

    @property
    def interval(self) -> timedelta:
        return self._interval

    def _pick_jitter(self) -> float:
        if self._jitter == "small":
            window = min(60.0, self._interval.total_seconds() * 0.05)
            if window <= 0:
                return 0.0
            return random.uniform(-window, window)
        if isinstance(self._jitter, (int, float)):
            window = abs(float(self._jitter))
            if window <= 0:
                return 0.0
            return random.uniform(-window, window)
        return 0.0

    def _compute_next_run(self, reference: datetime | None = None) -> datetime:
        now = reference or datetime.now(timezone.utc)
        interval_seconds = max(1.0, self._interval.total_seconds())
        # Align to UTC boundaries with optional jitter.
        cycles = math.floor(now.timestamp() / interval_seconds)
        base_seconds = (cycles + 1) * interval_seconds
        candidate = datetime.fromtimestamp(base_seconds, tz=timezone.utc)
        jitter_offset = self._pick_jitter()
        if jitter_offset:
            candidate = candidate + timedelta(seconds=jitter_offset)
        if candidate <= now:
            candidate = now + timedelta(seconds=1)
        return candidate

    async def _sleep_until_due(self) -> None:
        if self.next_run is None:
            self.next_run = self._compute_next_run()
        while True:
            assert self.next_run is not None
            now = datetime.now(timezone.utc)
            delay = (self.next_run - now).total_seconds()
            if delay <= 0:
                break
            await asyncio.sleep(min(delay, 60.0))

    def do(self, job: Callable[[], Awaitable[None]]) -> asyncio.Task:
        self.next_run = self._compute_next_run()

        async def runner() -> None:
            while True:
                await self._sleep_until_due()
                try:
                    await job()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception(
                        "recurring job error",
                        extra={
                            "job_name": self.name or getattr(job, "__name__", "job"),
                            "tag": self.tag,
                        },
                    )
                finally:
                    self.next_run = self._compute_next_run()

        task_name = self.name or getattr(job, "__name__", "recurring_job")
        return self._scheduler.spawn(runner(), name=task_name)


class _CronJob:
    def __init__(
        self,
        scheduler: "Scheduler",
        *,
        expression: str,
        tag: str | None = None,
        name: str | None = None,
    ) -> None:
        self._scheduler = scheduler
        self._fields = _parse_cron_expression(expression)
        self.tag = tag
        self.name = name
        self.next_run: datetime | None = None

    def _compute_next_run(self, reference: datetime | None = None) -> datetime:
        return _next_cron_run(self._fields, reference)

    async def _sleep_until_due(self) -> None:
        if self.next_run is None:
            self.next_run = self._compute_next_run()
        while True:
            assert self.next_run is not None
            now = datetime.now(timezone.utc)
            delay = (self.next_run - now).total_seconds()
            if delay <= 0:
                break
            await asyncio.sleep(min(delay, 60.0))

    def do(self, job: Callable[[], Awaitable[None]]) -> asyncio.Task:
        self.next_run = self._compute_next_run()

        async def runner() -> None:
            while True:
                await self._sleep_until_due()
                try:
                    await job()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception(
                        "cron job error",
                        extra={
                            "job_name": self.name or getattr(job, "__name__", "job"),
                            "tag": self.tag,
                        },
                    )
                finally:
                    self.next_run = self._compute_next_run(datetime.now(timezone.utc))

        task_name = self.name or getattr(job, "__name__", "cron_job")
        return self._scheduler.spawn(runner(), name=task_name)


class Scheduler:
    """Very small asyncio task supervisor for background jobs."""

    def __init__(self) -> None:
        self._tasks: list[asyncio.Task] = []
        self._jobs: list[_RecurringJob] = []

    def spawn(self, coro: Awaitable, *, name: Optional[str] = None) -> asyncio.Task:
        if name is not None:
            for existing in self._tasks:
                if existing.done():
                    continue
                if existing.get_name() == name:
                    try:
                        coro.close()
                    except Exception:
                        pass
                    log.debug(
                        "scheduler spawn skipped duplicate task",
                        extra={"task_name": name},
                    )
                    return existing
        if name is not None:
            task = asyncio.create_task(coro, name=name)
        else:
            task = asyncio.create_task(coro)
        self._tasks.append(task)
        return task

    def cron(
        self, expression: str, *, tag: str | None = None, name: str | None = None
    ) -> _CronJob:
        return _CronJob(self, expression=expression, tag=tag, name=name)

    def every(
        self,
        *,
        hours: float = 0.0,
        minutes: float = 0.0,
        seconds: float = 0.0,
        jitter: str | float | None = None,
        tag: str | None = None,
        name: str | None = None,
        component: str | None = None,
    ) -> _RecurringJob:
        if name is not None:
            for existing in self._jobs:
                if existing.name == name:
                    log.debug(
                        "scheduler registration skipped duplicate job",
                        extra={"job_name": name},
                    )
                    return existing
        total_seconds = float(hours) * 3600.0 + float(minutes) * 60.0 + float(seconds)
        if total_seconds <= 0:
            total_seconds = 60.0
        interval = timedelta(seconds=total_seconds)
        job = _RecurringJob(
            self,
            interval=interval,
            jitter=jitter,
            tag=tag,
            name=name,
            component=component,
        )
        self._jobs.append(job)
        return job

    @property
    def jobs(self) -> list[_RecurringJob]:
        return list(self._jobs)

    async def shutdown(self) -> None:
        for task in self._tasks:
            if task.done():
                continue
            task.cancel()
        for task in self._tasks:
            if task.done():
                continue
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # pragma: no cover - best-effort cleanup
                log.exception("scheduler task error during shutdown")


class Runtime:
    """Container object that wires the bot, health server, and scheduler."""

    def __init__(
        self,
        bot: commands.Bot,
        *,
        bot_factory: Callable[[], commands.Bot] | None = None,
        bot_rebuild_hook: Callable[[commands.Bot], None] | None = None,
    ) -> None:
        self.bot = bot
        self._bot_factory = bot_factory
        self._bot_rebuild_hook = bot_rebuild_hook
        self.scheduler = Scheduler()
        self._web_app: Optional[web.Application] = None
        self._web_runner: Optional[web.AppRunner] = None
        self._web_site: Optional[web.TCPSite] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._watchdog_params: Optional[tuple[int, int, int]] = None
        self._startup_scheduler_registered = False
        self._startup_scheduler_lock = asyncio.Lock()
        self._startup_diag: dict[str, Any] = {}
        set_active_runtime(self)

    def _reset_startup_diag(self, *, attempt: int) -> None:
        self._startup_diag = {
            "attempt": attempt,
            "phase": "init",
            "client_created": False,
            "startup_setup_ok": False,
            "login_reached": False,
            "ready_reached": False,
            "persistent_views_registered": False,
            "scheduler_start_reached": False,
            "feature_init_started": False,
            "cleanup_ok": None,
        }

    def startup_diag_mark(self, **fields: object) -> None:
        self._startup_diag.update(fields)

    def startup_diag_snapshot(self) -> dict[str, Any]:
        return dict(self._startup_diag)

    def _build_bot_for_attempt(self, startup_attempt: int) -> commands.Bot:
        if startup_attempt <= 1:
            self.startup_diag_mark(client_created=True)
            return self.bot
        if self._bot_factory is None:
            raise RuntimeError(
                "startup retry requested a new bot/client but no bot_factory is configured"
            )
        new_bot = self._bot_factory()
        self.bot = new_bot
        if self._bot_rebuild_hook is not None:
            self._bot_rebuild_hook(new_bot)
        self.startup_diag_mark(client_created=True)
        return new_bot

    async def _dispose_bot_for_attempt(self, bot: commands.Bot) -> None:
        try:
            await bot.close()
            self.startup_diag_mark(cleanup_ok=True)
        except Exception:
            self.startup_diag_mark(cleanup_ok=False)
            log.exception("failed to dispose startup attempt bot/client")

    async def start_webserver(self, *, port: Optional[int] = None) -> None:
        if self._web_site is not None:
            return
        port = port or get_port()

        global _web_app
        if _web_app is not None:
            try:
                await _web_app.shutdown()
            except Exception:
                pass
            try:
                await _web_app.cleanup()
            except Exception:
                pass
            _web_app = None

        app = await create_app(runtime=self)

        self._web_app = app
        _web_app = app
        self._web_runner = web.AppRunner(app)
        await self._web_runner.setup()
        self._web_site = web.TCPSite(self._web_runner, host="0.0.0.0", port=port)
        await self._web_site.start()
        human_log.human("info", f"web server listening • port={port}")
        log.info("web server listening", extra={"port": port})

    async def _health_payload(self) -> tuple[dict, bool]:
        stall = get_watchdog_stall_sec()
        keepalive = get_watchdog_check_sec()
        snapshot = hb.snapshot()
        age = snapshot.last_event_age
        healthy = age <= stall
        payload = {
            "ok": healthy,
            "bot": get_bot_name(),
            "env": get_env_name(),
            "version": os.getenv("BOT_VERSION", "dev"),
            "age_seconds": round(age, 3),
            "stall_after_sec": stall,
            "keepalive_sec": keepalive,
            "connected": snapshot.connected,
            "disconnect_age": (
                None
                if snapshot.disconnect_age is None
                else round(snapshot.disconnect_age, 3)
            ),
            "last_ready_age": (
                None
                if snapshot.last_ready_age is None
                else round(snapshot.last_ready_age, 3)
            ),
        }
        return payload, healthy

    async def shutdown_webserver(self) -> None:
        site, runner, app = self._web_site, self._web_runner, self._web_app
        self._web_site = None
        self._web_runner = None
        self._web_app = None
        global _web_app
        if _web_app is app:
            _web_app = None
        if site is not None:
            await site.stop()
        if runner is not None:
            await runner.cleanup()

    async def start_health_server(self) -> None:
        await self.start_webserver()

    async def shutdown_health_server(self) -> None:
        await self.shutdown_webserver()

    async def send_log_message(self, message: str) -> None:
        try:
            channel_id = get_log_channel_id()
            if not channel_id:
                return
            content = _trim_message(str(message))
            if not content:
                return
            await self.bot.wait_until_ready()
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                channel = await self.bot.fetch_channel(channel_id)
            await channel.send(content)
        except Exception:
            log.warning("failed to send log message (non-fatal)", exc_info=True)

    def schedule_startup_preload(self) -> asyncio.Task[StartupPreloadReport]:
        return schedule_startup_preload(self.bot)

    def watchdog(
        self,
        *,
        check_sec: Optional[int] = None,
        stall_sec: Optional[int] = None,
        disconnect_grace: Optional[int] = None,
        delay_sec: float = 0.0,
    ) -> tuple[bool, int, int, int]:
        check = check_sec if check_sec is not None else get_watchdog_check_sec()
        stall = stall_sec or get_watchdog_stall_sec()
        disconnect = disconnect_grace or get_watchdog_disconnect_grace_sec(stall)

        task = self._watchdog_task
        if task is not None and not task.done():
            return False, check, stall, disconnect
        if task is not None and task.done():
            try:
                exc = task.exception()
            except asyncio.CancelledError:
                exc = None
            if exc:
                log.error(
                    "previous watchdog task exited",
                    exc_info=(type(exc), exc, exc.__traceback__),
                )

        async def runner() -> None:
            if delay_sec > 0:
                await asyncio.sleep(delay_sec)
            await watchdog_loop.run(
                hb.age_seconds,
                stall_after_sec=stall,
                check_every=check,
                state_probe=hb.snapshot,
                disconnect_grace_sec=disconnect,
                latency_probe=lambda: getattr(self.bot, "latency", None),
            )

        self._watchdog_task = self.scheduler.spawn(runner(), name="watchdog")
        self._watchdog_params = (check, stall, disconnect)
        log.info(
            "watchdog loop started",
            extra={"interval": check, "stall": stall, "disconnect_grace": disconnect},
        )
        return True, check, stall, disconnect

    def schedule_at_times(
        self,
        callback: Callable[[], Awaitable[Optional[str]]],
        *,
        times: Optional[Iterable[str]] = None,
        timezone: Optional[str] = None,
        name: str = "scheduled_task",
    ) -> asyncio.Task:
        times_list = _parse_times(times or get_refresh_times())
        if not times_list:
            log.warning(
                "no valid refresh times supplied; defaulting to hourly schedule"
            )
            times_list = [dt_time(hour=0, minute=0)]
        tz_name = timezone or get_refresh_timezone()
        tz = _resolve_timezone(tz_name)

        async def runner() -> None:
            log.info(
                "scheduled runner active",
                extra={
                    "name": name,
                    "times": [f"{t.hour:02d}:{t.minute:02d}" for t in times_list],
                    "timezone": tz_name,
                },
            )
            while True:
                now = datetime.now(tz)
                next_at = _next_run(now, times_list)
                delay = max(1.0, (next_at - now).total_seconds())
                await asyncio.sleep(delay)
                try:
                    result = await callback()
                except Exception as exc:
                    log.exception("scheduled task error", extra={"job_name": name})
                    await self.send_log_message(f"❌ {name} failed: {exc}")
                else:
                    if result:
                        await self.send_log_message(str(result))

        return self.scheduler.spawn(runner(), name=name)

    async def load_extensions(self) -> None:
        """Load all feature modules into the shared bot instance."""

        from c1c_coreops import cog as coreops_cog
        from cogs import app_admin
        from cogs import housekeeping_mirralith
        from cogs import housekeeping_c1c_ad
        from cogs import recruitment_clan_ads
        from modules.housekeeping import cleanup as housekeeping_cleanup_commands
        from modules.onboarding import ops_check as onboarding_ops_check
        from modules.onboarding import reaction_fallback as onboarding_reaction_fallback
        from modules.onboarding import watcher_welcome as onboarding_welcome
        from modules.onboarding import watcher_promo as onboarding_promo
        from modules.onboarding import cmd_resume as onboarding_cmd_resume
        from modules.onboarding import (
            cmd_finishplacement as onboarding_cmd_finishplacement,
        )
        from modules.ops import permissions_ui as ops_permissions
        from c1c_coreops import ops as ops_cog

        await coreops_cog.setup(self.bot)
        await app_admin.setup(self.bot)

        from modules.common import feature_flags as features

        try:
            await features.refresh()
        except Exception:
            log.exception("feature toggle refresh failed")
        else:
            try:
                shared_config.update_feature_flags_snapshot(features.values())
            except Exception:
                log.exception("feature toggle snapshot update failed")

        toggles = shared_config.features

        await onboarding_pkg.setup(self.bot)

        if toggles.mirralith_overview_enabled:
            await housekeeping_mirralith.setup(self.bot)
            log.info("modules: mirralith_overview enabled")
        else:
            log.info("modules: mirralith_overview disabled")

        await housekeeping_c1c_ad.setup(self.bot)
        log.info("modules: c1c_ad command registered")

        await recruitment_clan_ads.setup(self.bot)
        log.info("modules: clan_ads command registered")

        await housekeeping_cleanup_commands.setup(self.bot)
        log.info("modules: housekeeping cleanup command registered")

        async def _load_feature_module(
            module_path: str, feature_keys: Sequence[str]
        ) -> None:
            enabled_keys = [key for key in feature_keys if features.is_enabled(key)]
            if not enabled_keys:
                extra_info = {
                    "feature_module": module_path,
                    "feature_keys": list(feature_keys),
                }
                if len(extra_info["feature_keys"]) == 1:
                    extra_info["feature_key"] = extra_info["feature_keys"][0]
                log.info(
                    "feature toggles disabled; skipping module",
                    extra=extra_info,
                )
                return

            try:
                module = importlib.import_module(module_path)
            except Exception as exc:  # pragma: no cover - defensive guard
                extra_info = {
                    "feature_module": module_path,
                    "feature_keys": enabled_keys,
                }
                if len(extra_info["feature_keys"]) == 1:
                    extra_info["feature_key"] = extra_info["feature_keys"][0]
                log.exception(
                    "failed to import feature module",
                    extra=extra_info,
                )
                try:
                    await self.send_log_message(
                        f"❌ Failed to import {module_path}: {exc}"
                    )
                except Exception:
                    pass
                raise

            setup = getattr(module, "setup", None)
            if setup is None:
                extra_info = {
                    "feature_module": module_path,
                    "feature_keys": enabled_keys,
                }
                if len(extra_info["feature_keys"]) == 1:
                    extra_info["feature_key"] = extra_info["feature_keys"][0]
                log.warning(
                    "feature module missing setup()",
                    extra=extra_info,
                )
                return

            try:
                result = setup(self.bot)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # pragma: no cover - defensive guard
                extra_info = {
                    "feature_module": module_path,
                    "feature_keys": enabled_keys,
                }
                if len(extra_info["feature_keys"]) == 1:
                    extra_info["feature_key"] = extra_info["feature_keys"][0]
                log.exception(
                    "feature module setup failed",
                    extra=extra_info,
                )
                try:
                    await self.send_log_message(f"❌ {module_path}.setup failed: {exc}")
                except Exception:
                    pass
                raise

            extra_info = {
                "feature_module": module_path,
                "feature_keys": enabled_keys,
            }
            if len(extra_info["feature_keys"]) == 1:
                extra_info["feature_key"] = extra_info["feature_keys"][0]
            log.debug(
                "feature module loaded",
                extra=extra_info,
            )

        await _load_feature_module(
            "modules.recruitment.services.search", ("member_panel", "recruiter_panel")
        )
        await _load_feature_module("cogs.recruitment_member", ("member_panel",))
        await _load_feature_module("cogs.recruitment_recruiter", ("recruiter_panel",))

        if features.is_enabled("clan_profile"):
            from cogs import recruitment_clan_profile

            await recruitment_clan_profile.setup(self.bot)
            log.info("modules: clan_profile enabled")
        else:
            log.info("modules: clan_profile disabled")

        from cogs import clanrole_management

        await clanrole_management.setup(self.bot)
        log.info("modules: clanrole_management enabled")
        await _load_feature_module("cogs.recruitment_welcome", ("recruitment_welcome",))
        from cogs import recruitment_open_spots

        await recruitment_open_spots.setup(self.bot)
        log.info("modules: recruitment_open_spots enabled")
        await _load_feature_module(
            "modules.recruitment.reports", ("recruitment_reports",)
        )
        await _load_feature_module(
            "modules.placement.target_select", ("placement_target_select",)
        )
        await _load_feature_module(
            "modules.placement.reservations",
            ("feature_reservations", "placement_reservations"),
        )
        await _load_feature_module(
            "modules.placement.reservation_jobs",
            ("feature_reservations", "placement_reservations"),
        )

        await onboarding_ops_check.setup(self.bot)
        if toggles.welcome_watcher_enabled:
            await onboarding_reaction_fallback.setup(self.bot)
            await onboarding_welcome.setup(self.bot)
            log.info("modules: onboarding_welcome enabled")
        else:
            log.info("modules: onboarding_welcome disabled")

        if toggles.promo_watcher_enabled:
            await onboarding_promo.setup(self.bot)
            log.info("modules: onboarding_promo enabled")
        else:
            log.info("modules: onboarding_promo disabled")

        if toggles.resume_command_enabled:
            await onboarding_cmd_resume.setup(self.bot)  # registers !onb resume
            log.info("modules: onboarding_resume enabled")
        else:
            log.info("modules: onboarding_resume disabled")

        if toggles.welcome_watcher_enabled or toggles.promo_watcher_enabled:
            await onboarding_cmd_finishplacement.setup(self.bot)
            log.info("modules: onboarding_finishplacement enabled")
        else:
            log.info("modules: onboarding_finishplacement disabled")

        await ops_cog.setup(self.bot)

        await ops_permissions.setup(self.bot)
        if toggles.ops_permissions_enabled:
            log.info("modules: ops_permissions enabled")
        else:
            log.info("modules: ops_permissions disabled")

        # === Always-on internal extensions (admin-gated debug/ops commands) ===
        ALWAYS_EXTENSIONS = ("modules.coreops.cmd_cfg",)
        for ext in ALWAYS_EXTENSIONS:
            try:
                await self.bot.load_extension(ext)
            except Exception as exc:
                human_log.human(
                    "error",
                    "feature module load failed",
                    feature_module=ext,
                    feature_key="always_on",
                    error=str(exc),
                )
                raise
            else:
                human_log.human(
                    "debug",
                    "feature module loaded",
                    feature_module=ext,
                    feature_key="always_on",
                )

        for ext in COMMUNITY_EXTENSIONS:
            try:
                await self.bot.load_extension(ext)
            except Exception as exc:
                human_log.human(
                    "warn",
                    "feature module load failed",
                    feature_module=ext,
                    feature_key="community",
                    error=str(exc),
                )
                continue
            else:
                human_log.human(
                    "debug",
                    "feature module loaded",
                    feature_module=ext,
                    feature_key="community",
                )

        # (Refresh commands now live directly in the CoreOps cog.)

    async def start(self, token: str) -> None:
        await self.start_webserver()
        max_attempts = 3

        for startup_attempt in range(1, max_attempts + 1):
            self._reset_startup_diag(attempt=startup_attempt)

            attempt_bot = self._build_bot_for_attempt(startup_attempt)
            log.info("startup attempt %s created new bot/client", startup_attempt)

            await asyncio.sleep(3)

            if attempt_bot.is_closed():
                raise RuntimeError("startup aborted: bot closed before login")

            log.info("startup attempt %s begin", startup_attempt)
            self.startup_diag_mark(attempt=startup_attempt, phase="startup_setup")

            try:
                await self._run_startup_setup()
            except StartupPhaseError:
                raise

            if _is_bot_http_session_closed(attempt_bot):
                raise RuntimeError(
                    "startup refused: bot HTTP session is already closed before login"
                )

            try:
                self.startup_diag_mark(phase="discord_login")
                _startup_phase_log("discord login", "start", attempt=startup_attempt)

                await attempt_bot.start(token)

                _startup_phase_log("discord login", "ok", attempt=startup_attempt)
                return

            except asyncio.CancelledError:
                raise

            except Exception as exc:
                login_reached = bool(getattr(attempt_bot, "user", None))
                self.startup_diag_mark(login_reached=login_reached)

                should_retry, detail = _is_retryable_discord_start_failure(exc)
                retry_allowed = should_retry and not login_reached and startup_attempt < max_attempts

                _startup_phase_log(
                    "discord login",
                    "fail",
                    attempt=startup_attempt,
                    reason=detail,
                    retry="yes" if retry_allowed else "no",
                )

                if not retry_allowed:
                    log.exception(
                        "startup failed",
                        extra={
                            "startup_diag": self.startup_diag_snapshot(),
                            "retryable": should_retry,
                            "reason": detail,
                        },
                    )
                    raise

                log.warning(
                    "startup login failed; retrying with a fresh bot/client",
                    exc_info=True,
                    extra={
                        "startup_diag": self.startup_diag_snapshot(),
                        "retryable": should_retry,
                        "reason": detail,
                        "attempt": startup_attempt,
                    },
                )
                await self._dispose_bot_for_attempt(attempt_bot)
                await _sleep_startup_retry_backoff(2 ** (startup_attempt - 1))

        raise RuntimeError("startup retry attempts exhausted")

    async def _run_startup_setup(self) -> None:
        _startup_phase_log("config validation", "start")
        try:
            rehydrate_tiers(self.bot)
            audit_tiers(self.bot, log)
            merged = shared_config.merge_onboarding_config_early()
            log.debug("runtime: onboarding config preload merged %d keys", merged)
        except Exception as exc:
            _startup_phase_log("config validation", "fail", error=repr(exc))
            raise StartupPhaseError("config validation", exc) from exc
        _startup_phase_log("config validation", "ok")
        self.startup_diag_mark(phase="extension_load")

        _startup_phase_log("extension load", "start")
        try:
            await self.load_extensions()
        except Exception as exc:
            _startup_phase_log("extension load", "fail", error=repr(exc))
            raise StartupPhaseError("extension load", exc) from exc
        _startup_phase_log("extension load", "ok")
        self.startup_diag_mark(phase="persistent_view_registration")

        _startup_phase_log("persistent view registration", "start")
        try:
            from modules.onboarding.ui import panels as onboarding_panels

            registration = onboarding_panels.register_persistent_views(self.bot)
            if not bool(registration.get("registered")):
                error = registration.get("error") or "unknown"
                raise RuntimeError(f"persistent view registration failed: {error}")
            self.startup_diag_mark(persistent_views_registered=True)
        except Exception as exc:
            _startup_phase_log("persistent view registration", "fail", error=repr(exc))
            raise StartupPhaseError("persistent view registration", exc) from exc
        _startup_phase_log("persistent view registration", "ok")
        self.startup_diag_mark(startup_setup_ok=True)

    async def register_ready_schedulers(self) -> None:
        async with self._startup_scheduler_lock:
            if self._startup_scheduler_registered:
                return
            _startup_phase_log("scheduler registration", "start")
            try:
                await self._register_ready_schedulers_inner()
            except Exception as exc:
                _startup_phase_log("scheduler registration", "fail", error=repr(exc))
                raise StartupPhaseError("scheduler registration", exc) from exc
            self._startup_scheduler_registered = True
            _startup_phase_log("scheduler registration", "ok")

    async def _register_cleanup_scheduler(
        self,
        *,
        toggles: Any,
        successes: list[tuple[Any, Any]],
        housekeeping_cleanup: Any,
    ) -> None:
        cleanup_logger = logging.getLogger("c1c.housekeeping.cleanup")
        if not toggles.housekeeping_enabled:
            cleanup_logger.info(
                "housekeeping cleanup disabled via global housekeeping feature toggle"
            )
            return

        try:
            cleanup_config = await housekeeping_cleanup.resolve_cleanup_config_async(
                cleanup_logger
            )
        except Exception as exc:
            if sheets_core._is_rate_limited_error(exc):
                cleanup_logger.warning(
                    "cleanup scheduler config resolve hit Google Sheets quota/backoff; "
                    "skipping cleanup registration for this ready cycle without failing startup: %s",
                    exc,
                )
                return
            cleanup_logger.exception(
                "cleanup scheduler config resolve failed; skipping cleanup registration without failing startup"
            )
            return
        if cleanup_config is None or not cleanup_config.enabled:
            return

        cleanup_job = self.scheduler.every(
            hours=float(cleanup_config.run_every_hours),
            tag="cleanup",
            name="cleanup_watcher",
        )

        async def cleanup_runner() -> None:
            tick_message = (
                "cleanup watcher tick: startup_validation=false writeback=true"
            )
            cleanup_logger.info(tick_message)
            try:
                await send_log_message(f"🧹 {tick_message}")
            except asyncio.CancelledError:
                raise
            except Exception:
                cleanup_logger.exception(
                    "cleanup watcher tick notice failed; cleanup still running"
                )
            try:
                await housekeeping_cleanup.run_cleanup(
                    self.bot, cleanup_logger, startup_validation=False, writeback=True
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                cleanup_logger.exception(
                    "cleanup watcher run failed; scheduled cleanup will retry on next tick"
                )
                try:
                    await send_log_message("🧹 cleanup watcher failed; see app logs")
                except asyncio.CancelledError:
                    raise
                except Exception:
                    cleanup_logger.exception("cleanup watcher failure notice failed")

        async def startup_validation_runner() -> None:
            try:
                run_cleanup_kwargs: dict[str, Any] = {
                    "startup_validation": True,
                    "writeback": False,
                }
                try:
                    if "resolved_config" in inspect.signature(
                        housekeeping_cleanup.run_cleanup
                    ).parameters:
                        run_cleanup_kwargs["resolved_config"] = cleanup_config
                except (TypeError, ValueError):
                    run_cleanup_kwargs["resolved_config"] = cleanup_config
                await housekeeping_cleanup.run_cleanup(
                    self.bot, cleanup_logger, **run_cleanup_kwargs
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                cleanup_logger.exception(
                    "cleanup startup validation failed; recurring cleanup remains scheduled"
                )

        cleanup_job.do(cleanup_runner)
        self.scheduler.spawn(
            startup_validation_runner(), name="cleanup_startup_validation"
        )

        next_run = getattr(cleanup_job, "next_run", None)
        next_run_text = (
            next_run.isoformat().replace("+00:00", "Z")
            if isinstance(next_run, datetime)
            else "unknown"
        )
        registration_summary = (
            "cleanup watcher scheduled: "
            f"every={cleanup_config.run_every_hours:g}h "
            f"dry_run={str(cleanup_config.dry_run).lower()} "
            f"tab={cleanup_config.tab_name} "
            f"next_run={next_run_text}"
        )
        cleanup_logger.info(registration_summary)

        async def registration_notice_runner() -> None:
            try:
                await send_log_message(f"🧹 {registration_summary}")
            except asyncio.CancelledError:
                raise
            except Exception:
                cleanup_logger.exception(
                    "cleanup watcher registration notice failed; recurring cleanup remains scheduled"
                )

        self.scheduler.spawn(
            registration_notice_runner(), name="cleanup_registration_notice"
        )
        successes.append(
            (
                SimpleNamespace(
                    bucket="cleanup",
                    cadence_label=f"{cleanup_config.run_every_hours:g}h",
                ),
                cleanup_job,
            )
        )

    def _log_optional_scheduler_quota_skip(
        self,
        *,
        logger: logging.Logger,
        scheduler_name: str,
        config_source: str,
        exc: Exception,
    ) -> None:
        logger.warning(
            "%s scheduler config resolve hit Google Sheets quota/backoff; "
            "skipping %s registration for this ready cycle without failing startup "
            "(config_source=%s exception_type=%s): %s",
            scheduler_name,
            scheduler_name,
            config_source,
            type(exc).__name__,
            exc,
        )

    def _register_optional_scheduler(
        self,
        scheduler_name: str,
        config_source: str,
        callback: Callable[[], Any],
        *,
        logger: logging.Logger | None = None,
    ) -> bool:
        target_logger = logger or log
        try:
            callback()
            return True
        except Exception as exc:
            if sheets_core._is_rate_limited_error(exc):
                self._log_optional_scheduler_quota_skip(
                    logger=target_logger,
                    scheduler_name=scheduler_name,
                    config_source=config_source,
                    exc=exc,
                )
                return False
            raise

    async def _register_ready_schedulers_inner(self) -> None:
        from shared.sheets.cache_scheduler import (
            ensure_cache_registration,
            register_refresh_job,
        )
        from modules.housekeeping import cleanup as housekeeping_cleanup
        from modules.housekeeping import keepalive as housekeeping_keepalive
        from modules.housekeeping import mirralith_overview as housekeeping_mirralith
        from modules.housekeeping import c1c_ad as housekeeping_c1c_ad
        from modules.recruitment import clan_ads as recruitment_clan_ads
        from modules.common import feature_flags
        from shared.sheets import recruitment as recruitment_sheets
        from modules.ops import server_map as server_map_module
        from modules.community.leagues import schedule_leagues_jobs
        from modules.community.fusion.scheduler import schedule_fusion_jobs
        from modules.community.shard_tracker.scheduler import schedule_shard_jobs
        from modules.community.reset_reminders.scheduler import (
            schedule_reset_reminder_jobs,
        )

        toggles = shared_config.features
        ensure_cache_registration()
        # Keep startup registration fast; long-running cache preload happens in
        # the background startup preloader task.
        cache_specs = (
            ("clans", timedelta(hours=3), "3h"),
            ("templates", timedelta(days=7), "7d"),
            ("clan_tags", timedelta(days=7), "7d"),
            ("onboarding_questions", timedelta(days=7), "7d"),
        )
        successes: list[tuple[Any, Any]] = []
        for bucket, interval, cadence in cache_specs:
            spec, job = register_refresh_job(
                self,
                bucket=bucket,
                interval=interval,
                cadence_label=cadence,
            )
            successes.append((spec, job))

        await self._register_cleanup_scheduler(
            toggles=toggles,
            successes=successes,
            housekeeping_cleanup=housekeeping_cleanup,
        )

        mirralith_cron = os.getenv("MIRRALITH_POST_CRON", "").strip()
        if mirralith_cron and toggles.mirralith_overview_enabled:
            mirralith_job = self.scheduler.cron(
                mirralith_cron,
                tag="mirralith_overview",
                name="mirralith_overview",
            )

            async def mirralith_runner() -> None:
                await housekeeping_mirralith.run_mirralith_overview_job(
                    self.bot, trigger="scheduled"
                )

            mirralith_job.do(mirralith_runner)
            successes.append(
                (
                    SimpleNamespace(
                        bucket="mirralith_overview", cadence_label=mirralith_cron
                    ),
                    mirralith_job,
                )
            )
        elif mirralith_cron:
            log.info(
                "Mirralith overview disabled via feature toggle; skipping schedule"
            )
        else:
            log.info("Mirralith overview job disabled; MIRRALITH_POST_CRON is not set.")

        try:
            c1c_refresh_days_raw = await recruitment_sheets.get_config_value_async(
                "C1C_AD_REFRESH_DAYS", None
            )
            c1c_refresh_days = (
                int(c1c_refresh_days_raw) if c1c_refresh_days_raw else None
            )
        except Exception as exc:
            c1c_refresh_days = None
            if sheets_core._is_rate_limited_error(exc):
                self._log_optional_scheduler_quota_skip(
                    logger=log,
                    scheduler_name="c1c_ad",
                    config_source="Config:C1C_AD_REFRESH_DAYS",
                    exc=exc,
                )
            else:
                log.exception("C1C ad refresh interval lookup failed; skipping schedule")

        if c1c_refresh_days and c1c_refresh_days > 0:
            c1c_ad_job = self.scheduler.every(
                hours=float(c1c_refresh_days) * 24.0,
                tag="c1c_ad",
                name="c1c_ad",
            )

            async def c1c_ad_runner() -> None:
                await housekeeping_c1c_ad.run_c1c_ad_job(
                    self.bot, trigger="scheduled", force=False
                )

            c1c_ad_job.do(c1c_ad_runner)
            successes.append(
                (
                    SimpleNamespace(
                        bucket="c1c_ad", cadence_label=f"{c1c_refresh_days}d"
                    ),
                    c1c_ad_job,
                )
            )
        else:
            log.info("C1C ad job disabled; C1C_AD_REFRESH_DAYS is not configured.")

        try:
            clan_ads_enabled = feature_flags.is_enabled("clan_ads")
        except Exception as exc:
            if sheets_core._is_rate_limited_error(exc):
                self._log_optional_scheduler_quota_skip(
                    logger=log,
                    scheduler_name="clan_ads",
                    config_source="Feature Toggle:clan_ads",
                    exc=exc,
                )
                clan_ads_enabled = False
            else:
                raise

        if not clan_ads_enabled:
            log.info("Clan ads job disabled via feature toggle.")
        else:
            try:
                clan_ads_config = await recruitment_clan_ads.load_config(force=True)
            except Exception as exc:
                if sheets_core._is_rate_limited_error(exc):
                    self._log_optional_scheduler_quota_skip(
                        logger=log,
                        scheduler_name="clan_ads",
                        config_source="Config:clan ads scheduler",
                        exc=exc,
                    )
                    clan_ads_config = None
                else:
                    raise
            if clan_ads_config and clan_ads_config.interval_hours > 0:
                clan_ads_job = self.scheduler.every(
                    hours=clan_ads_config.interval_hours,
                    tag="clan_ads",
                    name="clan_ads",
                )

                async def clan_ads_runner() -> None:
                    await recruitment_clan_ads.scheduled_tick(self.bot)

                clan_ads_job.do(clan_ads_runner)
                successes.append(
                    (
                        SimpleNamespace(
                            bucket="clan_ads",
                            cadence_label=f"{clan_ads_config.interval_hours:g}h",
                        ),
                        clan_ads_job,
                    )
                )
            else:
                log.info(
                    "Clan ads job disabled; clan_ad_post_interval_hours is not configured."
                )

        if toggles.housekeeping_enabled:
            keepalive_logger = logging.getLogger("c1c.housekeeping.keepalive")
            try:
                keepalive_config = await housekeeping_keepalive.resolve_keepalive_config_async(
                    keepalive_logger
                )
            except Exception as exc:
                keepalive_config = None
                if sheets_core._is_rate_limited_error(exc):
                    keepalive_logger.warning(
                        "thread keepalive config resolve hit Google Sheets quota/backoff; "
                        "skipping keepalive registration for this ready cycle without failing startup: %s",
                        exc,
                    )
                else:
                    keepalive_logger.exception(
                        "thread keepalive config resolve failed; skipping keepalive registration without failing startup"
                    )
            if keepalive_config and keepalive_config.enabled:
                keepalive_job = self.scheduler.every(
                    hours=float(keepalive_config.run_every_hours),
                    tag="keepalive",
                    name="housekeeping_keepalive",
                )

                async def keepalive_runner() -> None:
                    await housekeeping_keepalive.run_keepalive(
                        self.bot, keepalive_logger
                    )

                keepalive_job.do(keepalive_runner)
                successes.append(
                    (
                        SimpleNamespace(
                            bucket="housekeeping_keepalive",
                            cadence_label=f"{keepalive_config.run_every_hours:g}h",
                        ),
                        keepalive_job,
                    )
                )
            else:
                log.info(
                    "thread keepalive not scheduled; required sheet Config is missing, invalid, or disabled"
                )
        else:
            log.info("housekeeping keepalive disabled via feature toggle")

        self._register_optional_scheduler(
            "server_map",
            "Feature Toggle:SERVER_MAP / shared config",
            lambda: server_map_module.schedule_server_map_job(self),
            logger=logging.getLogger("c1c.server_map"),
        )
        self._register_optional_scheduler(
            "leagues",
            "environment scheduler config",
            lambda: schedule_leagues_jobs(self),
            logger=logging.getLogger("c1c.community.leagues.scheduler"),
        )
        self._register_optional_scheduler(
            "fusion",
            "runtime scheduler config",
            lambda: schedule_fusion_jobs(self),
            logger=logging.getLogger("c1c.community.fusion.scheduler"),
        )
        self._register_optional_scheduler(
            "shard_weekly_reminders",
            "runtime scheduler config",
            lambda: schedule_shard_jobs(self),
            logger=logging.getLogger("c1c.shards.scheduler"),
        )
        self._register_optional_scheduler(
            "reset_reminders",
            "Feature Toggle:reset_reminders",
            lambda: schedule_reset_reminder_jobs(self),
            logger=logging.getLogger("c1c.community.reset_reminders.scheduler"),
        )

    async def close(self) -> None:
        await self.shutdown_webserver()
        await self.scheduler.shutdown()
        set_active_runtime(None)
