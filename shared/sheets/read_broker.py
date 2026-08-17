"""Process-wide Google Sheets read broker foundation.

This module deliberately does not call gspread itself.  Callers provide an async
``loader`` representing one physical Sheets read.  Follow-up migration work can
route the existing Sheets access layer through this broker without changing the
broker's cache, pacing, single-flight, retry, or telemetry semantics.

Terminology
-----------
Logical read
    One feature/scheduler request to :meth:`SheetsReadBroker.read`.
Physical read
    One invocation of the supplied loader.  Retries are physical reads too,
    because each attempt consumes Google API quota.

The cache key identifies the underlying Sheets resource.  Caller metadata such as
``component`` and ``reason`` is intentionally excluded so different modules can
coalesce and reuse the same resource read.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import os
import random
import time
from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Awaitable, Callable, Deque, Dict, Mapping, TypeVar

T = TypeVar("T")
Loader = Callable[[], Awaitable[T]]
SleepFn = Callable[[float], Awaitable[None]]
ClockFn = Callable[[], float]

log = logging.getLogger("c1c.sheets.read_broker")

_DEFAULT_READ_BUDGET_RPM = 32
_DEFAULT_RETRY_ATTEMPTS = 3
_DEFAULT_RETRY_BASE_DELAY_SEC = 1.0
_DEFAULT_RETRY_FACTOR = 2.0
_DEFAULT_RETRY_MAX_DELAY_SEC = 30.0


class ReadPriority(IntEnum):
    """Broker queue priority. Lower numeric values are served first."""

    CRITICAL = 0
    HIGH = 1
    NORMAL = 2
    BACKGROUND = 3


@dataclass(frozen=True, slots=True)
class CachePolicy:
    """Fresh/stale bounds for one class of Sheets data."""

    name: str
    fresh_ttl_sec: float
    stale_ttl_sec: float

    def __post_init__(self) -> None:
        if self.fresh_ttl_sec < 0:
            raise ValueError("fresh_ttl_sec must be >= 0")
        if self.stale_ttl_sec < self.fresh_ttl_sec:
            raise ValueError("stale_ttl_sec must be >= fresh_ttl_sec")

    @property
    def stale_allowed(self) -> bool:
        return self.stale_ttl_sec > self.fresh_ttl_sec


STATIC_CONFIG = CachePolicy("STATIC_CONFIG", 30 * 60, 12 * 60 * 60)
RUNTIME_CONFIG = CachePolicy("RUNTIME_CONFIG", 10 * 60, 4 * 60 * 60)
ACTIVE_STATE = CachePolicy("ACTIVE_STATE", 60, 5 * 60)
BACKGROUND_DATA = CachePolicy("BACKGROUND_DATA", 30 * 60, 24 * 60 * 60)
FRESH_REQUIRED = CachePolicy("FRESH_REQUIRED", 0, 0)

CACHE_POLICIES: Mapping[str, CachePolicy] = {
    policy.name: policy
    for policy in (
        STATIC_CONFIG,
        RUNTIME_CONFIG,
        ACTIVE_STATE,
        BACKGROUND_DATA,
        FRESH_REQUIRED,
    )
}


@dataclass(frozen=True, slots=True)
class SheetReadKey:
    """Canonical identity for one read-side Sheets resource."""

    sheet_id: str
    operation: str
    worksheet: str = ""
    a1_range: str = ""

    def __post_init__(self) -> None:
        sheet_id = str(self.sheet_id or "").strip()
        operation = str(self.operation or "").strip().casefold()
        worksheet = str(self.worksheet or "").strip()
        a1_range = str(self.a1_range or "").strip()
        if not sheet_id:
            raise ValueError("sheet_id is required")
        if not operation:
            raise ValueError("operation is required")
        object.__setattr__(self, "sheet_id", sheet_id)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "worksheet", worksheet)
        object.__setattr__(self, "a1_range", a1_range)

    @classmethod
    def records(cls, sheet_id: str, worksheet: str) -> "SheetReadKey":
        return cls(sheet_id=sheet_id, operation="records_all", worksheet=worksheet)

    @classmethod
    def values(cls, sheet_id: str, worksheet: str) -> "SheetReadKey":
        return cls(sheet_id=sheet_id, operation="values_all", worksheet=worksheet)

    @classmethod
    def range(cls, sheet_id: str, worksheet: str, a1_range: str) -> "SheetReadKey":
        return cls(
            sheet_id=sheet_id,
            operation="range",
            worksheet=worksheet,
            a1_range=a1_range,
        )

    @property
    def sheet_tail(self) -> str:
        return self.sheet_id[-6:] if self.sheet_id else "-"


@dataclass(slots=True)
class _CacheEntry:
    value: Any
    loaded_at: float


@dataclass(slots=True)
class _InFlight:
    task: asyncio.Task[Any]
    generation: int


@dataclass(slots=True)
class _Waiter:
    future: asyncio.Future[float]
    enqueued_at: float


class _PriorityPacer:
    """Pace physical reads while allowing urgent work ahead of background work.

    A small anti-starvation rule forces one lower-priority grant after eight
    consecutive CRITICAL/HIGH grants whenever lower-priority work is waiting.
    """

    _URGENT_BURST_LIMIT = 8

    def __init__(
        self,
        *,
        rpm: int,
        window_seconds: float = 60.0,
        sleep_fn: SleepFn = asyncio.sleep,
        clock_fn: ClockFn = time.monotonic,
    ) -> None:
        if rpm <= 0:
            raise ValueError("rpm must be > 0")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be > 0")
        self.rpm = int(rpm)
        self.window_seconds = float(window_seconds)
        self._interval = self.window_seconds / self.rpm
        self._sleep = sleep_fn
        self._clock = clock_fn
        self._queues: Dict[ReadPriority, Deque[_Waiter]] = {
            priority: collections.deque() for priority in ReadPriority
        }
        self._worker: asyncio.Task[None] | None = None
        self._last_grant: float | None = None
        self._urgent_streak = 0
        self._closed = False

    @property
    def queued(self) -> int:
        return sum(len(queue) for queue in self._queues.values())

    async def acquire(self, priority: ReadPriority) -> float:
        if self._closed:
            raise RuntimeError("Sheets read pacer is closed")
        loop = asyncio.get_running_loop()
        future: asyncio.Future[float] = loop.create_future()
        self._queues[ReadPriority(priority)].append(
            _Waiter(future=future, enqueued_at=self._clock())
        )
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(
                self._run(), name="sheets-read-pacer"
            )
        try:
            return await future
        except asyncio.CancelledError:
            if not future.done():
                future.cancel()
            raise

    def _pick_priority(self) -> ReadPriority | None:
        urgent_waiting = any(
            self._queues[p]
            for p in (ReadPriority.CRITICAL, ReadPriority.HIGH)
        )
        lower_waiting = any(
            self._queues[p]
            for p in (ReadPriority.NORMAL, ReadPriority.BACKGROUND)
        )
        if (
            urgent_waiting
            and lower_waiting
            and self._urgent_streak >= self._URGENT_BURST_LIMIT
        ):
            for priority in (ReadPriority.NORMAL, ReadPriority.BACKGROUND):
                if self._queues[priority]:
                    return priority

        for priority in ReadPriority:
            if self._queues[priority]:
                return priority
        return None

    async def _run(self) -> None:
        try:
            while not self._closed:
                priority = self._pick_priority()
                if priority is None:
                    return
                queue = self._queues[priority]
                waiter = queue.popleft()
                if waiter.future.cancelled():
                    continue

                now = self._clock()
                if self._last_grant is not None:
                    delay = self._interval - (now - self._last_grant)
                    if delay > 0:
                        await self._sleep(delay)
                granted_at = self._clock()
                self._last_grant = granted_at

                if priority in (ReadPriority.CRITICAL, ReadPriority.HIGH):
                    self._urgent_streak += 1
                else:
                    self._urgent_streak = 0

                if not waiter.future.done():
                    waiter.future.set_result(max(0.0, granted_at - waiter.enqueued_at))
        finally:
            if asyncio.current_task() is self._worker:
                self._worker = None

    async def close(self) -> None:
        self._closed = True
        worker = self._worker
        if worker is not None and not worker.done():
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        for queue in self._queues.values():
            while queue:
                waiter = queue.popleft()
                if not waiter.future.done():
                    waiter.future.cancel()


class SheetsReadBroker:
    """Process-wide coordinator for all future Google Sheets physical reads."""

    def __init__(
        self,
        *,
        read_budget_rpm: int | None = None,
        rate_window_seconds: float = 60.0,
        retry_attempts: int = _DEFAULT_RETRY_ATTEMPTS,
        retry_base_delay_sec: float = _DEFAULT_RETRY_BASE_DELAY_SEC,
        retry_factor: float = _DEFAULT_RETRY_FACTOR,
        retry_max_delay_sec: float = _DEFAULT_RETRY_MAX_DELAY_SEC,
        sleep_fn: SleepFn = asyncio.sleep,
        clock_fn: ClockFn = time.monotonic,
        jitter_fn: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.read_budget_rpm = int(
            read_budget_rpm
            if read_budget_rpm is not None
            else _read_budget_from_env()
        )
        if self.read_budget_rpm <= 0:
            raise ValueError("read_budget_rpm must be > 0")
        if retry_attempts <= 0:
            raise ValueError("retry_attempts must be > 0")
        if retry_base_delay_sec < 0 or retry_factor < 1 or retry_max_delay_sec < 0:
            raise ValueError("invalid retry configuration")

        self._clock = clock_fn
        self._sleep = sleep_fn
        self._jitter = jitter_fn
        self._retry_attempts = int(retry_attempts)
        self._retry_base_delay_sec = float(retry_base_delay_sec)
        self._retry_factor = float(retry_factor)
        self._retry_max_delay_sec = float(retry_max_delay_sec)
        self._rate_window_seconds = float(rate_window_seconds)
        self._pacer = _PriorityPacer(
            rpm=self.read_budget_rpm,
            window_seconds=self._rate_window_seconds,
            sleep_fn=sleep_fn,
            clock_fn=clock_fn,
        )

        self._cache: Dict[SheetReadKey, _CacheEntry] = {}
        self._inflight: Dict[SheetReadKey, _InFlight] = {}
        self._generation: Dict[SheetReadKey, int] = collections.defaultdict(int)
        self._state_lock = asyncio.Lock()
        self._physical_timestamps: Deque[float] = collections.deque()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._closed = False

        self._stats: Dict[str, int] = collections.Counter(
            {
                "logical_reads": 0,
                "cache_hits": 0,
                "stale_hits": 0,
                "cache_misses": 0,
                "coalesced_joins": 0,
                "physical_reads": 0,
                "physical_successes": 0,
                "rate_limit_errors": 0,
                "retries": 0,
                "invalidations": 0,
            }
        )

    async def read(
        self,
        key: SheetReadKey,
        loader: Loader[T],
        *,
        policy: CachePolicy = RUNTIME_CONFIG,
        priority: ReadPriority = ReadPriority.NORMAL,
        component: str = "unknown",
        reason: str = "runtime",
    ) -> T:
        """Return a cached/coalesced value, invoking ``loader`` only when needed."""

        if self._closed:
            raise RuntimeError("Sheets read broker is closed")
        if not isinstance(key, SheetReadKey):
            raise TypeError("key must be a SheetReadKey")
        self._stats["logical_reads"] += 1
        now = self._clock()

        entry = self._cache.get(key)
        if entry is not None and policy is not FRESH_REQUIRED:
            age = max(0.0, now - entry.loaded_at)
            if age < policy.fresh_ttl_sec:
                self._stats["cache_hits"] += 1
                self._log_logical(
                    key,
                    policy=policy,
                    priority=priority,
                    component=component,
                    reason=reason,
                    cache_status="fresh_hit",
                    coalesced=False,
                )
                return entry.value
            if age < policy.stale_ttl_sec and policy.stale_allowed:
                self._stats["stale_hits"] += 1
                task, coalesced = await self._get_or_create_refresh(
                    key,
                    loader,
                    policy=policy,
                    priority=priority,
                    component=component,
                    reason="stale_revalidate",
                )
                self._track_background(task)
                self._log_logical(
                    key,
                    policy=policy,
                    priority=priority,
                    component=component,
                    reason=reason,
                    cache_status="stale_returned",
                    coalesced=coalesced,
                )
                return entry.value

        self._stats["cache_misses"] += 1
        task, coalesced = await self._get_or_create_refresh(
            key,
            loader,
            policy=policy,
            priority=priority,
            component=component,
            reason=reason,
        )
        self._log_logical(
            key,
            policy=policy,
            priority=priority,
            component=component,
            reason=reason,
            cache_status="miss",
            coalesced=coalesced,
        )
        return await asyncio.shield(task)

    async def _get_or_create_refresh(
        self,
        key: SheetReadKey,
        loader: Loader[T],
        *,
        policy: CachePolicy,
        priority: ReadPriority,
        component: str,
        reason: str,
    ) -> tuple[asyncio.Task[T], bool]:
        async with self._state_lock:
            existing = self._inflight.get(key)
            if existing is not None and not existing.task.done():
                self._stats["coalesced_joins"] += 1
                return existing.task, True  # type: ignore[return-value]

            generation = self._generation[key]
            task: asyncio.Task[T] = asyncio.create_task(
                self._refresh(
                    key,
                    loader,
                    generation=generation,
                    policy=policy,
                    priority=priority,
                    component=component,
                    reason=reason,
                ),
                name=f"sheets-read:{key.operation}:{key.sheet_tail}",
            )
            self._inflight[key] = _InFlight(task=task, generation=generation)
            task.add_done_callback(
                lambda done, request_key=key: self._refresh_done(request_key, done)
            )
            return task, False

    def _refresh_done(self, key: SheetReadKey, task: asyncio.Task[Any]) -> None:
        # Done callbacks execute on the broker's event loop, so this identity check
        # can safely clean up without spawning another task.  Identity matters: an
        # invalidation may detach this task and allow a newer refresh for the same
        # key to start before the old request finishes.
        current = self._inflight.get(key)
        if current is not None and current.task is task:
            self._inflight.pop(key, None)
        if task.cancelled():
            return
        try:
            task.exception()  # retrieve background-refresh exceptions
        except asyncio.CancelledError:
            return

    def _track_background(self, task: asyncio.Task[Any]) -> None:
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _refresh(
        self,
        key: SheetReadKey,
        loader: Loader[T],
        *,
        generation: int,
        policy: CachePolicy,
        priority: ReadPriority,
        component: str,
        reason: str,
    ) -> T:
        value = await self._execute_physical(
            key,
            loader,
            priority=priority,
            policy=policy,
            component=component,
            reason=reason,
        )
        async with self._state_lock:
            if self._generation[key] == generation:
                self._cache[key] = _CacheEntry(value=value, loaded_at=self._clock())
        return value

    async def _execute_physical(
        self,
        key: SheetReadKey,
        loader: Loader[T],
        *,
        priority: ReadPriority,
        policy: CachePolicy,
        component: str,
        reason: str,
    ) -> T:
        last_exc: BaseException | None = None
        for attempt in range(1, self._retry_attempts + 1):
            queue_wait = await self._pacer.acquire(priority)
            started = self._clock()
            self._record_physical_attempt(started)
            rate_limited = False
            try:
                result = await loader()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                last_exc = exc
                rate_limited = is_rate_limited_error(exc)
                if rate_limited:
                    self._stats["rate_limit_errors"] += 1
                self._log_physical(
                    key,
                    policy=policy,
                    priority=priority,
                    component=component,
                    reason=reason,
                    queue_wait_sec=queue_wait,
                    duration_sec=max(0.0, self._clock() - started),
                    attempt=attempt,
                    ok=False,
                    rate_limited=rate_limited,
                )
                if not rate_limited or attempt >= self._retry_attempts:
                    raise
                self._stats["retries"] += 1
                await self._sleep(self._retry_delay(attempt))
                continue
            else:
                self._stats["physical_successes"] += 1
                self._log_physical(
                    key,
                    policy=policy,
                    priority=priority,
                    component=component,
                    reason=reason,
                    queue_wait_sec=queue_wait,
                    duration_sec=max(0.0, self._clock() - started),
                    attempt=attempt,
                    ok=True,
                    rate_limited=False,
                )
                return result

        assert last_exc is not None  # pragma: no cover - loop always raises/returns
        raise last_exc

    def _retry_delay(self, failed_attempt: int) -> float:
        if self._retry_base_delay_sec <= 0:
            return 0.0
        raw = self._retry_base_delay_sec * (
            self._retry_factor ** max(0, failed_attempt - 1)
        )
        capped = min(self._retry_max_delay_sec, raw)
        jitter = max(0.0, self._jitter(0.75, 1.25))
        return capped * jitter

    def _record_physical_attempt(self, timestamp: float) -> None:
        self._stats["physical_reads"] += 1
        self._physical_timestamps.append(timestamp)
        self._purge_physical_timestamps(timestamp)

    def _purge_physical_timestamps(self, now: float | None = None) -> None:
        current = self._clock() if now is None else now
        cutoff = current - self._rate_window_seconds
        while self._physical_timestamps and self._physical_timestamps[0] <= cutoff:
            self._physical_timestamps.popleft()

    async def invalidate(self, key: SheetReadKey) -> int:
        """Invalidate one exact resource and detach any stale in-flight refresh."""

        async with self._state_lock:
            existed = key in self._cache or key in self._inflight
            self._generation[key] += 1
            self._cache.pop(key, None)
            self._inflight.pop(key, None)
            if existed:
                self._stats["invalidations"] += 1
            return 1 if existed else 0

    async def invalidate_worksheet(self, sheet_id: str, worksheet: str) -> int:
        """Invalidate every cached/in-flight read for one worksheet."""

        sheet = str(sheet_id or "").strip()
        tab = str(worksheet or "").strip()
        return await self._invalidate_matching(
            lambda key: key.sheet_id == sheet and key.worksheet == tab
        )

    async def invalidate_workbook(self, sheet_id: str) -> int:
        """Invalidate every cached/in-flight read for one workbook."""

        sheet = str(sheet_id or "").strip()
        return await self._invalidate_matching(lambda key: key.sheet_id == sheet)

    async def _invalidate_matching(
        self, predicate: Callable[[SheetReadKey], bool]
    ) -> int:
        async with self._state_lock:
            keys = {
                key for key in self._cache if predicate(key)
            } | {
                key for key in self._inflight if predicate(key)
            }
            for key in keys:
                self._generation[key] += 1
                self._cache.pop(key, None)
                self._inflight.pop(key, None)
            if keys:
                self._stats["invalidations"] += len(keys)
            return len(keys)

    def snapshot(self) -> dict[str, int | float]:
        """Return non-secret broker diagnostics suitable for health reporting."""

        self._purge_physical_timestamps()
        return {
            "read_budget_rpm": self.read_budget_rpm,
            "rolling_physical_reads": len(self._physical_timestamps),
            "cache_entries": len(self._cache),
            "inflight": sum(
                1 for item in self._inflight.values() if not item.task.done()
            ),
            "queued": self._pacer.queued,
            **dict(self._stats),
        }

    async def wait_for_idle(self) -> None:
        """Wait for currently tracked refresh/cleanup work; intended for tests/ops."""

        while True:
            pending = [task for task in self._background_tasks if not task.done()]
            inflight = [
                item.task for item in self._inflight.values() if not item.task.done()
            ]
            tasks = {task for task in pending + inflight if task is not asyncio.current_task()}
            if not tasks:
                return
            await asyncio.gather(*(asyncio.shield(task) for task in tasks), return_exceptions=True)

    async def close(self) -> None:
        """Cancel broker-owned background work and stop the request pacer."""

        self._closed = True
        tasks = {
            item.task for item in self._inflight.values() if not item.task.done()
        } | {
            task for task in self._background_tasks if not task.done()
        }
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._inflight.clear()
        self._background_tasks.clear()
        await self._pacer.close()

    def _log_logical(
        self,
        key: SheetReadKey,
        *,
        policy: CachePolicy,
        priority: ReadPriority,
        component: str,
        reason: str,
        cache_status: str,
        coalesced: bool,
    ) -> None:
        log.debug(
            "sheets logical read component=%s reason=%s operation=%s sheet_tail=%s "
            "worksheet=%s policy=%s priority=%s cache=%s coalesced=%s",
            _safe_meta(component),
            _safe_meta(reason),
            key.operation,
            key.sheet_tail,
            _safe_meta(key.worksheet),
            policy.name,
            ReadPriority(priority).name,
            cache_status,
            coalesced,
        )

    def _log_physical(
        self,
        key: SheetReadKey,
        *,
        policy: CachePolicy,
        priority: ReadPriority,
        component: str,
        reason: str,
        queue_wait_sec: float,
        duration_sec: float,
        attempt: int,
        ok: bool,
        rate_limited: bool,
    ) -> None:
        self._purge_physical_timestamps()
        log.info(
            "sheets physical read component=%s reason=%s operation=%s sheet_tail=%s "
            "worksheet=%s range=%s policy=%s priority=%s physical_request=true "
            "queue_wait_ms=%d duration_ms=%d attempt=%d rate_limited=%s "
            "rolling_reads=%d result=%s",
            _safe_meta(component),
            _safe_meta(reason),
            key.operation,
            key.sheet_tail,
            _safe_meta(key.worksheet),
            _safe_meta(key.a1_range),
            policy.name,
            ReadPriority(priority).name,
            int(max(0.0, queue_wait_sec) * 1000),
            int(max(0.0, duration_sec) * 1000),
            attempt,
            rate_limited,
            len(self._physical_timestamps),
            "ok" if ok else "fail",
        )


def _safe_meta(value: object) -> str:
    text = str(value or "-").replace("\n", " ").replace("\r", " ").strip()
    return text[:120] or "-"


def _read_budget_from_env() -> int:
    raw = str(os.getenv("SHEETS_READ_BUDGET_RPM", "") or "").strip()
    if not raw:
        return _DEFAULT_READ_BUDGET_RPM
    try:
        value = int(raw)
    except ValueError:
        log.warning(
            "invalid SHEETS_READ_BUDGET_RPM=%r; using default=%d",
            raw,
            _DEFAULT_READ_BUDGET_RPM,
        )
        return _DEFAULT_READ_BUDGET_RPM
    if value <= 0:
        log.warning(
            "non-positive SHEETS_READ_BUDGET_RPM=%r; using default=%d",
            raw,
            _DEFAULT_READ_BUDGET_RPM,
        )
        return _DEFAULT_READ_BUDGET_RPM
    return value


def is_rate_limited_error(exc: BaseException) -> bool:
    """Best-effort detection for Google/gspread 429 RESOURCE_EXHAUSTED errors."""

    candidates: list[object] = [
        getattr(exc, "status_code", None),
        getattr(exc, "code", None),
        getattr(exc, "status", None),
    ]
    response = getattr(exc, "response", None)
    if response is not None:
        candidates.extend(
            [
                getattr(response, "status_code", None),
                getattr(response, "status", None),
            ]
        )
    for value in candidates:
        try:
            if int(value) == 429:
                return True
        except (TypeError, ValueError):
            pass

    text = str(exc or "").casefold()
    return any(
        token in text
        for token in (
            "429",
            "resource_exhausted",
            "resource exhausted",
            "quota exceeded",
            "read requests per minute",
        )
    )


# Canonical process-wide instance.  PR 1 intentionally leaves existing callers
# untouched; the migration PR will route shared.sheets.core/async_core through it.
broker = SheetsReadBroker()


__all__ = [
    "ACTIVE_STATE",
    "BACKGROUND_DATA",
    "CACHE_POLICIES",
    "CachePolicy",
    "FRESH_REQUIRED",
    "RUNTIME_CONFIG",
    "ReadPriority",
    "STATIC_CONFIG",
    "SheetReadKey",
    "SheetsReadBroker",
    "broker",
    "is_rate_limited_error",
]
