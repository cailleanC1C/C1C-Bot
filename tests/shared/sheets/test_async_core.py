import asyncio
import time

import shared.sheets.async_core as async_core
import shared.sheets.core as core


def test_a_to_thread_with_backoff_does_not_use_sync_retry_inside_event_loop(monkeypatch):
    sync_retry_calls = []

    def fail_sync_retry(*_args, **_kwargs):
        sync_retry_calls.append(True)
        raise AssertionError("sync retry helper must not run inside the event loop")

    async def fake_async_retry(func, *args, **_kwargs):
        return await func(*args)

    monkeypatch.setattr(core, "_retry_with_backoff", fail_sync_retry)
    monkeypatch.setattr(core, "_retry_with_backoff_async", fake_async_retry)

    result = asyncio.run(async_core.a_to_thread_with_backoff(lambda: "ok"))

    assert result == "ok"
    assert sync_retry_calls == []


def test_a_to_thread_with_backoff_can_be_awaited_inside_active_event_loop(monkeypatch):
    async def fake_arun(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(core.async_adapter, "arun", fake_arun)

    async def run_inside_loop():
        return await async_core.a_to_thread_with_backoff(lambda value: f"{value}-ok", "loop")

    assert asyncio.run(run_inside_loop()) == "loop-ok"


def test_a_to_thread_with_backoff_retries_with_async_sleep_not_time_sleep(monkeypatch):
    calls = []
    sleeps = []

    class QuotaError(Exception):
        status_code = 429

    def flaky_call():
        calls.append("call")
        if len(calls) == 1:
            raise QuotaError("429 quota exceeded")
        return "retried"

    async def fake_arun(func, *args, **kwargs):
        return func(*args, **kwargs)

    async def fake_async_sleep(delay):
        sleeps.append(delay)

    def fail_time_sleep(_delay):
        raise AssertionError("async Sheets retry must not use time.sleep")

    monkeypatch.setattr(core.async_adapter, "arun", fake_arun)
    monkeypatch.setattr(core.asyncio, "sleep", fake_async_sleep)
    monkeypatch.setattr(time, "sleep", fail_time_sleep)

    result = asyncio.run(
        async_core.a_to_thread_with_backoff(
            flaky_call,
            attempts=2,
            base_delay=0.25,
            factor=2,
        )
    )

    assert result == "retried"
    assert calls == ["call", "call"]
    assert len(sleeps) == 1
    assert 0.1875 <= sleeps[0] <= 0.3125
