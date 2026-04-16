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
    monkeypatch.setattr(reminders, "ensure_fusion_announcement", ensure)

    asyncio.run(reminders.process_fusion_reminders(bot=object(), now=now))

    ensure.assert_awaited_once()
    assert channel.sent
