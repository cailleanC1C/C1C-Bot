import asyncio
import datetime as dt
from dataclasses import replace
from unittest.mock import AsyncMock

from modules.community.fusion import reminders
from shared.sheets import fusion as fusion_sheets


def _fusion_row(*, opt_in_role_id: int | None = None) -> fusion_sheets.FusionRow:
    return fusion_sheets.FusionRow(
        fusion_id="f-1",
        fusion_name="Mavara",
        champion="Mavara",
        champion_image_url="",
        fusion_type="traditional",
        fusion_structure="",
        reward_type="fragments",
        needed=400,
        available=450,
        start_at_utc=dt.datetime(2026, 4, 8, tzinfo=dt.timezone.utc),
        end_at_utc=dt.datetime(2026, 4, 22, tzinfo=dt.timezone.utc),
        announcement_channel_id=123,
        opt_in_role_id=opt_in_role_id,
        announcement_message_id=456,
        published_at=dt.datetime(2026, 4, 7, tzinfo=dt.timezone.utc),
        last_announcement_refresh_at=None,
        last_announcement_status_hash="",
        status="active",
    )


def _event(*, event_id: str, start_at: dt.datetime) -> fusion_sheets.FusionEventRow:
    return fusion_sheets.FusionEventRow(
        fusion_id="f-1",
        event_id=event_id,
        event_name=f"Event {event_id}",
        event_type="tournament",
        category="arena",
        start_at_utc=start_at,
        end_at_utc=start_at + dt.timedelta(hours=2),
        reward_amount=100.0,
        bonus=None,
        reward_type="fragments",
        points_needed=1000,
        is_estimated=False,
        sort_order=1,
    )


class _DummyChannel:
    def __init__(self) -> None:
        self.id = 123
        self.sent = []

    async def send(self, *, content=None, embed=None, view=None):
        self.sent.append({"content": content, "embed": embed, "view": view})


class _DummyAnnouncementMessage:
    def __init__(self, jump_url: str, channel: _DummyChannel) -> None:
        self.jump_url = jump_url
        self.channel = channel


def _settings(**overrides):
    return fusion_sheets.FusionReminderSettings(
        start_offset_minutes=overrides.get("start_offset_minutes", 360),
        end_lookahead_hours=overrides.get("end_lookahead_hours", 24),
        upcoming_window_days=overrides.get("upcoming_window_days", 2),
        group_events=overrides.get("group_events", True),
        grouped_post_time_utc=overrides.get("grouped_post_time_utc", "12:00"),
        include_start_events=overrides.get("include_start_events", True),
        include_ending_events=overrides.get("include_ending_events", False),
        include_upcoming_events=overrides.get("include_upcoming_events", False),
        grouped_embed_title=overrides.get("grouped_embed_title", "{fusion_title} Summary"),
        grouped_embed_description=overrides.get("grouped_embed_description", "Jump: {jump_link} | live={live_count} upcoming={upcoming_count}"),
        grouped_live_label=overrides.get("grouped_live_label", "Live"),
        grouped_upcoming_label=overrides.get("grouped_upcoming_label", "Upcoming"),
        grouped_ending_label=overrides.get("grouped_ending_label", "Ending"),
        grouped_empty_value=overrides.get("grouped_empty_value", "None"),
        grouped_jump_label=overrides.get("grouped_jump_label", "Open"),
        settings_raw_values=overrides.get(
            "settings_raw_values",
            {
                "group_events": "TRUE" if overrides.get("group_events", True) else "FALSE",
                "grouped_daily_post_time": overrides.get("grouped_daily_post_time", "13:00"),
            },
        ),
        settings_raw_types=overrides.get(
            "settings_raw_types",
            {"group_events": "str", "grouped_daily_post_time": "str"},
        ),
        settings_raw_key_names=overrides.get(
            "settings_raw_key_names",
            {"group_events": "group_events", "grouped_daily_post_time": "grouped_daily_post_time"},
        ),
    )


