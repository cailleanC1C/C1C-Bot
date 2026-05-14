import datetime as dt

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


def _run(values, monkeypatch):
    ws = _WS(values)
    monkeypatch.setattr(onboarding.core, "call_with_backoff", lambda fn, *a, **k: fn(*a, **k))
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


def test_repair_alert_mentions_open_spots_not_performed():
    onboarding._WELCOME_REPAIR_ALERT_LAST_TS = 0
    onboarding._WELCOME_REPAIR_ALERT_PENDING = None
    onboarding._queue_welcome_repair_alert(
        {
            "repaired": 0,
            "flagged": 1,
            "welcome_rows": 1,
            "reservation_rows": 0,
            "legacy_rows": 0,
            "malformed_rows": 0,
        }
    )
    message = onboarding.consume_welcome_repair_alert()
    assert message is not None
    assert "metadata repair" in message
    assert "open_spots_repair=not_performed" in message
