import asyncio
import time

from shared.sheets.read_broker import (
    ACTIVE_STATE,
    FRESH_REQUIRED,
    RUNTIME_CONFIG,
    CachePolicy,
    ReadPriority,
    SheetReadKey,
    SheetsReadBroker,
)


def _run(coro):
    return asyncio.run(coro)


def test_single_flight_coalesces_50_concurrent_identical_reads():
    async def scenario():
        broker = SheetsReadBroker(read_budget_rpm=60000)
        key = SheetReadKey.values("sheet-123456", "Config")
        calls = 0
        gate = asyncio.Event()

        async def loader():
            nonlocal calls
            calls += 1
            await gate.wait()
            return [["ok"]]

        tasks = [asyncio.create_task(broker.read(key, loader)) for _ in range(50)]
        await asyncio.sleep(0)
        gate.set()
        results = await asyncio.gather(*tasks)
        snap = broker.snapshot()
        await broker.close()
        assert results == [[["ok"]]] * 50
        assert calls == 1
        assert snap["coalesced_joins"] == 49
        assert snap["physical_reads"] == 1

    _run(scenario())


def test_different_keys_do_not_coalesce():
    async def scenario():
        broker = SheetsReadBroker(read_budget_rpm=60000)
        calls = []

        async def loader(name):
            calls.append(name)
            await asyncio.sleep(0)
            return name

        first, second = await asyncio.gather(
            broker.read(SheetReadKey.values("sheet-123456", "A"), lambda: loader("A")),
            broker.read(SheetReadKey.values("sheet-123456", "B"), lambda: loader("B")),
        )
        await broker.close()
        assert {first, second} == {"A", "B"}
        assert sorted(calls) == ["A", "B"]

    _run(scenario())


def test_fresh_cache_hit_avoids_second_loader_call():
    async def scenario():
        now = [100.0]
        broker = SheetsReadBroker(read_budget_rpm=60000, clock_fn=lambda: now[0])
        key = SheetReadKey.records("sheet-123456", "Config")
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            return {"version": calls}

        first = await broker.read(key, loader, policy=RUNTIME_CONFIG)
        now[0] += 30
        second = await broker.read(key, loader, policy=RUNTIME_CONFIG)
        snap = broker.snapshot()
        await broker.close()
        assert first == second == {"version": 1}
        assert calls == 1
        assert snap["cache_hits"] == 1

    _run(scenario())


def test_stale_while_revalidate_returns_stale_and_runs_one_refresh():
    async def scenario():
        now = [100.0]
        policy = CachePolicy("TEST", fresh_ttl_sec=10, stale_ttl_sec=100)
        broker = SheetsReadBroker(read_budget_rpm=60000, clock_fn=lambda: now[0])
        key = SheetReadKey.values("sheet-123456", "State")
        calls = 0
        refresh_gate = asyncio.Event()

        async def loader():
            nonlocal calls
            calls += 1
            if calls > 1:
                await refresh_gate.wait()
            return calls

        assert await broker.read(key, loader, policy=policy) == 1
        now[0] += 20
        stale_results = await asyncio.gather(
            *(broker.read(key, loader, policy=policy) for _ in range(10))
        )
        assert stale_results == [1] * 10
        await asyncio.sleep(0)
        assert calls == 2
        refresh_gate.set()
        await broker.wait_for_idle()
        assert await broker.read(key, loader, policy=policy) == 2
        snap = broker.snapshot()
        await broker.close()
        assert snap["stale_hits"] == 10
        assert snap["physical_reads"] == 2

    _run(scenario())


def test_expired_stale_window_waits_for_refresh():
    async def scenario():
        now = [100.0]
        policy = CachePolicy("TEST", fresh_ttl_sec=10, stale_ttl_sec=20)
        broker = SheetsReadBroker(read_budget_rpm=60000, clock_fn=lambda: now[0])
        key = SheetReadKey.values("sheet-123456", "State")
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            return calls

        assert await broker.read(key, loader, policy=policy) == 1
        now[0] += 25
        assert await broker.read(key, loader, policy=policy) == 2
        await broker.close()
        assert calls == 2

    _run(scenario())


def test_fresh_required_never_reuses_stale_cache():
    async def scenario():
        broker = SheetsReadBroker(read_budget_rpm=60000)
        key = SheetReadKey.values("sheet-123456", "State")
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            return calls

        assert await broker.read(key, loader, policy=ACTIVE_STATE) == 1
        assert await broker.read(key, loader, policy=FRESH_REQUIRED) == 2
        await broker.close()
        assert calls == 2

    _run(scenario())


def test_failed_loader_cleans_inflight_and_later_read_can_succeed():
    async def scenario():
        broker = SheetsReadBroker(read_budget_rpm=60000, retry_attempts=1)
        key = SheetReadKey.values("sheet-123456", "State")
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("boom")
            return "ok"

        try:
            await broker.read(key, loader)
        except RuntimeError as exc:
            assert str(exc) == "boom"
        else:
            raise AssertionError("first loader call should fail")
        await asyncio.sleep(0)
        assert await broker.read(key, loader) == "ok"
        await broker.close()
        assert calls == 2

    _run(scenario())