def test_grouped_daily_reminder_posts_once_and_ignores_old_non_grouped_rows(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    event = _event(event_id="e-start", start_at=now)
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    persisted: set[tuple[str, str, str]] = {("f-1", "e-start", "start"), ("f-1", "e-pre", "prestart_6h")}

    async def _get_sent_keys(fusion_id: str):
        return {(event_id, reminder_type) for f_id, event_id, reminder_type in persisted if f_id == fusion_id}

    async def _mark_sent(fusion_id: str, *, event_id: str, reminder_type: str, sent_at: dt.datetime):
        persisted.add((fusion_id, event_id, reminder_type))

    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row(opt_in_role_id=777)))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[event]))
    monkeypatch.setattr(fusion_sheets, "get_valid_event_timing", lambda event, **_kwargs: (event.start_at_utc, event.end_at_utc))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", _get_sent_keys)
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", _mark_sent)
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings()))
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: "view")
    reminders._MEMORY_SENT_KEYS.clear()

    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now + dt.timedelta(minutes=1)))

    assert len(channel.sent) == 1
    assert channel.sent[0]["content"] == "<@&777>"
    assert channel.sent[0]["view"] == "view"
    embed = channel.sent[0]["embed"]
    assert embed.title == "Mavara Summary"
    assert "[Open](https://discord.test/jump)" in embed.description
    assert any(field.name == "Live" and "Event e-start" in field.value for field in embed.fields)
    assert ("f-1", "grouped_daily:2026-04-10", "grouped_daily") in persisted


def test_grouped_daily_reminder_waits_until_configured_post_time(monkeypatch):
    now = dt.datetime(2026, 4, 10, 11, 59, tzinfo=dt.timezone.utc)
    event = _event(event_id="e-live", start_at=now - dt.timedelta(hours=1))
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[event]))
    monkeypatch.setattr(fusion_sheets, "get_valid_event_timing", lambda event, **_kwargs: (event.start_at_utc, event.end_at_utc))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings(grouped_post_time_utc="12:00")))
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    reminders._MEMORY_SENT_KEYS.clear()

    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))

    assert channel.sent == []
    fusion_sheets.mark_reminder_sent.assert_not_awaited()


def test_grouped_daily_reminder_posts_again_next_day(monkeypatch):
    first = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    second = first + dt.timedelta(days=1)
    event = _event(event_id="e-live", start_at=first)
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    persisted: set[tuple[str, str]] = set()

    async def _get_sent_keys(_fusion_id: str):
        return set(persisted)

    async def _mark_sent(_fusion_id: str, *, event_id: str, reminder_type: str, sent_at: dt.datetime):
        persisted.add((event_id, reminder_type))

    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[event]))
    monkeypatch.setattr(fusion_sheets, "get_valid_event_timing", lambda _event, **_kwargs: (first, second + dt.timedelta(hours=2)))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", _get_sent_keys)
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", _mark_sent)
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings()))
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: None)
    reminders._MEMORY_SENT_KEYS.clear()

    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=first))
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=second))

    assert len(channel.sent) == 2
    assert ("grouped_daily:2026-04-10", "grouped_daily") in persisted
    assert ("grouped_daily:2026-04-11", "grouped_daily") in persisted


def test_grouped_missing_copy_logs_and_skips(monkeypatch, caplog):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    event = _event(event_id="e-empty", start_at=now)
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[event]))
    monkeypatch.setattr(fusion_sheets, "get_valid_event_timing", lambda event, **_kwargs: (event.start_at_utc, event.end_at_utc))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings(grouped_embed_title="")))
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: None)
    reminders._MEMORY_SENT_KEYS.clear()

    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))

    assert channel.sent == []
    assert "missing required sheet copy fields" in caplog.text


def test_startup_summary_reports_grouped_daily_status(monkeypatch):
    now = dt.datetime(2026, 4, 10, 11, 30, tzinfo=dt.timezone.utc)
    due_event = _event(event_id="due", start_at=now)

    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[due_event]))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
    monkeypatch.setattr(fusion_sheets, "get_last_reminder_sent_at", AsyncMock(return_value=dt.datetime(2026, 4, 9, 12, tzinfo=dt.timezone.utc)))
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings(grouped_post_time_utc="12:00")))
    monkeypatch.setattr(
        fusion_sheets,
        "get_valid_event_timing",
        lambda event, **_kwargs: (event.start_at_utc, event.end_at_utc),
    )

    lines = asyncio.run(
        reminders.collect_fusion_reminder_startup_summary(object(), scheduler_started=True, now=now)
    )

    assert "• scheduler_started=yes" in lines
    assert "• enabled=yes" in lines
    assert "• configured_local_post_time=13:00 Europe/Vienna" in lines
    assert "• parsed_utc_post_time=12:00" in lines
    assert "• resolved channel=no thread=no role=n/a" in lines
    assert any(line.startswith("• next_due=2026-04-10 12:00 UTC") for line in lines)
    assert not any(line.startswith("• settings_") for line in lines)
    assert not any(line.startswith("• raw_grouped_reminder_settings=") for line in lines)
    assert not any(line.startswith("• skipped=") for line in lines)


