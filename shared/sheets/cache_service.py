from __future__ import annotations

import asyncio
import datetime as dt
import logging
from types import ModuleType
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from shared.sheets import audit as sheets_audit

UTC = dt.timezone.utc
log = logging.getLogger(__name__)

_runtime_module: ModuleType | None = None


def _get_runtime_module() -> ModuleType:
    global _runtime_module
    if _runtime_module is None:
        # Lazy import to avoid circular import at module load time.
        from modules.common import runtime as _rt

        _runtime_module = _rt
    return _runtime_module

_ONBOARDING_QUESTIONS_TTL_SEC = 7 * 24 * 60 * 60


async def _load_onboarding_questions() -> Tuple[dict[str, str], ...]:
    """Load onboarding questions from Sheets via the async cache loader."""

    from shared.sheets.onboarding_questions import fetch_question_rows_async

    rows = await fetch_question_rows_async()
    return rows


def _errtext(exc: BaseException) -> str:
    if isinstance(exc, KeyError) and exc.args:
        candidate = exc.args[0]
        if isinstance(candidate, str):
            text = candidate.strip()
            if text:
                return text
    s = str(exc).strip()
    if s.startswith("'") and s.endswith("'") and len(s) > 2:
        inner = s[1:-1].strip()
        if inner:
            return inner
    return s or getattr(exc, "__class__", type(exc)).__name__


# Type aliases
Loader = Callable[[], Awaitable[Any]]

class CacheBucket:
    __slots__ = (
        "name",
        "ttl_sec",
        "loader",
        "value",
        "last_refresh",
        "refreshing",
        "last_latency_ms",
        "last_result",
        "last_error",
        "last_retries",
        "last_item_count",
        "last_trigger",
        "last_ttl_expired",
        "retry_delay_sec",
    )
    def __init__(self, name: str, ttl_sec: int, loader: Loader, *, retry_delay_sec: int = 300):
        self.name = name
        self.ttl_sec = ttl_sec
        self.loader = loader
        self.value: Any = None
        self.last_refresh: Optional[dt.datetime] = None
        self.refreshing: Optional[asyncio.Task] = None
        self.last_latency_ms: Optional[int] = None
        self.last_result: Optional[str] = None
        self.last_error: Optional[str] = None
        self.last_retries: int = 0
        self.last_item_count: Optional[int] = None
        self.last_trigger: Optional[str] = None
        self.last_ttl_expired: Optional[bool] = None
        self.retry_delay_sec = retry_delay_sec

    def age_sec(self) -> Optional[int]:
        if not self.last_refresh:
            return None
        return int((dt.datetime.now(UTC) - self.last_refresh).total_seconds())

    def next_refresh_at(self, schedule_hint: Optional[dt.datetime] = None) -> Optional[dt.datetime]:
        if self.last_refresh:
            return self.last_refresh + dt.timedelta(seconds=self.ttl_sec)
        return schedule_hint

