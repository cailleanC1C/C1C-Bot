from __future__ import annotations

import asyncio
import importlib

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
    async def load_extension(self, _extension: str) -> None:
        return None


def _patch_always_loaded_setups(monkeypatch, *, ops_calls: list[str]) -> None:
    async def noop_setup(_bot) -> None:
        return None

    async def ops_setup(_bot) -> None:
        ops_calls.append("modules.ops.permissions_ui")

    module_names = (
        "c1c_coreops.cog",
        "c1c_coreops.ops",
        "cogs.app_admin",
        "cogs.housekeeping_mirralith",
        "cogs.housekeeping_achievements",
        "cogs.housekeeping_achievement_collector",
        "cogs.housekeeping_wandering_souls",
        "cogs.housekeeping_c1c_ad",
        "cogs.housekeeping_realmwalker",
        "cogs.recruitment_clan_ads",
        "cogs.clanrole_management",
        "cogs.recruitment_open_spots",
        "modules.housekeeping.cleanup",
        "modules.onboarding.ops_check",
        "modules.onboarding.reaction_fallback",
        "modules.onboarding.watcher_welcome",
        "modules.onboarding.watcher_promo",
        "modules.onboarding.cmd_resume",
        "modules.onboarding.cmd_finishplacement",
    )
    for module_name in module_names:
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "setup", noop_setup)
    permissions = importlib.import_module("modules.ops.permissions_ui")
    monkeypatch.setattr(permissions, "setup", ops_setup)
    monkeypatch.setattr(runtime_module.onboarding_pkg, "setup", noop_setup)
    monkeypatch.setattr(runtime_module, "COMMUNITY_EXTENSIONS", ())


def _run_extension_load(
    monkeypatch, *, failure_reason: str | None
) -> tuple[list[str], list[str]]:
    runtime = runtime_module.Runtime(_Bot())  # type: ignore[arg-type]
    messages: list[str] = []
    ops_calls: list[str] = []
    _patch_always_loaded_setups(monkeypatch, ops_calls=ops_calls)

    async def fake_refresh() -> str | None:
        values = None
        if failure_reason:
            values = {
                "mirralith_overview_enabled": False,
                "welcome_watcher_enabled": False,
                "promo_watcher_enabled": False,
                "resume_command_enabled": False,
                "ops_permissions_enabled": False,
            }
        runtime_module.shared_config.update_feature_flags_snapshot(values)
        return failure_reason

    async def capture(message: str) -> None:
        messages.append(message)

    monkeypatch.setattr(runtime, "_refresh_feature_toggles", fake_refresh)
    monkeypatch.setattr(runtime, "send_log_message", capture)
    monkeypatch.setattr(feature_flags, "is_enabled", lambda _key: False)

    async def run() -> None:
        await runtime.load_extensions()
        await asyncio.sleep(0)

    asyncio.run(run())
    return messages, ops_calls


def test_runtime_alert_reports_modules_skipped_by_feature_loader(monkeypatch) -> None:
    messages, _ops_calls = _run_extension_load(
        monkeypatch, failure_reason="Config tab read failed"
    )

    assert len(messages) == 1
    assert "feature toggles could not be read" in messages[0]
    assert "disabled/skipped for safety" in messages[0]
    assert "Config tab read failed" in messages[0]
    assert (
        "modules.recruitment.services.search (keys=member_panel,recruiter_panel)"
        in messages[0]
    )


def test_runtime_alert_reports_fail_closed_finishplacement(monkeypatch) -> None:
    messages, _ops_calls = _run_extension_load(
        monkeypatch, failure_reason="Feature Toggles tab read failed"
    )

    assert "modules.onboarding.cmd_finishplacement" in messages[0]
    assert "keys=welcome_watcher_enabled,promo_watcher_enabled" in messages[0]


def test_runtime_alert_reports_each_explicit_shared_config_skip(monkeypatch) -> None:
    messages, _ops_calls = _run_extension_load(
        monkeypatch, failure_reason="Feature Toggles tab read failed"
    )

    expected_modules = (
        "cogs.housekeeping_mirralith",
        "modules.onboarding.reaction_fallback",
        "modules.onboarding.watcher_welcome",
        "modules.onboarding.watcher_promo",
        "modules.onboarding.cmd_resume",
        "modules.onboarding.cmd_finishplacement",
    )
    assert all(module_path in messages[0] for module_path in expected_modules)
    assert "—" not in messages[0]


def test_runtime_alert_does_not_report_ops_permissions_when_setup_runs(
    monkeypatch,
) -> None:
    messages, ops_calls = _run_extension_load(
        monkeypatch, failure_reason="Feature Toggles tab read failed"
    )

    assert ops_calls == ["modules.ops.permissions_ui"]
    assert "modules.ops.permissions_ui" not in messages[0]


def test_runtime_does_not_alert_after_successful_refresh(monkeypatch) -> None:
    messages, _ops_calls = _run_extension_load(monkeypatch, failure_reason=None)

    assert messages == []
