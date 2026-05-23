import asyncio
import datetime as dt
from types import SimpleNamespace

from modules.common import runtime
from shared.cache.telemetry import CacheSnapshot, RefreshResult


def _snapshot(*, ttl_expired: bool) -> CacheSnapshot:
    return CacheSnapshot(
        name="clans",
        available=True,
        ttl_seconds=3600 * 1000,
        ttl_human="1h",
        ttl_sec=3600 * 1000,
        last_refresh_at=dt.datetime(2026, 5, 23, 9, 11, tzinfo=dt.timezone.utc),
        age_seconds=3 * 3600 * 1000,
        age_human="3h",
        age_sec=3 * 3600 * 1000,
        next_refresh_at=None,
        next_refresh_delta_seconds=None,
        next_refresh_human=None,
        last_result="ok",
        last_error=None,
        retries=0,
        last_trigger="manual",
        ttl_expired=ttl_expired,
        item_count=24,
        metadata=None,
    )


def test_startup_preload_rows_use_status_labels_and_details(monkeypatch) -> None:
    async def runner() -> None:
        async def no_sleep(_seconds: float) -> None:
            return None

        async def fake_refresh_now(name: str, actor: str):
            if name == "clans":
                return RefreshResult(name=name, ok=True, duration_ms=1500, error=None, retries=0, snapshot=_snapshot(ttl_expired=True))
            if name == "templates":
                return RefreshResult(name=name, ok=False, duration_ms=1200, error="boom", retries=0, snapshot=_snapshot(ttl_expired=True))
            return RefreshResult(name=name, ok=True, duration_ms=700, error=None, retries=0, snapshot=_snapshot(ttl_expired=False))

        monkeypatch.setattr(runtime.asyncio, "sleep", no_sleep)
        monkeypatch.setattr("shared.cache.telemetry.list_buckets", lambda: ["clans", "templates", "onboarding_questions"])
        monkeypatch.setattr("shared.cache.telemetry.refresh_now", fake_refresh_now)

        report = await runtime._startup_preload(bot=SimpleNamespace())

        assert any("clans refreshed" in f"{row['name']} {row['status']}" for row in report.rows)
        assert any("templates stale" in f"{row['name']} {row['status']}" for row in report.rows)
        assert any("onboarding_questions fresh" in f"{row['name']} {row['status']}" for row in report.rows)

        clans_row = next(row for row in report.rows if row["name"] == "clans")
        assert "ttl_expired" in clans_row["detail_parts"]
        assert "age=3h" in clans_row["detail_parts"]
        assert "ttl=1h" in clans_row["detail_parts"]

        templates_row = next(row for row in report.rows if row["name"] == "templates")
        assert "refresh_failed" in templates_row["detail_parts"]

    asyncio.run(runner())
