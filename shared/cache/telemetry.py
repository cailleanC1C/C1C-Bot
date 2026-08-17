"""Public cache telemetry wrapper for CoreOps and shared tooling."""
from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import dataclass
from typing import Dict, List, Mapping, Optional, Sequence

from shared.sheets import cache_service
from shared.utils import humanize_duration

UTC = dt.timezone.utc

# Runtime startup already waits 15 seconds before entering the cache preloader.
# Pace the remaining warmup across ~285 seconds so non-critical cache work lands
# near the five-minute mark instead of producing an 8-second-spaced read burst.
_STARTUP_WARMUP_WINDOW_SECONDS = 285.0
_STARTUP_WARMUP_RESET_SECONDS = 360.0
_STARTUP_WARMUP_STARTED_AT: float | None = None
_STARTUP_WARMUP_OFFSETS: dict[str, float] = {}

# The broker still owns physical request priority and pacing. These bands only
# control when background startup cache demand is introduced into that broker.
_STARTUP_CRITICAL_BUCKETS = {
    "clans",
    "clan_tags",
    "onboarding_questions",
}
_STARTUP_BACKGROUND_TOKENS = (
    "achievement",
    "audit",
    "history",
    "realmwalker",
    "wandering",
)
_STARTUP_ACTIVE_TOKENS = (
    "fusion",
    "live_arena",
    "placement",
    "recruitment",
    "reminder",
    "reset",
    "shard",
)
_STARTUP_SUPPORT_TOKENS = (
    "guide",
    "help",
    "league",
    "server_map",
    "template",
)


@dataclass(frozen=True)
class CacheSnapshot:
    """Immutable view of a cache bucket's telemetry."""

    name: str
    available: bool
    ttl_seconds: Optional[int]
    ttl_human: Optional[str]
    ttl_sec: Optional[int]
    last_refresh_at: Optional[dt.datetime]
    age_seconds: Optional[int]
    age_human: Optional[str]
    age_sec: Optional[int]
    next_refresh_at: Optional[dt.datetime]
    next_refresh_delta_seconds: Optional[int]
    next_refresh_human: Optional[str]
    last_result: Optional[str]
    last_error: Optional[str]
    retries: Optional[int]
    last_trigger: Optional[str]
    ttl_expired: Optional[bool]
    item_count: Optional[int]
    metadata: Optional[Mapping[str, str]] = None


@dataclass(frozen=True)
class RefreshResult:
    """Result metadata for a manual refresh attempt."""

    name: str
    ok: bool
    duration_ms: Optional[int]
    error: Optional[str]
    retries: Optional[int]
    snapshot: CacheSnapshot


def _now_utc() -> dt.datetime:
    return dt.datetime.now(UTC)


