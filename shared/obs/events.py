"""Centralized helpers for humanized event emissions."""

from __future__ import annotations

import hashlib
import time
from typing import Iterable, Sequence, TYPE_CHECKING

from shared.dedupe import EventDeduper
from shared.logfmt import BucketResult, LogTemplates, human_reason

if TYPE_CHECKING:  # pragma: no cover - typing only
    from shared.cache.telemetry import RefreshResult

__all__ = [
    "refresh_deduper",
    "refresh_dedupe_key",
    "refresh_bucket_results",
    "format_refresh_message",
]


_REFRESH_DEDUPER = EventDeduper()


def refresh_deduper() -> EventDeduper:
    return _REFRESH_DEDUPER


def _normalize_status(result: "RefreshResult") -> str:
    snapshot = getattr(result, "snapshot", None)
    raw = None
    if snapshot is not None:
        raw = getattr(snapshot, "last_result", None)
    if raw is None:
        raw = getattr(result, "status", None)
    if raw is None:
        raw = "ok" if getattr(result, "ok", False) else "fail"
    text = str(raw).replace("_", " ").strip()
    return text or ("ok" if getattr(result, "ok", False) else "fail")


def refresh_bucket_results(results: Sequence["RefreshResult"]) -> list[BucketResult]:
    buckets: list[BucketResult] = []
    for item in results:
        snapshot = getattr(item, "snapshot", None)
        status = _normalize_status(item)
        duration_s = (getattr(item, "duration_ms", 0) or 0) / 1000.0
        retries = getattr(item, "retries", None)
        if snapshot is None:
            reason = human_reason(getattr(item, "error", None)) if not getattr(item, "ok", False) else None
            buckets.append(
                BucketResult(
                    name=getattr(item, "name", "unknown"),
                    status=status,
                    duration_s=duration_s,
                    item_count=None,
                    ttl_ok=None,
                    retries=retries,
                    reason=reason,
                    metadata=None,
                    cache_age_s=None,
                    ttl_s=None,
                    last_refresh_at=None,
                )
            )
            continue
        ttl_expired_before_refresh = snapshot.ttl_expired
        currently_stale_after_refresh = bool(ttl_expired_before_refresh and not getattr(item, "ok", False))
        ttl_ok: bool | None = None
        if currently_stale_after_refresh is True:
            ttl_ok = False
        elif ttl_expired_before_refresh is False or getattr(item, "ok", False):
            ttl_ok = True
        reason = None
        if not getattr(item, "ok", False):
            reason = human_reason(getattr(item, "error", None) or snapshot.last_error)
        if getattr(item, "ok", False):
            if ttl_expired_before_refresh is True:
                status = "refreshed"
            elif ttl_expired_before_refresh is False:
                status = "fresh"
        elif ttl_expired_before_refresh is True:
            status = "stale"
        metadata = snapshot.metadata
        if snapshot.name == "onboarding_questions" and metadata:
            metadata = {k: v for k, v in metadata.items() if k not in {"sheet", "tab"}}
            if not metadata:
                metadata = None

        buckets.append(
            BucketResult(
                name=getattr(item, "name", None) or snapshot.name,
                status=status,
                duration_s=duration_s,
                item_count=snapshot.item_count,
                ttl_ok=ttl_ok,
                retries=retries,
                reason=reason,
                metadata=metadata,
                cache_age_s=snapshot.age_seconds,
                ttl_s=snapshot.ttl_seconds,
                last_refresh_at=snapshot.last_refresh_at,
                ttl_expired_before_refresh=ttl_expired_before_refresh,
                currently_stale_after_refresh=currently_stale_after_refresh,
            )
        )
    return buckets


def refresh_dedupe_key(scope: str, snapshot_id: str | None, bucket_names: Iterable[str]) -> str:
    names = sorted(str(name) for name in bucket_names)
    window = max(int(_REFRESH_DEDUPER.window) or 1, 1)
    if snapshot_id:
        token = snapshot_id
    else:
        ts_bucket = int(time.time() // window)
        digest = hashlib.sha1("/".join(names).encode("utf-8")).hexdigest()[:8]
        token = f"{ts_bucket}:{digest}"
    return f"refresh:{scope}:{token}"


def format_refresh_message(scope: str, buckets: Sequence[BucketResult], total_s: float | None = None) -> str:
    total = total_s if total_s is not None else sum(item.duration_s for item in buckets)
    return LogTemplates.select_refresh_template(scope, buckets, total)
