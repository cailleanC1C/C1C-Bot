import asyncio

from shared.sheets.cache_service import CacheService


def test_successful_cache_refresh_clears_previous_error(monkeypatch):
    cache = CacheService()
    calls = 0

    async def no_sleep(_seconds):
        return None

    async def loader():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("old failure")
        return ["ok"]

    monkeypatch.setattr("shared.sheets.cache_service.asyncio.sleep", no_sleep)
    cache.register("bucket", 60, loader)

    asyncio.run(cache.refresh_now("bucket", actor="test"))
    bucket = cache.get_bucket("bucket")
    assert bucket.last_result == "retry_ok"
    assert bucket.last_error is None
    assert bucket.value == ["ok"]
