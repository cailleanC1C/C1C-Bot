import asyncio
import datetime as dt
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
        group_events=overrides.get("group_events", False),
        include_start_events=overrides.get("include_start_events", True),
        include_ending_events=overrides.get("include_ending_events", False),
        include_upcoming_events=overrides.get("include_upcoming_events", False),
    )


def test_start_reminder_fires_once_and_is_restart_safe(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    event = _event(event_id="e-start", start_at=now)
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)

    persisted: set[tuple[str, str, str]] = set()

    async def _get_sent_keys(fusion_id: str):
        return {(event_id, reminder_type) for f_id, event_id, reminder_type in persisted if f_id == fusion_id}

    async def _mark_sent(fusion_id: str, *, event_id: str, reminder_type: str, sent_at: dt.datetime):
        persisted.add((fusion_id, event_id, reminder_type))

    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[event]))
    monkeypatch.setattr(fusion_sheets, "get_valid_event_timing", lambda *_args, **_kwargs: (now, now + dt.timedelta(hours=1)))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", _get_sent_keys)
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", _mark_sent)
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings()))
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: None)

    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))

    assert len(channel.sent) == 1
    assert channel.sent[0]["content"] is None
    assert channel.sent[0]["view"] is None
    embed = channel.sent[0]["embed"]
    assert embed.title == "Fusion Reminder"
    assert (
        embed.description == "⚠️ **Event e-start is live**\n"
        "Time to put in some work — fragments won’t collect themselves.\n\n"
        "🔗 [Open Fusion Overview](https://discord.test/jump)"
    )
    assert not embed.fields
    assert ("f-1", "e-start", "start") in persisted


def test_prestart_reminder_fires_once(monkeypatch):
    now = dt.datetime(2026, 4, 10, 6, 0, tzinfo=dt.timezone.utc)
    event_start = now + dt.timedelta(hours=6)
    event = _event(event_id="e-pre", start_at=event_start)
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    sent: set[tuple[str, str]] = set()

    async def _get_sent_keys(_fusion_id: str):
        return set(sent)

    async def _mark_sent(_fusion_id: str, *, event_id: str, reminder_type: str, sent_at: dt.datetime):
        sent.add((event_id, reminder_type))

    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row(opt_in_role_id=777)))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[event]))
    monkeypatch.setattr(fusion_sheets, "get_valid_event_timing", lambda *_args, **_kwargs: (event_start, event_start + dt.timedelta(hours=1)))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", _get_sent_keys)
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", _mark_sent)
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings()))
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: "view")

    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now + dt.timedelta(minutes=1)))

    assert len(channel.sent) == 1
    assert channel.sent[0]["content"] == "<@&777>"
    assert channel.sent[0]["view"] == "view"
    embed = channel.sent[0]["embed"]
    assert embed.title == "⏳ Event e-pre starts soon"
    assert embed.description == "Starts in 6h. Plan accordingly."
    assert embed.fields[0].name == "Fusion"
    assert embed.fields[0].value == "[Open Fusion Overview](https://discord.test/jump)"
    assert ("e-pre", "prestart_6h") in sent


def test_invalid_events_skipped_and_no_role_mention_when_absent(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    good = _event(event_id="good", start_at=now)
    bad = _event(event_id="bad", start_at=now)
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)

    def _timing(event, **_kwargs):
        if event.event_id == "bad":
            return None
        return (now, now + dt.timedelta(hours=1))

    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row(opt_in_role_id=None)))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[bad, good]))
    monkeypatch.setattr(fusion_sheets, "get_valid_event_timing", _timing)
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings()))
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: None)

    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))

    assert len(channel.sent) == 1
    assert channel.sent[0]["content"] is None
    assert channel.sent[0]["view"] is None


def test_announcement_self_heals_before_sending(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    event = _event(event_id="heal", start_at=now)
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    ensure = AsyncMock(return_value=announcement)

    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[event]))
    monkeypatch.setattr(fusion_sheets, "get_valid_event_timing", lambda *_args, **_kwargs: (now, now + dt.timedelta(hours=1)))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings()))
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", ensure)

    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))

    ensure.assert_awaited_once()
    assert channel.sent


