from __future__ import annotations

from shared import health
from shared.sheets import read_broker


def test_components_snapshot_includes_sheets_read_broker(monkeypatch) -> None:
    monkeypatch.setattr(
        read_broker.broker,
        "snapshot",
        lambda: {
            "read_budget_rpm": 32,
            "rolling_physical_reads": 12,
            "logical_reads": 100,
            "physical_reads": 20,
            "cache_hits": 50,
            "stale_hits": 10,
            "coalesced_joins": 15,
            "queued": 2,
            "inflight": 1,
            "rate_limit_errors": 3,
            "retries": 4,
            "invalidations": 7,
        },
    )

    snapshot = health.components_snapshot(include_required=False)
    broker = snapshot["sheets_read_broker"]

    assert broker["ok"] is True
    assert broker["read_budget_rpm"] == 32
    assert broker["rolling_physical_reads"] == 12
    assert broker["physical_reads"] == 20
    assert broker["cache_hit_rate_pct"] == 75.0
    assert broker["queued"] == 2
    assert broker["rate_limit_errors"] == 3
    assert broker["invalidations"] == 7


def test_broker_diagnostics_failure_does_not_break_health(monkeypatch) -> None:
    def _boom():
        raise RuntimeError("diagnostics unavailable")

    monkeypatch.setattr(read_broker.broker, "snapshot", _boom)

    snapshot = health.components_snapshot(include_required=False)

    assert "sheets_read_broker" not in snapshot