def test_429_retries_with_async_backoff_and_counts_each_physical_attempt():
    async def scenario():
        sleeps = []
        calls = 0

        class QuotaError(Exception):
            status_code = 429

        async def fake_sleep(delay):
            sleeps.append(delay)

        broker = SheetsReadBroker(
            read_budget_rpm=60000,
            retry_attempts=3,
            retry_base_delay_sec=0.2,
            retry_factor=2,
            retry_max_delay_sec=1,
            sleep_fn=fake_sleep,
            jitter_fn=lambda _low, _high: 1.0,
        )
        key = SheetReadKey.values("sheet-123456", "State")

        async def loader():
            nonlocal calls
            calls += 1
            if calls < 3:
                raise QuotaError("RESOURCE_EXHAUSTED")
            return "ok"

        assert await broker.read(key, loader) == "ok"
        snap = broker.snapshot()
        await broker.close()
        assert calls == 3
        assert snap["physical_reads"] == 3
        assert snap["rate_limit_errors"] == 2
        assert snap["retries"] == 2
        assert any(abs(delay - 0.2) < 1e-9 for delay in sleeps)
        assert any(abs(delay - 0.4) < 1e-9 for delay in sleeps)

    _run(scenario())


def test_exact_invalidation_forces_reload():
    async def scenario():
        broker = SheetsReadBroker(read_budget_rpm=60000)
        key = SheetReadKey.records("sheet-123456", "Config")
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            return calls

        assert await broker.read(key, loader) == 1
        assert await broker.read(key, loader) == 1
        assert await broker.invalidate(key) == 1
        assert await broker.read(key, loader) == 2
        await broker.close()
        assert calls == 2

    _run(scenario())


def test_worksheet_and_workbook_invalidation_are_scoped():
    async def scenario():
        broker = SheetsReadBroker(read_budget_rpm=60000)
        keys = [
            SheetReadKey.values("sheet-a", "One"),
            SheetReadKey.range("sheet-a", "One", "A1:B2"),
            SheetReadKey.values("sheet-a", "Two"),
            SheetReadKey.values("sheet-b", "One"),
        ]

        async def loader_for(value):
            return value

        for idx, key in enumerate(keys):
            assert await broker.read(key, lambda idx=idx: loader_for(idx)) == idx
        assert await broker.invalidate_worksheet("sheet-a", "One") == 2
        assert broker.snapshot()["cache_entries"] == 2
        assert await broker.invalidate_workbook("sheet-a") == 1
        assert broker.snapshot()["cache_entries"] == 1
        await broker.close()

    _run(scenario())


def test_critical_request_advances_ahead_of_queued_background_work():
    async def scenario():
        order = []
        broker = SheetsReadBroker(
            read_budget_rpm=100,
            rate_window_seconds=0.1,
        )

        async def loader(name):
            order.append(name)
            return name

        first = asyncio.create_task(
            broker.read(
                SheetReadKey.values("sheet-1", "bg-1"),
                lambda: loader("bg-1"),
                policy=FRESH_REQUIRED,
                priority=ReadPriority.BACKGROUND,
            )
        )
        await asyncio.sleep(0)
        second = asyncio.create_task(
            broker.read(
                SheetReadKey.values("sheet-1", "bg-2"),
                lambda: loader("bg-2"),
                policy=FRESH_REQUIRED,
                priority=ReadPriority.BACKGROUND,
            )
        )
        third = asyncio.create_task(
            broker.read(
                SheetReadKey.values("sheet-1", "bg-3"),
                lambda: loader("bg-3"),
                policy=FRESH_REQUIRED,
                priority=ReadPriority.BACKGROUND,
            )
        )
        critical = asyncio.create_task(
            broker.read(
                SheetReadKey.values("sheet-1", "critical"),
                lambda: loader("critical"),
                policy=FRESH_REQUIRED,
                priority=ReadPriority.CRITICAL,
            )
        )
        await asyncio.gather(first, second, third, critical)
        await broker.close()
        assert order[0] == "bg-1"
        assert order[1] == "critical"
        assert set(order[2:]) == {"bg-2", "bg-3"}

    _run(scenario())


def test_read_budget_paces_concurrent_physical_requests():
    async def scenario():
        broker = SheetsReadBroker(
            read_budget_rpm=2,
            rate_window_seconds=0.04,
            retry_attempts=1,
        )
        starts = []

        async def loader(value):
            starts.append(time.monotonic())
            return value

        started = time.monotonic()
        results = await asyncio.gather(
            *(
                broker.read(
                    SheetReadKey.values("sheet-budget", f"tab-{idx}"),
                    lambda idx=idx: loader(idx),
                    policy=FRESH_REQUIRED,
                )
                for idx in range(4)
            )
        )
        elapsed = time.monotonic() - started
        await broker.close()
        assert results == [0, 1, 2, 3]
        assert len(starts) == 4
        assert elapsed >= 0.05
        gaps = [later - earlier for earlier, later in zip(starts, starts[1:])]
        assert all(gap >= 0.015 for gap in gaps)

    _run(scenario())


