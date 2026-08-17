from __future__ import annotations

from shared import health
from shared.sheets import read_diagnostics


def test_components_snapshot_includes_non_blocking_broker_diagnostics(monkeypatch) -> None:
    monkeypatch.setattr(
        read_diagnostics,
        "health_snapshot",
        lambda: {
            "read_budget_rpm": 32,
            "rolling_physical_reads_1m": 7,
            "physical_reads_total": 20,
            "logical_reads_total": 100,
            "cache_hit_rate_pct": 75.0,
            "cache_entries": 4,
            "queued": 2,
            "inflight": 1,
            "rate_limit_errors_total": 3,
            "retries_total": 4,
            "invalidations_total": 5,
            "rolling_5m": {
                "window_sec": 300,
                "physical_reads": 12,
                "failures": 1,
                "rate_limited": 1,
                "top_consumers": [
                    {
                        "component": "live_arena",
                        "reason": "startup_sync",
                        "reads": 5,
                        "failures": 0,
                        "rate_limited": 0,
                    }
                ],
                "top_operations": [{"operation": "values_all", "reads": 7}],
            },
        },
    )

    snapshot = health.components_snapshot(include_required=False)
    broker = snapshot["sheets_read_broker"]

    assert broker["ok"] is True
    assert broker["read_budget_rpm"] == 32
    assert broker["rolling_physical_reads_1m"] == 7
    assert broker["cache_hit_rate_pct"] == 75.0
    rolling = broker["rolling_5m"]
    assert isinstance(rolling, dict)
    assert rolling["physical_reads"] == 12
    assert rolling["top_consumers"][0]["component"] == "live_arena"


def test_diagnostics_failure_does_not_break_health_or_readiness(monkeypatch) -> None:
    original_components = dict(health._components)
    original_updated = dict(health._updated_at)

    def _boom():
        raise RuntimeError("diagnostics unavailable")

    monkeypatch.setattr(read_diagnostics, "health_snapshot", _boom)
    try:
        health._components.clear()
        health._updated_at.clear()
        health.set_component("runtime", True)
        health.set_component("discord", True)

        snapshot = health.components_snapshot(include_required=True)

        assert "sheets_read_broker" not in snapshot
        assert health.overall_ready() is True
    finally:
        health._components.clear()
        health._components.update(original_components)
        health._updated_at.clear()
        health._updated_at.update(original_updated)


def test_required_component_runtime_value_is_unchanged() -> None:
    assert health.required_components() == frozenset({"runtime", "discord"})
