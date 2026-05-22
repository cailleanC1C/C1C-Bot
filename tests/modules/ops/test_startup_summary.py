from modules.ops.startup_summary import render_startup_summary


def test_startup_summary_contains_all_sections_and_refresh_rows() -> None:
    message = render_startup_summary(
        sections={
            "allow_list": ["✅ Guild allow-list", "• verified", "• allowed=['A']", "• connected=['A']"],
            "watchers": ["✅ Watchers", "• Promo watcher — event=enabled"],
            "scheduler": ["🧭 Scheduler", "• intervals: clans=3h", "• clans=2026-05-22 00:00 UTC"],
            "watchdog": ["🐶 Watchdog started", "• interval=300s"],
            "refresh": ["♻️ Refresh", "• clans ok (0.2s, 24, ttl)", "• total=4.8s"],
        }
    )
    assert message.count("♻️ Refresh") == 1
    for section in ["Guild allow-list", "Watchers", "Scheduler", "Watchdog", "Refresh"]:
        assert section in message
    assert "• clans ok (0.2s, 24, ttl)" in message
    assert "• total=4.8s" in message
    assert "cache refresh details unavailable" not in message
    assert "pending (?)" not in message
    assert "unknown (?)" not in message
