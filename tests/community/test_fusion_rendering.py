import datetime as dt

from modules.community.fusion.rendering import build_fusion_announcement_embed
from shared.sheets.fusion import FusionEventRow, FusionRow


def _fusion() -> FusionRow:
    return FusionRow(
        fusion_id="f-1",
        fusion_name="Mavara",
        champion="Mavara",
        fusion_type="traditional",
        fusion_structure="",
        reward_type="fragments",
        needed=400,
        available=450,
        start_at_utc=dt.datetime(2026, 4, 8, tzinfo=dt.timezone.utc),
        end_at_utc=dt.datetime(2026, 4, 22, tzinfo=dt.timezone.utc),
        announcement_channel_id=1,
        opt_in_role_id=None,
        announcement_message_id=None,
        published_at=None,
        status="draft",
    )


def _event(day: int, idx: int) -> FusionEventRow:
    start = dt.datetime(2026, 4, day, 9, tzinfo=dt.timezone.utc)
    return FusionEventRow(
        fusion_id="f-1",
        event_id=f"e-{day}-{idx}",
        event_name=(
            f"Event {day}-{idx} with deliberately long wording to force schedule chunking across fields"
        ),
        event_type="tournament",
        category="main",
        start_at_utc=start,
        end_at_utc=start + dt.timedelta(hours=12),
        reward_amount=25,
        bonus=None,
        reward_type="fragments",
        points_needed=2000 + idx,
        is_estimated=False,
        sort_order=idx,
    )


def test_build_fusion_embed_target_and_schedule_field_chunks() -> None:
    events = []
    for day in range(8, 15):
        events.extend([_event(day, 1), _event(day, 2)])

    embed = build_fusion_announcement_embed(_fusion(), list(reversed(events)))

    assert "Target: 400 fragments needed / 450 available" in (embed.description or "")
    assert embed.fields[0].name == "Key Milestones"
    assert len(embed.fields) >= 3
    for field in embed.fields[1:]:
        assert "Schedule (Part" not in field.name

    day_headers = [field.name for field in embed.fields[1:]]
    assert day_headers == [
        "Wed, Apr 8",
        "Thu, Apr 9",
        "Fri, Apr 10",
        "Sat, Apr 11",
        "Sun, Apr 12",
        "Mon, Apr 13",
        "Tue, Apr 14",
    ]
