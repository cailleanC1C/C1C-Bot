import logging

from shared.sheets import onboarding


class _WS:
    def __init__(self, values):
        self.values = values
        self.updated = []

    def row_values(self, idx):
        return self.values[0] if idx == 1 else []

    def get_all_values(self):
        return self.values

    def update(self, rng, payload):
        self.updated.append((rng, payload))


def _run(values, monkeypatch, *, expected_headers=None):
    ws = _WS(values)
    monkeypatch.setattr(onboarding.core, "call_with_backoff", lambda fn, *a, **k: fn(*a, **k))
    monkeypatch.setattr(onboarding, "get_welcome_headers", lambda: list(expected_headers or values[0]))
    summary = onboarding.repair_welcome_rows(ws)
    return ws, summary


def test_repair_uses_duplicate_identity_before_needs_review(monkeypatch):
    header = onboarding.WELCOME_HEADERS
    values = [
        header,
        ["W0880", "user", "", "", "", "111111111111111111", "222222222222222222", "", "open", "", "2026-04-02T00:00:00+00:00", ""],
        ["W0880", "user", "", "", "", "", "", "", "open", "", "2026-04-02T00:00:00+00:00", ""],
    ]
    ws, _ = _run(values, monkeypatch)
    assert ws.updated
    updated_row = ws.updated[-1][1][0]
    assert updated_row[5] == "111111111111111111"
    assert updated_row[6] == "222222222222222222"
    assert updated_row[8] != "needs_review"


def test_conflicting_identity_sets_needs_review_reason(monkeypatch):
    header = onboarding.WELCOME_HEADERS
    values = [
        header,
        ["W0881", "user", "", "", "", "111111111111111111", "", "", "open", "", "2026-04-02T00:00:00+00:00", ""],
        ["W0881", "user", "", "", "", "222222222222222222", "", "", "open", "", "2026-04-02T00:00:00+00:00", ""],
        ["W0881", "user", "", "", "", "", "", "", "open", "", "2026-04-02T00:00:00+00:00", ""],
    ]
    ws, _ = _run(values, monkeypatch)
    updated_row = ws.updated[-1][1][0]
    assert updated_row[8] == "needs_review"
    assert updated_row[9] == "conflicting user IDs"


def test_legacy_rows_not_flagged(monkeypatch):
    header = onboarding.WELCOME_HEADERS
    values = [
        header,
        ["W0100", "user", "", "", "", "", "", "", "open", "", "2026-03-01T00:00:00+00:00", ""],
    ]
    ws, summary = _run(values, monkeypatch)
    assert summary["flagged"] == 0
    assert ws.updated == []


def test_repair_alert_no_repair_needed_is_short_without_noisy_counters():
    message = onboarding._format_welcome_repair_alert(
        {
            "repaired": 0,
            "flagged": 0,
            "welcome_rows": 41,
            "reservation_rows": 0,
            "legacy_rows": 0,
            "malformed_rows": 1098,
        }
    )

    assert message == "✅ Welcome ticket metadata check: no repair needed. 41 welcome tickets checked."
    assert "malformed" not in message
    assert "open_spots" not in message


def test_repair_alert_repaired_without_flags_reports_repair_done():
    message = onboarding._format_welcome_repair_alert(
        {
            "repaired": 2,
            "flagged": 0,
            "welcome_rows": 41,
            "reservation_rows": 0,
            "legacy_rows": 0,
            "malformed_rows": 1098,
        }
    )

    assert message == "✅ Welcome ticket metadata check: 2 repaired, none need review."
    assert "no repair needed" not in message
    assert "malformed" not in message
    assert "open_spots" not in message