class CacheService:
    def __init__(self):
        self._buckets: Dict[str, CacheBucket] = {}

    def register(
        self, name: str, ttl_sec: int, loader: Loader, *, retry_delay_sec: int = 300
    ) -> CacheBucket:
        b = CacheBucket(name, ttl_sec, loader, retry_delay_sec=retry_delay_sec)
        self._buckets[name] = b
        return b

    def get_bucket(self, name: str) -> Optional[CacheBucket]:
        return self._buckets.get(name)

    def capabilities(self) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for name, b in self._buckets.items():
            out[name] = {
                "ttl_sec": b.ttl_sec,
                "last_refresh_at": b.last_refresh,
                "next_refresh_at": b.next_refresh_at(),
                "refresh": lambda n=name: self.refresh_now(n),
            }
        return out

    async def get(self, name: str) -> Any:
        b = self._buckets[name]
        # Fast-path: fresh enough
        age = b.age_sec()
        if age is not None and age < b.ttl_sec and b.value is not None:
            with sheets_audit.log_read(
                component="cache_service",
                operation="get",
                sheet_source="cache",
                cache_bucket=name,
                trigger="runtime",
                cache_status="hit",
            ) as audit_fields:
                audit_fields["result"] = b.value
                return b.value
        # Otherwise trigger background refresh (debounced) and return stale ASAP
        await self._ensure_background_refresh(name)
        with sheets_audit.log_read(
            component="cache_service",
            operation="get",
            sheet_source="cache",
            cache_bucket=name,
            trigger="runtime",
            cache_status="miss_stale_returned",
        ) as audit_fields:
            audit_fields["result"] = b.value
            return b.value

    async def invalidate(self, name: str) -> None:
        b = self._buckets[name]
        b.last_refresh = None  # mark stale

    async def refresh_now(
        self, name: str, *, actor: Optional[str] = None, trigger: str = "manual"
    ) -> None:
        await self._refresh(name, trigger=trigger, actor=actor)

    async def _ensure_background_refresh(self, name: str) -> None:
        b = self._buckets[name]
        if b.refreshing and not b.refreshing.done():
            return
        b.refreshing = asyncio.create_task(self._refresh(name, trigger="schedule", actor=None))

    async def _refresh(self, name: str, *, trigger: str, actor: Optional[str]) -> None:
        b = self._buckets[name]
        rt = _get_runtime_module()
        t0 = rt.monotonic_ms()
        result = "ok"
        err_text: Optional[str] = None
        first_err: Optional[str] = None
        retries = 0
        success = False
        new_val: Any = None
        ttl_expired = False
        try:
            age = b.age_sec()
        except Exception:
            age = None
        if b.last_refresh is None:
            ttl_expired = True
        elif isinstance(age, int):
            ttl_expired = age >= b.ttl_sec
        try:
            # run loader with async backoff (single retry on failure)
            try:
                with sheets_audit.log_read(
                    component="cache_service",
                    operation="refresh",
                    sheet_source="loader",
                    cache_bucket=name,
                    trigger=trigger,
                    cache_status="refresh",
                ) as audit_fields:
                    new_val = await b.loader()
                    audit_fields["result"] = new_val
                success = True
            except asyncio.CancelledError:
                # Propagate cancellation so shutdown isn't blocked
                result = "cancelled"
                err_text = "cancelled"
                raise
            except Exception as exc:
                retries = 1
                first_err = _errtext(exc)
                err_text = first_err
                b.last_error = first_err
                await asyncio.sleep(b.retry_delay_sec)
                try:
                    with sheets_audit.log_read(
                        component="cache_service",
                        operation="refresh_retry",
                        sheet_source="loader",
                        cache_bucket=name,
                        trigger=trigger,
                        cache_status="refresh_retry",
                    ) as audit_fields:
                        new_val = await b.loader()
                        audit_fields["result"] = new_val
                    success = True
                    result = "retry_ok"
                except asyncio.CancelledError:
                    result = "cancelled"
                    err_text = "cancelled"
                    raise
                except Exception as exc2:
                    second_err = _errtext(exc2)
                    if second_err in (None, "", "-"):
                        err_text = first_err
                    else:
                        err_text = f"{first_err} | retry: {second_err}"
                    result = "fail"
            if success:
                b.value = new_val
                b.last_refresh = dt.datetime.now(UTC)
                b.last_item_count = _count_items(new_val)
                err_text = None
        except asyncio.CancelledError:
            # Let the runtime cancel this task; finally will still run
            result = "cancelled"
            err_text = "cancelled"
            raise
        except Exception as e:
            result = "fail"
            err_text = _errtext(e)
        finally:
            b.last_latency_ms = rt.monotonic_ms() - t0
            b.last_result = result
            b.last_error = err_text
            b.last_retries = retries
            b.last_trigger = trigger
            b.last_ttl_expired = ttl_expired
            await self._log_refresh(b, trigger=trigger, actor=actor, retries=retries)
            # clear marker
            b.refreshing = None

    async def _log_refresh(self, b: CacheBucket, *, trigger: str, actor: Optional[str], retries: int) -> None:
        # Format: [refresh] bucket=clans trigger=schedule actor=@user duration=842ms result=ok hits=?,misses=?,retries=1
        normalized_trigger = "manual"
        if trigger in {"schedule", "cron"}:
            normalized_trigger = "cron"
        normalized_actor = actor or "-"
        if normalized_actor == "cron":
            normalized_actor = "scheduler"
        ttl_flag = "true" if b.last_ttl_expired else "false"
        count_text = "-"
        if isinstance(b.last_item_count, int):
            count_text = str(b.last_item_count)
        raw_result = (b.last_result or "fail").lower()
        result_flag = "ok" if raw_result in {"ok", "retry_ok"} else "fail"
        error_text = b.last_error or "-"
        msg = (
            f"[refresh] bucket={b.name} trigger={normalized_trigger} "
            f"actor={normalized_actor} duration={b.last_latency_ms or 0}ms "
            f"result={result_flag} retries={retries} "
            f"ttl_expired={ttl_flag} count={count_text} error={error_text or '-'}"
        )
        log.info(msg)


def _count_items(value: Any) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, dict):
        return len(value)
    if isinstance(value, (set, list, tuple)):
        return len(value)
    if hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
        try:
            return len(value)  # type: ignore[arg-type]
        except Exception:  # pragma: no cover - defensive guard
            return None
    return None

cache = CacheService()


def capabilities() -> Dict[str, Dict[str, Any]]:
    """Expose cache capabilities for convenience imports."""
    return cache.capabilities()


def register_onboarding_questions_bucket() -> None:
    """Ensure the onboarding questions cache bucket is registered."""

    if cache.get_bucket("onboarding_questions") is not None:
        return
    cache.register(
        "onboarding_questions",
        _ONBOARDING_QUESTIONS_TTL_SEC,
        _load_onboarding_questions,
    )


def get_bucket_snapshot(name: str) -> Dict[str, Any]:
    """Return a read-only snapshot for ``name`` (fail-soft)."""

    try:
        bucket = cache.get_bucket(name)
    except Exception:
        bucket = None

    if bucket is None:
        return {
            "name": name,
            "ttl_sec": None,
            "last_refresh_at": None,
            "next_refresh_at": None,
            "last_result": None,
            "last_error": f"unknown bucket: {name}",
        }

    next_refresh: Optional[dt.datetime]
    try:
        next_refresh = bucket.next_refresh_at()
    except Exception:
        next_refresh = None

    metadata: Dict[str, str] | None = None
    if bucket.name == "onboarding_questions":
        try:
            from shared.sheets.onboarding_questions import describe_source

            metadata = describe_source()
        except Exception:
            metadata = None

    snapshot: Dict[str, Any] = {
        "name": bucket.name,
        "ttl_sec": getattr(bucket, "ttl_sec", None),
        "last_refresh_at": getattr(bucket, "last_refresh", None),
        "next_refresh_at": next_refresh,
        "last_result": getattr(bucket, "last_result", None),
        "last_error": getattr(bucket, "last_error", None),
        "last_trigger": getattr(bucket, "last_trigger", None),
        "ttl_expired": getattr(bucket, "last_ttl_expired", None),
        "item_count": getattr(bucket, "last_item_count", None),
    }
    if metadata:
        snapshot["metadata"] = dict(metadata)
    return snapshot
