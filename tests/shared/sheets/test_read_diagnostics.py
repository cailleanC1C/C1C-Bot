from __future__ import annotations

import asyncio

from shared.sheets.read_broker import FRESH_REQUIRED, SheetReadKey, SheetsReadBroker
from shared.sheets.read_diagnostics import (
    PhysicalReadDiagnostics,
    health_snapshot,
    install,
)


def _broker() -> SheetsReadBroker:
    return SheetsReadBroker(
        read_budget_rpm=1_000_000,
        rate_window_seconds=0.001,
        retry_attempts=2,
        retry_base_delay_sec=0,
    )


def test_physical_attempts_are_attributed_by_component_and_reason() -> None:
    async def scenario() -> dict[str, object]:
        broker = _broker()
        collector = PhysicalReadDiagnostics(window_sec=300)
        install(target_broker=broker, collector=collector)

        calls = 0

        async def loader() -> list[str]:
            nonlocal calls
            calls += 1
            return ["ok"]

        try:
            result = await broker.read(
                SheetReadKey.values("sheet-123", "CONFIG"),
                loader,
                policy=FRESH_REQUIRED,
                component="live_arena",
                reason="startup_sync",
            )
            assert result == ["ok"]
            assert calls == 1
            return collector.snapshot()
        finally:
            await broker.close()

    snapshot = asyncio.run(scenario())

    assert snapshot["physical_reads"] == 1
    assert snapshot["failures"] == 0
    assert snapshot["rate_limited"] == 0
    assert snapshot["top_consumers"] == [
        {
            "component": "live_arena",
            "reason": "startup_sync",
            "reads": 1,
            "failures": 0,
            "rate_limited": 0,
        }
    ]
    assert snapshot["top_operations"] == [{"operation": "values_all", "reads": 1}]


def test_retry_attempts_each_count_as_physical_reads() -> None:
    async def scenario() -> dict[str, object]:
        broker = _broker()
        collector = PhysicalReadDiagnostics(window_sec=300)
        install(target_broker=broker, collector=collector)

        calls = 0

        async def loader() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("429 RESOURCE_EXHAUSTED")
            return "ok"

        try:
            assert await broker.read(
                SheetReadKey.values("sheet-123", "STATE"),
                loader,
                policy=FRESH_REQUIRED,
                component="reset_reminders",
                reason="scheduler_tick",
            ) == "ok"
            return collector.snapshot()
        finally:
            await broker.close()

    snapshot = asyncio.run(scenario())

    assert snapshot["physical_reads"] == 2
    assert snapshot["failures"] == 1
    assert snapshot["rate_limited"] == 1
    assert snapshot["top_consumers"] == [
        {
            "component": "reset_reminders",
            "reason": "scheduler_tick",
            "reads": 2,
            "failures": 1,
            "rate_limited": 1,
        }
    ]


def test_rolling_window_expires_old_events() -> None:
    now = 1000.0

    def clock() -> float:
        return now

    collector = PhysicalReadDiagnostics(window_sec=300, clock_fn=clock)
    collector.record(
        component="old",
        reason="startup",
        operation="values_all",
        ok=True,
        rate_limited=False,
        timestamp=699.0,
    )
    collector.record(
        component="current",
        reason="runtime",
        operation="records_all",
        ok=True,
        rate_limited=False,
        timestamp=999.0,
    )

    snapshot = collector.snapshot()

    assert snapshot["physical_reads"] == 1
    assert snapshot["top_consumers"] == [
        {
            "component": "current",
            "reason": "runtime",
            "reads": 1,
            "failures": 0,
            "rate_limited": 0,
        }
    ]


def test_install_is_idempotent() -> None:
    async def scenario() -> dict[str, object]:
        broker = _broker()
        collector = PhysicalReadDiagnostics(window_sec=300)
        install(target_broker=broker, collector=collector)
        install(target_broker=broker, collector=collector)

        async def loader() -> str:
            return "ok"

        try:
            await broker.read(
                SheetReadKey.values("sheet-123", "CONFIG"),
                loader,
                policy=FRESH_REQUIRED,
                component="config",
                reason="load",
            )
            return collector.snapshot()
        finally:
            await broker.close()

    snapshot = asyncio.run(scenario())
    assert snapshot["physical_reads"] == 1


def test_health_snapshot_combines_broker_totals_and_five_minute_attribution() -> None:
    async def scenario() -> dict[str, object]:
        broker = _broker()
        collector = PhysicalReadDiagnostics(window_sec=300)
        install(target_broker=broker, collector=collector)

        async def loader() -> str:
            return "ok"

        try:
            await broker.read(
                SheetReadKey.values("sheet-123", "CONFIG"),
                loader,
                policy=FRESH_REQUIRED,
                component="config",
                reason="runtime",
            )
            return health_snapshot(target_broker=broker, collector=collector)
        finally:
            await broker.close()

    snapshot = asyncio.run(scenario())

    assert snapshot["physical_reads_total"] == 1
    assert snapshot["logical_reads_total"] == 1
    assert snapshot["cache_hit_rate_pct"] == 0.0
    rolling = snapshot["rolling_5m"]
    assert isinstance(rolling, dict)
    assert rolling["physical_reads"] == 1