def test_repair_alert_review_needed_with_persisted_details_points_to_sheet():
    onboarding._WELCOME_REPAIR_ALERT_LAST_TS = 0
    onboarding._WELCOME_REPAIR_ALERT_PENDING = None
    onboarding._queue_welcome_repair_alert(
        {
            "repaired": 3,
            "flagged": 9,
            "welcome_rows": 41,
            "review_detail_rows": 9,
            "app_logged_review_details": 9,
            "malformed_rows": 1098,
        }
    )

    message = onboarding.consume_welcome_repair_alert()
    assert message is not None
    assert "9 tickets need review, 3 repaired" in message
    assert "review_reason" in message
    assert "flagged for review" not in message
    assert "malformed" not in message
    assert "open_spots" not in message


def test_repair_alert_review_needed_without_sheet_details_points_to_app_logs():
    message = onboarding._format_welcome_repair_alert(
        {
            "repaired": 0,
            "flagged": 2,
            "welcome_rows": 4,
            "review_detail_rows": 0,
            "app_logged_review_details": 2,
        }
    )

    assert message == (
        "⚠️ Welcome ticket metadata check: "
        "2 ticket records could not be auto-repaired. Details in app logs."
    )
    assert "flagged for review" not in message


def test_repair_alert_without_review_visibility_suppresses_count_and_logs_defect(caplog):
    caplog.set_level(logging.ERROR, logger=onboarding.__name__)

    message = onboarding._format_welcome_repair_alert(
        {
            "repaired": 0,
            "flagged": 2,
            "welcome_rows": 4,
            "review_detail_rows": 0,
            "app_logged_review_details": 0,
        }
    )

    assert message is None
    assert "flagged for review" not in caplog.text
    assert "without review visibility" in caplog.text


def test_existing_needs_review_row_gets_review_reason_and_structured_log(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=onboarding.__name__)
    header = onboarding.WELCOME_HEADERS
    values = [
        header,
        ["W0882", "user", "", "", "", "", "", "", "needs_review", "", "2026-04-02T00:00:00+00:00", ""],
    ]

    ws, summary = _run(values, monkeypatch)

    assert summary["flagged"] == 1
    assert summary["review_detail_rows"] == 1
    updated_row = ws.updated[-1][1][0]
    assert updated_row[8] == "needs_review"
    assert updated_row[9] == "no matching ticket source found"
    records = [r for r in caplog.records if r.message == "welcome ticket metadata check needs attention"]
    assert records
    assert records[-1].ticket == "W0882"
    assert records[-1].row_number == 2
    assert records[-1].review_reason == "no matching ticket source found"


def test_missing_review_header_uses_app_log_message_without_review_claim(monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger=onboarding.__name__)
    header = [h for h in onboarding.WELCOME_HEADERS if h != "review_reason"]
    values = [
        header,
        ["W0883", "user", "", "", "", "", "", "", "open", "2026-04-02T00:00:00+00:00", ""],
    ]
    ws, summary = _run(values, monkeypatch, expected_headers=header)

    assert summary["flagged"] == 1
    assert summary["review_detail_rows"] == 0
    assert summary["app_logged_review_details"] == 1
    message = onboarding._format_welcome_repair_alert(summary)
    assert "Details in app logs" in message
    assert "flagged for review" not in message
    assert ws.updated
    assert "welcome ticket metadata check needs attention" in caplog.text


def test_malformed_noise_stays_out_of_discord_summary():
    message = onboarding._format_welcome_repair_alert(
        {"repaired": 0, "flagged": 0, "welcome_rows": 1, "malformed_rows": 1098}
    )

    assert message is not None
    assert "malformed" not in message
    assert "1098" not in message


def test_missing_required_header_logs_and_skips_safely(monkeypatch, caplog):
    caplog.set_level(logging.ERROR, logger=onboarding.__name__)
    header = ["ticket_number", "status", "created_at"]
    values = [header, ["W0884", "open", "2026-04-02T00:00:00+00:00"]]

    ws, summary = _run(values, monkeypatch, expected_headers=header)

    assert summary["config_error"] == "missing_required_header"
    assert summary["flagged"] == 0
    assert ws.updated == []
    assert "missing required header" in caplog.text
    assert onboarding._format_welcome_repair_alert(summary) == (
        "⚠️ Welcome ticket metadata check skipped: required sheet configuration is missing."
    )
