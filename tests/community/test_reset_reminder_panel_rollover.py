import asyncio
import datetime as dt

import pytest

from modules.community.reset_reminders import scheduler
from modules.community.reset_reminders.panel_rollover import (
    _build_rollover_embed,
    _first_future_reset,
    _rollover_due_panels,
)


class _DummyMessage:
    def __init__(self, message_id: int, *, embeds=None) -> None:
        self.id = message_id
        self.embeds = list(embeds or [])
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if kwargs.get("embed") is not None:
            self.embeds = [kwargs["embed"]]
        return self


class _DummyChannel:
    def __init__(self, message: _DummyMessage) -> None:
        self.message = message

    async def fetch_message(self, message_id: int):
        assert message_id == self.message.id
        return self.message


@pytest.fixture(autouse=True)
def _reset_rollover_state(monkeypatch):
    monkeypatch.setattr(scheduler, "_panel_rollover_completed", set())
    monkeypatch.setattr(
        scheduler,
        "_last_successful_load",
        {"tab_name": "ResetTab", "header_map": {}, "records": None},
    )


def _make_reminder(**overrides):
    reset_time = dt.datetime(2026, 8, 7, 11, 30, tzinfo=dt.timezone.utc)
    values = {
        "reset_id": "grim_forest",
        "label": "Grim Forest",
        "status": "active",
        "reference_date_utc": reset_time,
        "cycle_days": 30,
        "lead_minutes": 240,
        "role_id": 999,
        "channel_id": 123,
        "thread_id": None,
        "embed_title": "Grim Forest reset",
        "embed_description": "Reset incoming",
        "embed_footer": "footer",
        "button_label_opt_in": "Opt in",
        "button_label_opt_out": "Opt out",
        "last_sent_for_reset_utc": reset_time,
        "next_scheduled_post_utc": dt.datetime(
            2026, 9, 6, 7, 30, tzinfo=dt.timezone.utc
        ),
        "last_message_id": 111,
        "emoji_name_or_id": "",
    }
    values.update(overrides)
    return scheduler.ResetReminder(**values)


def _cache(reminder):
    scheduler._last_successful_load["records"] = [
        scheduler._ResetReminderRecord(row_number=7, reminder=reminder)
    ]


def test_due_registry_arms_panel_rollover_at_actual_reset_time() -> None:
    reminder = _make_reminder()
    _cache(reminder)

    assert scheduler._earliest_cached_due() == reminder.last_sent_for_reset_utc


def test_rollover_edits_existing_panel_in_place_without_new_ping(monkeypatch) -> None:
    reminder = _make_reminder()
    _cache(reminder)
    message = _DummyMessage(reminder.last_message_id)
    channel = _DummyChannel(message)

    async def _resolve_target(_bot, resolved_reminder):
        assert resolved_reminder is reminder
        return channel

    monkeypatch.setattr(scheduler, "_resolve_target_channel", _resolve_target)

    asyncio.run(
        _rollover_due_panels(
            scheduler,
            object(),
            now_utc=reminder.last_sent_for_reset_utc,
        )
    )

    assert len(message.edits) == 1
    edit = message.edits[0]
    assert set(edit) == {"embed", "view"}
    assert "content" not in edit
    assert "attachments" not in edit

    next_reset = reminder.last_sent_for_reset_utc + dt.timedelta(
        days=reminder.cycle_days
    )
    embed = edit["embed"]
    assert f"<t:{int(next_reset.timestamp())}:F>" in embed.description
    assert f"<t:{int(next_reset.timestamp())}:R>" in embed.description
    following_reset = next_reset + dt.timedelta(days=reminder.cycle_days)
    assert embed.footer.text.endswith(
        following_reset.strftime("%Y-%m-%d %H:%M UTC")
    )

    # Once the standing panel is rolled, the next scheduler wake-up returns to
    # the configured fresh-reminder post time for the following cycle.
    assert scheduler._earliest_cached_due() == reminder.next_scheduled_post_utc


def test_restart_reconcile_does_not_reedit_panel_already_showing_current_cycle(
    monkeypatch,
) -> None:
    reminder = _make_reminder()
    _cache(reminder)
    next_reset = reminder.last_sent_for_reset_utc + dt.timedelta(
        days=reminder.cycle_days
    )
    current_embed = _build_rollover_embed(scheduler, reminder, next_reset)
    message = _DummyMessage(reminder.last_message_id, embeds=[current_embed])
    channel = _DummyChannel(message)

    async def _resolve_target(_bot, _reminder):
        return channel

    monkeypatch.setattr(scheduler, "_resolve_target_channel", _resolve_target)

    asyncio.run(
        _rollover_due_panels(
            scheduler,
            object(),
            now_utc=reminder.last_sent_for_reset_utc + dt.timedelta(days=1),
        )
    )

    assert message.edits == []
    assert scheduler._earliest_cached_due() == reminder.next_scheduled_post_utc


def test_rollover_catches_up_to_first_future_cycle_after_long_downtime() -> None:
    displayed = dt.datetime(2026, 5, 29, 10, 0, tzinfo=dt.timezone.utc)
    now = dt.datetime(2026, 7, 10, 12, 0, tzinfo=dt.timezone.utc)

    assert _first_future_reset(
        displayed,
        cycle_days=28,
        now_utc=now,
    ) == dt.datetime(2026, 7, 24, 10, 0, tzinfo=dt.timezone.utc)
