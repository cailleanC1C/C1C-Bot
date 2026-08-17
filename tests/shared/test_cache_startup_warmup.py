from __future__ import annotations

from shared.cache import telemetry


def test_startup_order_prioritizes_operational_buckets():
    names = [
        "achievement_history",
        "templates",
        "live_arena_state",
        "clans",
        "guides_help_index",
        "reset_reminders",
    ]

    ordered = telemetry.startup_order(names)

    assert ordered[0] == "clans"
    assert ordered.index("live_arena_state") < ordered.index("templates")
    assert ordered.index("reset_reminders") < ordered.index("guides_help_index")
    assert ordered[-1] == "achievement_history"


def test_startup_offsets_span_priority_bands_to_five_minute_boundary():
    names = [
        "clans",
        "live_arena_state",
        "templates",
        "achievement_history",
    ]

    offsets = telemetry.startup_warmup_offsets(names)

    assert offsets["clans"] == 0
    assert 30 <= offsets["live_arena_state"] <= 90
    assert 90 <= offsets["templates"] <= 180
    assert offsets["achievement_history"] == telemetry._STARTUP_WARMUP_WINDOW_SECONDS


def test_unknown_bucket_is_support_not_critical():
    offsets = telemetry.startup_warmup_offsets(["clans", "new_future_bucket"])

    assert offsets["clans"] == 0
    assert 90 <= offsets["new_future_bucket"] <= 180
