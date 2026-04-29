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