def test_group_events_true_sends_single_grouped_message(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    e1 = _event(event_id="a", start_at=now)
    e2 = _event(event_id="b", start_at=now + dt.timedelta(hours=4))
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[e1, e2]))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
    mark_sent = AsyncMock()
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", mark_sent)
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: None)
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings(group_events=True, include_upcoming_events=True)))
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))
    assert len(channel.sent) == 1
    assert channel.sent[0]["embed"].fields[0].name == "Live now"
    assert mark_sent.await_count == 1


def test_grouped_upcoming_toggle(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    e = _event(event_id="future", start_at=now + dt.timedelta(days=1))
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    common = [
        ("get_publishable_fusion", AsyncMock(return_value=_fusion_row())),
        ("get_fusion_events", AsyncMock(return_value=[e])),
        ("get_sent_reminder_keys", AsyncMock(return_value=set())),
        ("mark_reminder_sent", AsyncMock()),
    ]
    for name, fn in common:
        monkeypatch.setattr(fusion_sheets, name, fn)
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: None)
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings(group_events=True, include_upcoming_events=False)))
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))
    assert len(channel.sent) == 0
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings(group_events=True, include_upcoming_events=True)))
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))
    assert len(channel.sent) == 0


def test_grouped_dedupe_allows_new_window_when_event_set_changes(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    live = _event(event_id="live", start_at=now - dt.timedelta(minutes=1))
    upcoming = _event(event_id="upcoming", start_at=now + dt.timedelta(hours=1))
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    sent: set[tuple[str, str]] = set()

    async def _get_sent_keys(_fusion_id: str):
        return set(sent)

    async def _mark_sent(_fusion_id: str, *, event_id: str, reminder_type: str, sent_at: dt.datetime):
        sent.add((event_id, reminder_type))

    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[live]))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", _get_sent_keys)
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", _mark_sent)
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: None)
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings(group_events=True, include_upcoming_events=True)))
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now + dt.timedelta(hours=1)))
    assert len(channel.sent) == 1
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[live, upcoming]))
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now + dt.timedelta(hours=2)))
    assert len(channel.sent) == 2


def test_grouped_boundary_start_is_live_not_upcoming(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    event = _event(event_id="boundary", start_at=now)
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[event]))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: None)
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings(group_events=True, include_upcoming_events=True)))
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))
    names = [field.name for field in channel.sent[0]["embed"].fields]
    assert "Live now" in names
    assert "Starting soon" not in names


def test_grouped_state_change_upcoming_to_live_resends(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    event = _event(event_id="flip", start_at=now + dt.timedelta(minutes=1))
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    sent: set[tuple[str, str]] = set()

    async def _get_sent_keys(_fusion_id: str):
        return set(sent)

    async def _mark_sent(_fusion_id: str, *, event_id: str, reminder_type: str, sent_at: dt.datetime):
        sent.add((event_id, reminder_type))

    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[event]))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", _get_sent_keys)
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", _mark_sent)
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: None)
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings(group_events=True, include_upcoming_events=True)))
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now + dt.timedelta(minutes=2)))
    assert len(channel.sent) == 2


def test_grouped_hash_stable_for_row_order(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    e1 = _event(event_id="a", start_at=now - dt.timedelta(minutes=1))
    e2 = _event(event_id="b", start_at=now + dt.timedelta(hours=1))
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    sent: set[tuple[str, str]] = set()

    async def _get_sent_keys(_fusion_id: str):
        return set(sent)

    async def _mark_sent(_fusion_id: str, *, event_id: str, reminder_type: str, sent_at: dt.datetime):
        sent.add((event_id, reminder_type))

    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[e1, e2]))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", _get_sent_keys)
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", _mark_sent)
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: None)
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings(group_events=True, include_upcoming_events=True)))
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[e2, e1]))
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now + dt.timedelta(minutes=1)))
    assert len(channel.sent) == 1


def test_grouped_planning_only_does_not_trigger_send(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    planning = _event(event_id="plan", start_at=now + dt.timedelta(hours=10))
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[planning]))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: None)
    monkeypatch.setattr(
        fusion_sheets,
        "get_fusion_reminder_settings",
        AsyncMock(return_value=_settings(group_events=True, include_upcoming_events=True, start_offset_minutes=120, upcoming_window_days=1)),
    )
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))
    assert len(channel.sent) == 0


