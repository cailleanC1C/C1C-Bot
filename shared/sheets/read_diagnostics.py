"""Rolling, metadata-only diagnostics for physical Google Sheets reads.

The read broker already owns the physical request boundary.  This module attaches
an in-process observer to that boundary and keeps a short rolling attribution
window by component/reason/operation.  It never stores Sheet IDs, ranges, values,
or response payloads.
"""

from __future__ import annotations

import collections
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

from shared.sheets.read_broker import SheetsReadBroker, broker

ClockFn = Callable[[], float]
_DEFAULT_WINDOW_SEC = 5 * 60.0
_DEFAULT_TOP_LIMIT = 10


@dataclass(frozen=True, slots=True)
class PhysicalReadEvent:
    """One physical broker attempt, reduced to safe attribution metadata."""

    timestamp: float
    component: str
    reason: str
    operation: str
    ok: bool
    rate_limited: bool


class PhysicalReadDiagnostics:
    """Keep a bounded rolling window of physical read-attribution events."""

    def __init__(
        self,
        *,
        window_sec: float = _DEFAULT_WINDOW_SEC,
        clock_fn: ClockFn = time.monotonic,
    ) -> None:
        if window_sec <= 0:
            raise ValueError("window_sec must be > 0")
        self.window_sec = float(window_sec)
        self._clock = clock_fn
        self._events: collections.deque[PhysicalReadEvent] = collections.deque()
        self._lock = threading.Lock()

    def record(
        self,
        *,
        component: object,
        reason: object,
        operation: object,
        ok: bool,
        rate_limited: bool,
        timestamp: float | None = None,
    ) -> None:
        event = PhysicalReadEvent(
            timestamp=self._clock() if timestamp is None else float(timestamp),
            component=_safe_meta(component),
            reason=_safe_meta(reason),
            operation=_safe_meta(operation),
            ok=bool(ok),
            rate_limited=bool(rate_limited),
        )
        with self._lock:
            self._events.append(event)
            self._purge_locked(event.timestamp)

    def snapshot(self, *, top_limit: int = _DEFAULT_TOP_LIMIT) -> dict[str, object]:
        limit = max(1, int(top_limit))
        now = self._clock()
        with self._lock:
            self._purge_locked(now)
            events = tuple(self._events)

        consumers: dict[tuple[str, str], dict[str, int]] = {}
        operations: collections.Counter[str] = collections.Counter()
        for event in events:
            key = (event.component, event.reason)
            bucket = consumers.setdefault(
                key,
                {"reads": 0, "failures": 0, "rate_limited": 0},
            )
            bucket["reads"] += 1
            if not event.ok:
                bucket["failures"] += 1
            if event.rate_limited:
                bucket["rate_limited"] += 1
            operations[event.operation] += 1

        ranked_consumers = sorted(
            (
                {
                    "component": component,
                    "reason": reason,
                    **counts,
                }
                for (component, reason), counts in consumers.items()
            ),
            key=lambda item: (
                -int(item["reads"]),
                str(item["component"]),
                str(item["reason"]),
            ),
        )[:limit]
        ranked_operations = [
            {"operation": operation, "reads": reads}
            for operation, reads in operations.most_common(limit)
        ]

        return {
            "window_sec": int(self.window_sec),
            "physical_reads": len(events),
            "failures": sum(1 for event in events if not event.ok),
            "rate_limited": sum(1 for event in events if event.rate_limited),
            "top_consumers": ranked_consumers,
            "top_operations": ranked_operations,
        }

    def clear(self) -> None:
        with self._lock:
            self._events.clear()

    def _purge_locked(self, now: float) -> None:
        cutoff = now - self.window_sec
        while self._events and self._events[0].timestamp <= cutoff:
            self._events.popleft()


def _safe_meta(value: object) -> str:
    text = str(value or "unknown").replace("\n", " ").replace("\r", " ").strip()
    return text[:120] or "unknown"


diagnostics = PhysicalReadDiagnostics()


def install(
    *,
    target_broker: SheetsReadBroker = broker,
    collector: PhysicalReadDiagnostics = diagnostics,
) -> None:
    """Attach attribution to one broker instance exactly once.

    The observer wraps the broker's existing physical-log boundary because that
    method is invoked exactly once for every physical attempt, including retries
    and failed/rate-limited attempts.  The original logger still runs unchanged.
    """

    marker = "_c1c_read_diagnostics_installed"
    if bool(getattr(target_broker, marker, False)):
        return

    original = target_broker._log_physical

    def _observed_log_physical(
        key: Any,
        *,
        policy: Any,
        priority: Any,
        component: str,
        reason: str,
        queue_wait_sec: float,
        duration_sec: float,
        attempt: int,
        ok: bool,
        rate_limited: bool,
    ) -> None:
        collector.record(
            component=component,
            reason=reason,
            operation=getattr(key, "operation", "unknown"),
            ok=ok,
            rate_limited=rate_limited,
        )
        original(
            key,
            policy=policy,
            priority=priority,
            component=component,
            reason=reason,
            queue_wait_sec=queue_wait_sec,
            duration_sec=duration_sec,
            attempt=attempt,
            ok=ok,
            rate_limited=rate_limited,
        )

    target_broker._log_physical = _observed_log_physical  # type: ignore[method-assign]
    setattr(target_broker, marker, True)


def health_snapshot(
    *,
    target_broker: SheetsReadBroker = broker,
    collector: PhysicalReadDiagnostics = diagnostics,
) -> dict[str, object]:
    """Return non-secret broker health plus rolling five-minute attribution."""

    raw = target_broker.snapshot()
    logical_reads = int(raw.get("logical_reads", 0) or 0)
    cache_hits = int(raw.get("cache_hits", 0) or 0)
    stale_hits = int(raw.get("stale_hits", 0) or 0)
    coalesced = int(raw.get("coalesced_joins", 0) or 0)
    served_without_physical = cache_hits + stale_hits + coalesced
    hit_rate = (
        (served_without_physical / logical_reads) * 100.0 if logical_reads > 0 else 0.0
    )
    rolling = collector.snapshot()

    return {
        "read_budget_rpm": int(raw.get("read_budget_rpm", 0) or 0),
        "rolling_physical_reads_1m": int(raw.get("rolling_physical_reads", 0) or 0),
        "physical_reads_total": int(raw.get("physical_reads", 0) or 0),
        "logical_reads_total": logical_reads,
        "cache_hit_rate_pct": round(hit_rate, 2),
        "cache_entries": int(raw.get("cache_entries", 0) or 0),
        "queued": int(raw.get("queued", 0) or 0),
        "inflight": int(raw.get("inflight", 0) or 0),
        "rate_limit_errors_total": int(raw.get("rate_limit_errors", 0) or 0),
        "retries_total": int(raw.get("retries", 0) or 0),
        "invalidations_total": int(raw.get("invalidations", 0) or 0),
        "rolling_5m": rolling,
    }


__all__ = [
    "PhysicalReadDiagnostics",
    "PhysicalReadEvent",
    "diagnostics",
    "health_snapshot",
    "install",
]
