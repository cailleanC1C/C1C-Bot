import datetime as dt

from modules.community.fusion import progress_share
from shared.sheets import fusion as fusion_sheets


def _fusion_row() -> fusion_sheets.FusionRow:
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
        opt_in_role_id=777,
        announcement_message_id=456,
        published_at=dt.datetime(2026, 4, 7, tzinfo=dt.timezone.utc),
        last_announcement_refresh_at=None,
        last_announcement_status_hash="",
        status="active",
    )


def _event_row(event_id: str, event_name: str) -> fusion_sheets.FusionEventRow:
    return fusion_sheets.FusionEventRow(
        fusion_id="f-1",
        event_id=event_id,
        event_name=event_name,
        event_type="dungeon",
        category="Tournaments",
        start_at_utc=dt.datetime(2026, 4, 28, tzinfo=dt.timezone.utc),
        end_at_utc=dt.datetime(2026, 4, 29, tzinfo=dt.timezone.utc),
        reward_amount=5.0,
        bonus=None,
        reward_type="fragments",
        points_needed=None,
        is_estimated=False,
        sort_order=1,
    )


def test_build_progress_share_embed_summary_mode_has_generic_summary_block():
    embed = progress_share.build_progress_share_embed(
        target=_fusion_row(),
        events=[_event_row("e1", "Dungeon Dash")],
        progress_by_event={"e1": "done"},
        user_display_name="Tester",
        mode="summary",
    )

    summary_field = next(field for field in embed.fields if field.name == "Summary")
    assert "✅ Done: 1" in summary_field.value
    assert "Progress: 5 / 450 fragments" in summary_field.value
    assert all(field.name != "Event Breakdown" for field in embed.fields)


def test_build_progress_share_embed_detailed_mode_adds_event_breakdown():
    embed = progress_share.build_progress_share_embed(
        target=_fusion_row(),
        events=[_event_row("e1", "Dungeon Dash"), _event_row("e2", "Arena Rush")],
        progress_by_event={"e1": "done", "e2": "in_progress"},
        user_display_name="Tester",
        mode="detailed",
    )

    detail_field = next(field for field in embed.fields if field.name == "Event Breakdown")
    assert "Dungeon Dash: Done" in detail_field.value
    assert "Arena Rush: In Progress" in detail_field.value