def test_grouped_start_offset_triggers_when_inside_window(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    soon = _event(event_id="soon", start_at=now + dt.timedelta(minutes=90))
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[soon]))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: None)
    monkeypatch.setattr(
        fusion_sheets,
        "get_fusion_reminder_settings",
        AsyncMock(return_value=_settings(group_events=True, include_upcoming_events=True, start_offset_minutes=120, upcoming_window_days=1)),
    )
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))
    assert len(channel.sent) == 1
    names = [field.name for field in channel.sent[0]["embed"].fields]
    assert "Starting soon" in names
    assert "Upcoming / planning" not in names


def test_grouped_live_event_triggers_send(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    live = _event(event_id="live-only", start_at=now - dt.timedelta(minutes=10))
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[live]))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: None)
    monkeypatch.setattr(fusion_sheets, "get_fusion_reminder_settings", AsyncMock(return_value=_settings(group_events=True, include_start_events=True)))
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))
    assert len(channel.sent) == 1


def test_grouped_ending_soon_triggers_send(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    ending = fusion_sheets.FusionEventRow(
        fusion_id="f-1",
        event_id="ending",
        event_name="Event ending",
        event_type="tournament",
        category="arena",
        start_at_utc=now - dt.timedelta(hours=1),
        end_at_utc=now + dt.timedelta(minutes=30),
        reward_amount=100.0,
        bonus=None,
        reward_type="fragments",
        points_needed=1000,
        is_estimated=False,
        sort_order=1,
    )
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[ending]))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: None)
    monkeypatch.setattr(
        fusion_sheets,
        "get_fusion_reminder_settings",
        AsyncMock(return_value=_settings(group_events=True, include_start_events=False, include_ending_events=True, end_lookahead_hours=1)),
    )
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))
    assert len(channel.sent) == 1
    names = [field.name for field in channel.sent[0]["embed"].fields]
    assert "Ending soon" in names


def test_grouped_include_upcoming_false_excludes_planning_context(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    due = _event(event_id="due", start_at=now - dt.timedelta(minutes=5))
    future = _event(event_id="future", start_at=now + dt.timedelta(hours=10))
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[due, future]))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: None)
    monkeypatch.setattr(
        fusion_sheets,
        "get_fusion_reminder_settings",
        AsyncMock(return_value=_settings(group_events=True, include_upcoming_events=False, start_offset_minutes=60)),
    )
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))
    assert len(channel.sent) == 1
    names = [field.name for field in channel.sent[0]["embed"].fields]
    assert "Upcoming / planning" not in names


def test_grouped_planning_only_changes_do_not_resend(monkeypatch):
    now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
    due = _event(event_id="due", start_at=now + dt.timedelta(minutes=20))
    plan_a = _event(event_id="plan-a", start_at=now + dt.timedelta(hours=10))
    plan_b = _event(event_id="plan-b", start_at=now + dt.timedelta(hours=11))
    channel = _DummyChannel()
    announcement = _DummyAnnouncementMessage("https://discord.test/jump", channel)
    sent: set[tuple[str, str]] = set()

    async def _get_sent_keys(_fusion_id: str):
        return set(sent)

    async def _mark_sent(_fusion_id: str, *, event_id: str, reminder_type: str, sent_at: dt.datetime):
        sent.add((event_id, reminder_type))

    monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[due, plan_a]))
    monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", _get_sent_keys)
    monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", _mark_sent)
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", AsyncMock(return_value=announcement))
    monkeypatch.setattr(reminders, "build_fusion_opt_in_view", lambda _target: None)
    monkeypatch.setattr(
        fusion_sheets,
        "get_fusion_reminder_settings",
        AsyncMock(return_value=_settings(group_events=True, include_upcoming_events=True, start_offset_minutes=60, upcoming_window_days=1)),
    )
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))
    monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[due, plan_b]))
    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now + dt.timedelta(minutes=1)))
    assert len(channel.sent) == 1
