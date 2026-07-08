import importlib
import logging
import sys
import types


def test_merge_onboarding_config_early_merges(monkeypatch, caplog):
    import shared.config as config

    monkeypatch.setenv("DISCORD_TOKEN", "token")
    monkeypatch.setenv("GSPREAD_CREDENTIALS", "{}")
    monkeypatch.setenv("RECRUITMENT_SHEET_ID", "recruit-sheet")
    monkeypatch.setenv("ONBOARDING_SHEET_ID", "onboard-sheet-XYZ")

    fake_config = {
        "ONBOARDING_TAB": "WelcomeQuestions",
        "WELCOME_TICKETS_TAB": "WelcomeTickets",
        "PROMO_TICKETS_TAB": "PromoTickets",
    }

    def _read_onboarding_config(sheet_id):
        assert sheet_id == "onboard-sheet-XYZ"
        return fake_config

    monkeypatch.setitem(
        sys.modules,
        "shared.sheets.onboarding",
        types.SimpleNamespace(_read_onboarding_config=_read_onboarding_config),
    )

    original_getenv = config.os.getenv

    def _guarded_getenv(key, default=None):
        assert key not in {"GOOGLE_SHEET_ID", "GSHEET_ID"}
        return original_getenv(key, default)

    monkeypatch.setattr(config.os, "getenv", _guarded_getenv)

    importlib.reload(config)

    caplog.set_level(logging.INFO)
    merged = config.merge_onboarding_config_early()

    assert merged == len(fake_config)
    assert config.onboarding_config_merge_count() == len(fake_config)
    assert config.resolve_onboarding_tab(config.cfg) == "WelcomeQuestions"

    messages = [record.getMessage() for record in caplog.records]
    assert any("🧩 Config — merged onboarding tab" in message for message in messages)


def test_amerge_onboarding_config_early_uses_async_loader(monkeypatch):
    import asyncio
    import shared.config as config

    monkeypatch.setenv("ONBOARDING_SHEET_ID", "onboard-sheet-XYZ")
    fake_config = {"onboarding_tab": "WelcomeQuestions"}
    calls = {"async": 0, "milestones": 0}

    async def _aload_config(*, force=False):
        calls["async"] += 1
        assert force is True
        return fake_config

    async def _aload_milestones_config_values():
        calls["milestones"] += 1
        return "milestone-sheet", {"SHARD_MERCY_TAB": "Mercy"}

    def _sync_loader():
        raise AssertionError("sync onboarding Config loader must not run in async runtime")

    monkeypatch.setitem(
        sys.modules,
        "shared.sheets.onboarding",
        types.SimpleNamespace(_aload_config=_aload_config, _read_onboarding_config=_sync_loader),
    )
    monkeypatch.setattr(config, "_aload_milestones_config_values", _aload_milestones_config_values)

    merged = asyncio.run(config.amerge_onboarding_config_early())

    assert merged == 1
    assert calls == {"async": 1, "milestones": 1}
    assert config.cfg.get("ONBOARDING_TAB") == "WelcomeQuestions"
    assert config.cfg.get("SHARD_MERCY_TAB") == "Mercy"


def test_amerge_onboarding_config_early_executes_real_async_sheet_loaders(monkeypatch):
    import asyncio

    import shared.config as config
    from shared.sheets import core as sheets_core
    from shared.sheets import milestones_config
    from shared.sheets import onboarding as onboarding_sheets

    monkeypatch.setenv("ONBOARDING_SHEET_ID", "onboard-sheet-XYZ")
    monkeypatch.setenv("ONBOARDING_CONFIG_TAB", "OnboardingConfigTab")
    monkeypatch.setenv("MILESTONES_SHEET_ID", "milestone-sheet-XYZ")
    monkeypatch.setenv("MILESTONES_CONFIG_TAB", "MilestonesConfigTab")
    config._CONFIG["MILESTONES_SHEET_ID"] = "milestone-sheet-XYZ"

    onboarding_sheets._CONFIG_CACHE = None
    onboarding_sheets._CONFIG_CACHE_TS = 0.0

    calls = []

    async def _fake_onboarding_afetch_records(sheet_id, tab_name, *args, **kwargs):
        calls.append(("onboarding", sheet_id, tab_name))
        assert sheet_id == "onboard-sheet-XYZ"
        assert tab_name == "OnboardingConfigTab"
        return [
            {"Key": "ONBOARDING_TAB", "Value": "WelcomeQuestions"},
            {"Key": "WELCOME_TICKETS_TAB", "Value": "WelcomeTickets"},
        ]

    async def _fake_milestones_afetch_records(sheet_id, tab_name, *args, **kwargs):
        calls.append(("milestones", sheet_id, tab_name))
        assert sheet_id == "milestone-sheet-XYZ"
        assert tab_name == "MilestonesConfigTab"
        return [
            {"Key": "SHARD_MERCY_TAB", "Value": "Mercy"},
            {"Key": "RESET_REMINDER_TAB", "Value": "ResetReminders"},
        ]

    def _sync_fetch_records(*args, **kwargs):
        raise AssertionError("sync Sheets fetch_records must not run during async startup preload")

    monkeypatch.setattr(onboarding_sheets, "afetch_records", _fake_onboarding_afetch_records)
    monkeypatch.setattr(milestones_config.async_core, "afetch_records", _fake_milestones_afetch_records)
    monkeypatch.setattr(sheets_core, "fetch_records", _sync_fetch_records)

    merged = asyncio.run(config.amerge_onboarding_config_early())

    assert merged == 2
    assert config.cfg.get("ONBOARDING_TAB") == "WelcomeQuestions"
    assert config.cfg.get("WELCOME_TICKETS_TAB") == "WelcomeTickets"
    assert config.cfg.get("SHARD_MERCY_TAB") == "Mercy"
    assert config.cfg.get("RESET_REMINDER_TAB") == "ResetReminders"
    assert calls == [
        ("onboarding", "onboard-sheet-XYZ", "OnboardingConfigTab"),
        ("milestones", "milestone-sheet-XYZ", "MilestonesConfigTab"),
    ]