def test_cancelled_waiter_does_not_cancel_shared_refresh():
    async def scenario():
        broker = SheetsReadBroker(read_budget_rpm=60000)
        key = SheetReadKey.values("sheet-123456", "State")
        gate = asyncio.Event()
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            await gate.wait()
            return "ok"

        first = asyncio.create_task(broker.read(key, loader))
        second = asyncio.create_task(broker.read(key, loader))
        await asyncio.sleep(0)
        second.cancel()
        try:
            await second
        except asyncio.CancelledError:
            pass
        gate.set()
        assert await first == "ok"
        assert calls == 1
        await broker.close()

    _run(scenario())


def test_invalidating_during_inflight_read_prevents_old_result_from_repopulating_cache():
    async def scenario():
        broker = SheetsReadBroker(read_budget_rpm=60000)
        key = SheetReadKey.values("sheet-123456", "State")
        gate = asyncio.Event()
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            current = calls
            if current == 1:
                await gate.wait()
            return current

        old_read = asyncio.create_task(broker.read(key, loader))
        await asyncio.sleep(0)
        assert await broker.invalidate(key) == 1
        gate.set()
        assert await old_read == 1
        await asyncio.sleep(0)
        assert await broker.read(key, loader) == 2
        await broker.close()
        assert calls == 2

    _run(scenario())


def test_stale_refresh_failure_keeps_last_good_value_available():
    async def scenario():
        now = [100.0]
        policy = CachePolicy("TEST", fresh_ttl_sec=10, stale_ttl_sec=100)
        broker = SheetsReadBroker(
            read_budget_rpm=60000,
            retry_attempts=1,
            clock_fn=lambda: now[0],
        )
        key = SheetReadKey.values("sheet-123456", "State")
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            if calls == 1:
                return "good"
            raise RuntimeError("refresh failed")

        assert await broker.read(key, loader, policy=policy) == "good"
        now[0] += 20
        assert await broker.read(key, loader, policy=policy) == "good"
        await broker.wait_for_idle()
        assert calls == 2
        assert await broker.read(key, loader, policy=policy) == "good"
        await broker.close()

    _run(scenario())


def test_priority_anti_starvation_serves_lower_priority_after_urgent_burst():
    async def scenario():
        order = []
        broker = SheetsReadBroker(
            read_budget_rpm=10000,
            rate_window_seconds=0.1,
        )

        async def loader(name):
            order.append(name)
            return name

        first = asyncio.create_task(
            broker.read(
                SheetReadKey.values("sheet-starve", "first"),
                lambda: loader("first"),
                policy=FRESH_REQUIRED,
                priority=ReadPriority.CRITICAL,
            )
        )
        await asyncio.sleep(0)
        background = asyncio.create_task(
            broker.read(
                SheetReadKey.values("sheet-starve", "background"),
                lambda: loader("background"),
                policy=FRESH_REQUIRED,
                priority=ReadPriority.BACKGROUND,
            )
        )
        urgent = [
            asyncio.create_task(
                broker.read(
                    SheetReadKey.values("sheet-starve", f"urgent-{idx}"),
                    lambda idx=idx: loader(f"urgent-{idx}"),
                    policy=FRESH_REQUIRED,
                    priority=ReadPriority.CRITICAL,
                )
            )
            for idx in range(12)
        ]
        await asyncio.gather(first, background, *urgent)
        await broker.close()
        background_index = order.index("background")
        assert background_index <= 9

    _run(scenario())


def test_rate_limit_detection_covers_resource_exhausted_text_and_status_code():
    from shared.sheets.read_broker import is_rate_limited_error

    class StatusQuota(Exception):
        status_code = 429

    assert is_rate_limited_error(StatusQuota("anything")) is True
    assert (
        is_rate_limited_error(RuntimeError("RESOURCE_EXHAUSTED: read requests per minute"))
        is True
    )
    assert is_rate_limited_error(RuntimeError("ordinary failure")) is False


def test_caller_metadata_does_not_change_canonical_cache_identity():
    async def scenario():
        broker = SheetsReadBroker(read_budget_rpm=60000)
        key = SheetReadKey.records("sheet-123456", "Config")
        calls = 0

        async def loader():
            nonlocal calls
            calls += 1
            return "shared"

        assert await broker.read(
            key, loader, component="reset_reminders", reason="scheduler"
        ) == "shared"
        assert await broker.read(
            key, loader, component="fusion", reason="interaction"
        ) == "shared"
        await broker.close()
        assert calls == 1

    _run(scenario())
