"""Ensure onboarding sheet ID must be configured explicitly."""

from __future__ import annotations

import logging

import pytest

from shared.sheets import onboarding


def test_sheet_id_requires_explicit_config(monkeypatch: "pytest.MonkeyPatch") -> None:
    """_sheet_id should raise when ONBOARDING_SHEET_ID is missing from Config."""

    monkeypatch.setattr(onboarding, "get_onboarding_sheet_id", lambda: "")

    with pytest.raises(RuntimeError) as excinfo:
        onboarding._sheet_id()

    assert "ONBOARDING_SHEET_ID" in str(excinfo.value)


def test_sheet_id_returns_value_when_present(monkeypatch: "pytest.MonkeyPatch") -> None:
    monkeypatch.setattr(onboarding, "get_onboarding_sheet_id", lambda: "abc123")
    monkeypatch.setattr(onboarding, "_LAST_INFO_LOGGED_SHEET_ID", None)

    assert onboarding._sheet_id() == "abc123"


def test_sheet_id_does_not_emit_repeated_info_logs(
    monkeypatch: "pytest.MonkeyPatch", caplog: "pytest.LogCaptureFixture"
) -> None:
    monkeypatch.setattr(onboarding, "get_onboarding_sheet_id", lambda: "sheet-abc123")
    monkeypatch.setattr(onboarding, "_LAST_INFO_LOGGED_SHEET_ID", None)

    with caplog.at_level(logging.DEBUG, logger=onboarding.log.name):
        assert onboarding._sheet_id() == "sheet-abc123"
        assert onboarding._sheet_id() == "sheet-abc123"
        assert onboarding._sheet_id() == "sheet-abc123"

    resolved = [
        record
        for record in caplog.records
        if record.getMessage().startswith("📄 Onboarding sheet resolved")
    ]
    assert [record.levelno for record in resolved] == [
        logging.INFO,
        logging.DEBUG,
        logging.DEBUG,
    ]