def test_non_grouped_config_does_not_send_per_event_reminders(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    event = _event(event_id="e-start", start_at=now)
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[event]))
    monkeypatch.setattr(fusion_sheets, "get_valid_event_timing", lambda event, **_kwargs: (event.start_at_utc, event.end_at_utc))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings(group_events=False)))
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    reminders._MEMORY_SENT_KEYS.clear()

    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))

    assert channel.sent == []
    fusion_sheets.mark_reminder_sent.assert_not_awaited()

def test_disabled_grouped_config_logs_resolved_source(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    source = fusion_sheets.FusionReminderSettingSource(
        tab_name="FusionReminderSettings",
        key_header="setting_key",
        value_header="setting_value",
        raw_value="FALSE",
    )
    settings = replace(_settings(group_events=False), group_events_source=source)
    alerts = []

    async def _send_ops_alert(**kwargs):
        alerts.append(kwargs)

    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=settings))
    monkeypatch.setattr(reminders.fusion_logs, "send_ops_alert", _send_ops_alert)

    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))

    assert alerts == [
        {
            "component": "reminders",
            "summary": "grouped_reminders_disabled",
            "dedupe_key": "fusion:grouped_reminders:disabled:f-1",
            "reason": "group_events_resolved_false",
            "fields": {
                "fusion_id": "f-1",
                "group_events_resolved": "false",
                "group_events_raw_value": "FALSE",
                "group_events_source_tab": "FusionReminderSettings",
                "group_events_key_header": "setting_key",
                "group_events_value_header": "setting_value",
            },
        }
    ]


def test_grouped_reminder_skips_when_dedupe_read_times_out_after_retry(monkeypatch, caplog):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    event = _event(event_id="e-start", start_at=now)
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    alerts = []
    attempts = 0

    async def _get_sent_keys(_fusion_id: str):
        nonlocal attempts
        attempts += 1
        raise TimeoutError("dedupe unavailable")

    async def _send_ops_alert(**kwargs):
        alerts.append(kwargs)

    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row(opt_in_role_id=777)))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[event]))
    monkeypatch.setattr(fusion_sheets, "get_valid_event_timing", lambda event, **_kwargs: (event.start_at_utc, event.end_at_utc))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", _get_sent_keys)
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings()))
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: "view")
    monkeypatch.setattr(reminders.fusion_logs, "send_ops_alert", _send_ops_alert)
    monkeypatch.setattr(reminders, "_DEDUP_READ_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(reminders, "_DEDUP_BACKOFF_UNTIL_MONOTONIC", 0.0)
    reminders._MEMORY_SENT_KEYS.clear()
    reminders._SENT_KEYS_CACHE.clear()

    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))

    assert attempts == 2
    assert channel.sent == []
    fusion_sheets.mark_reminder_sent.assert_not_awaited()
    assert "reminders skipped because sent-reminder dedupe could not be verified" in caplog.text
    assert alerts[-1]["summary"] == "grouped_dedupe_unavailable_reminders_skipped"


def test_grouped_reminder_retries_dedupe_then_sends_when_retry_succeeds(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    event = _event(event_id="e-start", start_at=now)
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    attempts = 0

    async def _get_sent_keys(_fusion_id: str):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("first attempt")
        return set()

    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[event]))
    monkeypatch.setattr(fusion_sheets, "get_valid_event_timing", lambda event, **_kwargs: (event.start_at_utc, event.end_at_utc))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", _get_sent_keys)
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings()))
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: "view")
    monkeypatch.setattr(reminders, "_DEDUP_READ_RETRY_DELAY_SEC", 0)
    monkeypatch.setattr(reminders, "_DEDUP_BACKOFF_UNTIL_MONOTONIC", 0.0)
    reminders._MEMORY_SENT_KEYS.clear()
    reminders._SENT_KEYS_CACHE.clear()

    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))

    assert attempts == 2
    assert len(channel.sent) == 1
    fusion_sheets.mark_reminder_sent.assert_awaited_once()


def test_grouped_reminder_reuses_brief_dedupe_cache(monkeypatch):
    now = dt.datetime(2026, 4, 10, 11, 59, tzinfo=dt.timezone.utc)
    calls = 0

    async def _get_sent_keys(_fusion_id: str):
        nonlocal calls
        calls += 1
        return set()

    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", _get_sent_keys)
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings(grouped_post_time_utc="12:00")))
    monkeypatch.setattr(reminders, "_DEDUP_BACKOFF_UNTIL_MONOTONIC", 0.0)
    reminders._SENT_KEYS_CACHE.clear()
    reminders._MEMORY_SENT_KEYS.clear()

    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now + dt.timedelta(seconds=10)))

    assert calls == 1
