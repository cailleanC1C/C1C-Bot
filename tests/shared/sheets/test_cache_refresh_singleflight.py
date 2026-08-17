import asyncio

from shared.sheets.cache_service import CacheService


def test_refresh_now_joins_background_refresh_started_by_get():
    async def run():
        cache = CacheService()
        calls = 0
        started = asyncio.Event()
        release = asyncio.Event()

        async def loader():
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return ["ok"]

        cache.register("bucket", 60, loader)
        assert await cache.get("bucket") is None
        await started.wait()

        waiter = asyncio.create_task(cache.refresh_now("bucket", actor="test"))
        await asyncio.sleep(0)
        assert calls == 1
        release.set()
        await waiter

        bucket = cache.get_bucket("bucket")
        assert calls == 1
        assert bucket.value == ["ok"]
        assert bucket.last_result == "ok"

    asyncio.run(run())


def test_cache_layer_does_not_retry_rate_limit_after_broker_exhaustion(monkeypatch):
    async def run():
        cache = CacheService()
        calls = 0
        sleeps = []

        class QuotaError(Exception):
            status_code = 429

        async def loader():
            nonlocal calls
            calls += 1
            raise QuotaError("429 RESOURCE_EXHAUSTED")

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr("shared.sheets.cache_service.asyncio.sleep", fake_sleep)
        cache.register("bucket", 60, loader, retry_delay_sec=1)
        await cache.refresh_now("bucket", actor="test")

        bucket = cache.get_bucket("bucket")
        assert calls == 1
        assert sleeps == []
        assert bucket.last_result == "fail"
        assert bucket.last_retries == 0

    asyncio.run(run())
