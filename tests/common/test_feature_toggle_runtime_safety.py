from __future__ import annotations

import asyncio

from modules.common import feature_flags
from modules.common import runtime as runtime_module


def _reset_feature_state(monkeypatch) -> None:
    monkeypatch.setattr(feature_flags, "_FEATURE_VALUES", {})
    monkeypatch.setattr(feature_flags, "_INVALID_FEATURE_VALUES", {})
    monkeypatch.setattr(feature_flags, "_GLOBAL_FAILURE_REASON", "uninitialized")
    monkeypatch.setattr(feature_flags, "_GLOBAL_WARNINGS_SENT", set())


def test_refresh_missing_sheet_id_is_fail_closed(monkeypatch) -> None:
    _reset_feature_state(monkeypatch)
    monkeypatch.setattr(feature_flags, "get_recruitment_sheet_id", lambda: "")

    asyncio.run(feature_flags.refresh())

    assert feature_flags.global_failure_reason() == (
        "Recruitment sheet ID missing; all feature toggles disabled."
    )
    assert feature_flags.is_enabled("welcome_watcher_enabled") is False
    assert all(value is False for value in feature_flags.values().values())


def test_unexpected_refresh_error_clears_previous_true_values(monkeypatch) -> None:
    _reset_feature_state(monkeypatch)
    monkeypatch.setattr(
        feature_flags, "_FEATURE_VALUES", {"welcome_watcher_enabled": True}
    )
    monkeypatch.setattr(feature_flags, "get_recruitment_sheet_id", lambda: "sheet")

    async def fail_refresh():
        raise RuntimeError("unexpected reader failure")

    monkeypatch.setattr(feature_flags, "_refresh", fail_refresh)

    asyncio.run(feature_flags.refresh())

    assert "unexpected reader failure" in (feature_flags.global_failure_reason() or "")
    assert feature_flags.is_enabled("welcome_watcher_enabled") is False
    assert feature_flags.values()["welcome_watcher_enabled"] is False


class _Bot:
    pass


def test_runtime_schedules_aggregated_discord_alert_on_toggle_failure(
    monkeypatch,
) -> None:
    _reset_feature_state(monkeypatch)
    runtime = runtime_module.Runtime(_Bot())  # type: ignore[arg-type]
    messages: list[str] = []

    async def fake_refresh() -> None:
        monkeypatch.setattr(
            feature_flags, "_GLOBAL_FAILURE_REASON", "Config tab read failed"
        )

    async def capture(message: str) -> None:
        messages.append(message)

    monkeypatch.setattr(feature_flags, "refresh", fake_refresh)
    monkeypatch.setattr(runtime, "send_log_message", capture)

    async def run() -> None:
        await runtime._refresh_feature_toggles()
        await asyncio.sleep(0)

    asyncio.run(run())

    assert len(messages) == 1
    assert "feature toggles could not be read" in messages[0]
    assert "disabled/skipped for safety" in messages[0]
    assert "Config tab read failed" in messages[0]
    assert "member_panel" in messages[0]


def test_runtime_does_not_alert_after_successful_refresh(monkeypatch) -> None:
    _reset_feature_state(monkeypatch)
    runtime = runtime_module.Runtime(_Bot())  # type: ignore[arg-type]
    messages: list[str] = []

    async def fake_refresh() -> None:
        monkeypatch.setattr(feature_flags, "_GLOBAL_FAILURE_REASON", None)
        monkeypatch.setattr(feature_flags, "_FEATURE_VALUES", {"member_panel": True})

    async def capture(message: str) -> None:
        messages.append(message)

    monkeypatch.setattr(feature_flags, "refresh", fake_refresh)
    monkeypatch.setattr(runtime, "send_log_message", capture)

    asyncio.run(runtime._refresh_feature_toggles())

    assert messages == []
