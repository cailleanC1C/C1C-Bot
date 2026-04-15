import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

from modules.community.fusion import announcement_refresh
from modules.community.fusion.rendering import build_fusion_announcement_embed
from shared.sheets import fusion as fusion_sheets


def _fusion_row(
    *,
    last_refresh: dt.datetime | None,
    last_hash: str,
) -> fusion_sheets.FusionRow:
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
        opt_in_role_id=None,
        announcement_message_id=456,
        published_at=dt.datetime(2026, 4, 8, tzinfo=dt.timezone.utc),
        last_announcement_refresh_at=last_refresh,
        last_announcement_status_hash=last_hash,
        status="published",
    )


def _event(*, event_id: str, start_at: dt.datetime, end_at: dt.datetime) -> fusion_sheets.FusionEventRow:
    return fusion_sheets.FusionEventRow(
        fusion_id="f-1",
        event_id=event_id,
        event_name=f"Event {event_id}",
        event_type="tournament",
        category="main",
        start_at_utc=start_at,
        end_at_utc=end_at,
        reward_amount=25,
        bonus=None,
        reward_type="fragments",
        points_needed=2000,
        is_estimated=False,
        sort_order=1,
    )


def test_build_embed_includes_event_status_section() -> None:
    now = dt.datetime(2026, 4, 10, 12, tzinfo=dt.timezone.utc)
    events = [
        _event(
            event_id="upcoming",
            start_at=now + dt.timedelta(hours=1),
            end_at=now + dt.timedelta(hours=2),
        ),
        _event(
            event_id="live",
            start_at=now - dt.timedelta(hours=1),
            end_at=now + dt.timedelta(hours=1),
        ),
        _event(
            event_id="ended",
            start_at=now - dt.timedelta(hours=3),
            end_at=now - dt.timedelta(hours=1),
        ),
    ]
    embed = build_fusion_announcement_embed(_fusion_row(last_refresh=None, last_hash=""), events, now=now)
    status_field = next(field for field in embed.fields if field.name == "Event Status")
    assert "⏳ Event upcoming" in status_field.value
    assert "🔥 Event live" in status_field.value
    assert "✅ Event ended" in status_field.value


def test_refresh_skips_when_day_and_status_hash_unchanged(monkeypatch) -> None:
    async def _run() -> None:
        now = dt.datetime(2026, 4, 10, 12, tzinfo=dt.timezone.utc)
        events = [_event(event_id="live", start_at=now - dt.timedelta(hours=1), end_at=now + dt.timedelta(hours=1))]
        status_hash = announcement_refresh._compute_status_hash(events, now=now)
        fusion = _fusion_row(last_refresh=now - dt.timedelta(minutes=10), last_hash=status_hash)

        monkeypatch.setattr(fusion_sheets, "get_published_fusions", AsyncMock(return_value=[fusion]))
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=events))
        monkeypatch.setattr(announcement_refresh, "ensure_fusion_announcement", AsyncMock())
        monkeypatch.setattr(fusion_sheets, "update_fusion_announcement_refresh_state", AsyncMock())

        await announcement_refresh.process_fusion_announcement_refreshes(bot=object(), now=now)

        announcement_refresh.ensure_fusion_announcement.assert_not_awaited()
        fusion_sheets.update_fusion_announcement_refresh_state.assert_not_awaited()

    asyncio.run(_run())


def test_refresh_edits_existing_announcement_and_updates_metadata(monkeypatch) -> None:
    async def _run() -> None:
        now = dt.datetime(2026, 4, 10, 12, tzinfo=dt.timezone.utc)
        events = [_event(event_id="live", start_at=now - dt.timedelta(hours=1), end_at=now + dt.timedelta(hours=1))]
        fusion = _fusion_row(last_refresh=now - dt.timedelta(days=1), last_hash="")
        message = SimpleNamespace(edit=AsyncMock())
        channel = SimpleNamespace(fetch_message=AsyncMock(return_value=message))

        monkeypatch.setattr(fusion_sheets, "get_published_fusions", AsyncMock(return_value=[fusion]))
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=events))
        monkeypatch.setattr(announcement_refresh, "resolve_announcement_channel", AsyncMock(return_value=channel))
        monkeypatch.setattr(announcement_refresh, "ensure_fusion_announcement", AsyncMock())
        monkeypatch.setattr(fusion_sheets, "update_fusion_announcement_refresh_state", AsyncMock())

        await announcement_refresh.process_fusion_announcement_refreshes(bot=object(), now=now)

        message.edit.assert_awaited_once()
        fusion_sheets.update_fusion_announcement_refresh_state.assert_awaited_once()
        announcement_refresh.ensure_fusion_announcement.assert_not_awaited()

    asyncio.run(_run())