def _normalize_datetime(value: object) -> Optional[dt.datetime]:
    if isinstance(value, dt.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return None


def _to_int(value: object) -> Optional[int]:
    if isinstance(value, (int, float)):
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
    return None


def _clean_text(value: object) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _build_snapshot(name: str, raw: Optional[Dict[str, object]]) -> CacheSnapshot:
    available = isinstance(raw, dict)
    ttl_seconds = _to_int(raw.get("ttl_sec")) if available else None
    last_refresh_at = _normalize_datetime(raw.get("last_refresh_at")) if available else None
    next_refresh_at = _normalize_datetime(raw.get("next_refresh_at")) if available else None
    last_result = _clean_text(raw.get("last_result")) if available else None
    last_error = _clean_text(raw.get("last_error")) if available else None
    retries = _to_int(raw.get("retries")) if available else None
    last_trigger = _clean_text(raw.get("last_trigger")) if available else None
    ttl_expired: Optional[bool] = None
    if available:
        value = raw.get("ttl_expired")
        if isinstance(value, bool):
            ttl_expired = value
    item_count = _to_int(raw.get("item_count")) if available else None

    now = _now_utc()
    age_seconds: Optional[int] = None
    if last_refresh_at is not None:
        try:
            delta = now - last_refresh_at
            age_seconds = max(0, int(delta.total_seconds()))
        except Exception:
            age_seconds = None

    next_delta: Optional[int] = None
    if next_refresh_at is not None:
        try:
            delta = next_refresh_at - now
            next_delta = int(delta.total_seconds())
        except Exception:
            next_delta = None

    ttl_human = humanize_duration(ttl_seconds) if ttl_seconds is not None else None
    age_human = humanize_duration(age_seconds) if age_seconds is not None else None
    next_human = None
    if next_delta is not None:
        next_human = humanize_duration(abs(next_delta))

    metadata: Optional[Mapping[str, str]] = None
    if available:
        meta_raw = raw.get("metadata")
        if isinstance(meta_raw, Mapping):
            cleaned: Dict[str, str] = {}
            for key, value in meta_raw.items():
                key_text = str(key).strip()
                val_text = str(value).strip()
                if key_text and val_text:
                    cleaned[key_text] = val_text
            if cleaned:
                metadata = cleaned

    return CacheSnapshot(
        name=name,
        available=available,
        ttl_seconds=ttl_seconds,
        ttl_human=ttl_human,
        ttl_sec=ttl_seconds,
        last_refresh_at=last_refresh_at,
        age_seconds=age_seconds,
        age_human=age_human,
        age_sec=age_seconds,
        next_refresh_at=next_refresh_at,
        next_refresh_delta_seconds=next_delta,
        next_refresh_human=next_human,
        last_result=last_result,
        last_error=last_error,
        retries=retries,
        last_trigger=last_trigger,
        ttl_expired=ttl_expired,
        item_count=item_count,
        metadata=metadata,
    )


def startup_priority(name: str) -> int:
    """Return startup warmup band for a cache bucket (0=earliest, 3=latest)."""

    normalized = str(name or "").strip().lower()
    if normalized in _STARTUP_CRITICAL_BUCKETS or "config" in normalized:
        return 0
    if any(token in normalized for token in _STARTUP_ACTIVE_TOKENS):
        return 1
    if any(token in normalized for token in _STARTUP_BACKGROUND_TOKENS):
        return 3
    if any(token in normalized for token in _STARTUP_SUPPORT_TOKENS):
        return 2
    return 2


def startup_order(names: Sequence[str]) -> list[str]:
    """Return deterministic cache warmup order with operational data first."""

    return sorted(
        (str(name) for name in names if str(name).strip()),
        key=lambda name: (startup_priority(name), name),
    )


def startup_warmup_offsets(
    names: Sequence[str],
    *,
    window_seconds: float = _STARTUP_WARMUP_WINDOW_SECONDS,
) -> dict[str, float]:
    """Build deterministic target offsets for startup cache refreshes.

    Bands intentionally leave the first operational resources near the beginning
    of the warmup while pushing support/background work toward the five-minute
    boundary. The runtime's existing 15-second initial delay is outside this
    window.
    """

    ordered = startup_order(names)
    if not ordered:
        return {}

    window = max(0.0, float(window_seconds))
    band_edges = {
        0: (0.0, window * (30.0 / 285.0)),
        1: (window * (30.0 / 285.0), window * (90.0 / 285.0)),
        2: (window * (90.0 / 285.0), window * (180.0 / 285.0)),
        3: (window * (180.0 / 285.0), window),
    }
    grouped: dict[int, list[str]] = {0: [], 1: [], 2: [], 3: []}
    for name in ordered:
        grouped[startup_priority(name)].append(name)

    offsets: dict[str, float] = {}
    for priority in range(4):
        group = grouped[priority]
        if not group:
            continue
        start, end = band_edges[priority]
        span = max(0.0, end - start)
        if priority == 0:
            # Critical cache data begins immediately; spread the remainder of the
            # band without deliberately delaying the first usable snapshot.
            denominator = max(1, len(group))
            for index, name in enumerate(group):
                offsets[name] = start + span * (index / denominator)
        else:
            # Non-critical bands finish at their boundary. With one bucket this
            # deliberately places it at the end of its band instead of clustering
            # everything at startup.
            denominator = max(1, len(group))
            for index, name in enumerate(group, start=1):
                offsets[name] = start + span * (index / denominator)
    return offsets


def _registered_bucket_names() -> List[str]:
    try:
        caps = cache_service.capabilities()
    except Exception:
        return []
    names: List[str] = []
    for key in caps.keys():
        if isinstance(key, str):
            names.append(key)
    return names


def list_buckets() -> List[str]:
    """Return registered bucket names in startup-safe priority order."""

    return startup_order(_registered_bucket_names())


async def _await_startup_warmup_slot(bucket: str) -> None:
    global _STARTUP_WARMUP_STARTED_AT, _STARTUP_WARMUP_OFFSETS

    now = time.monotonic()
    if (
        _STARTUP_WARMUP_STARTED_AT is None
        or now - _STARTUP_WARMUP_STARTED_AT > _STARTUP_WARMUP_RESET_SECONDS
        or bucket not in _STARTUP_WARMUP_OFFSETS
    ):
        _STARTUP_WARMUP_STARTED_AT = now
        _STARTUP_WARMUP_OFFSETS = startup_warmup_offsets(_registered_bucket_names())

    target = float(_STARTUP_WARMUP_OFFSETS.get(bucket, 0.0))
    elapsed = max(0.0, time.monotonic() - _STARTUP_WARMUP_STARTED_AT)
    delay = max(0.0, target - elapsed)
    if delay > 0:
        await asyncio.sleep(delay)


def get_snapshot(name: str) -> CacheSnapshot:
    """Return telemetry snapshot for ``name`` (fail-soft)."""

    raw: Optional[Dict[str, object]] = None
    try:
        data = cache_service.get_bucket_snapshot(name)
    except Exception:
        data = None
    if isinstance(data, dict):
        raw = data
    return _build_snapshot(name, raw)


def get_all_snapshots() -> Dict[str, CacheSnapshot]:
    """Return telemetry snapshots for all known buckets."""

    snapshots: Dict[str, CacheSnapshot] = {}
    for name in list_buckets():
        raw: Optional[Dict[str, object]] = None
        try:
            candidate = cache_service.get_bucket_snapshot(name)
        except Exception:
            candidate = None
        if isinstance(candidate, dict):
            raw = candidate
        snapshots[name] = _build_snapshot(name, raw)
    return snapshots


def _format_exception(exc: BaseException) -> str:
    message = str(exc).strip().strip("\"")
    if message:
        return f"{type(exc).__name__}: {message}"
    return type(exc).__name__


def _normalize_bucket_name(name: str) -> str:
    cleaned = name.strip()
    if not cleaned:
        raise ValueError("cache bucket name must be non-empty")
    return cleaned


def _derive_error_text(snapshot: CacheSnapshot) -> Optional[str]:
    if snapshot.last_error:
        return snapshot.last_error
    if snapshot.last_result and snapshot.last_result.lower().startswith("fail"):
        return snapshot.last_result
    return None


async def refresh_now(name: str, actor: Optional[str] = None) -> RefreshResult:
    """Trigger an immediate refresh and return result metadata.

    Notes:
        Some cache loaders record failures in the bucket snapshot (last_result/last_error)
        without raising. We therefore inspect the snapshot after the call and flip `ok`
        accordingly to avoid reporting a false success.
    """

    bucket = _normalize_bucket_name(name)
    if actor == "startup":
        await _await_startup_warmup_slot(bucket)

    start = time.monotonic()
    error_text: Optional[str] = None
    ok = True
    trigger = "cron" if actor in {"cron", "scheduler"} else "manual"
    try:
        await cache_service.cache.refresh_now(bucket, trigger=trigger, actor=actor)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # pragma: no cover - defensive guard
        ok = False
        error_text = _format_exception(exc)
    duration_ms = int((time.monotonic() - start) * 1000)
    snapshot = get_snapshot(bucket)
    retries = snapshot.retries
    if retries is not None:
        try:
            retries = int(retries)
        except (TypeError, ValueError):  # pragma: no cover - defensive guard
            retries = None
    if ok:
        derived = _derive_error_text(snapshot)
        if derived:
            ok = False
            error_text = derived
    if error_text:
        error_text = error_text.strip().strip("\"")
    return RefreshResult(
        name=bucket,
        ok=ok,
        duration_ms=duration_ms,
        error=error_text,
        retries=retries,
        snapshot=snapshot,
    )