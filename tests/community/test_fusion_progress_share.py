import datetime as dt
from dataclasses import replace

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
        start_at_utc=dt.datetime(2026, 8, 28, tzinfo=dt.timezone.utc),
        end_at_utc=dt.datetime(2026, 8, 29, tzinfo=dt.timezone.utc),
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

    summary_field = next(field for field in embed.fields if field.name == "Event/Tournament Progress")
    assert "✅ Done: 1" in summary_field.value
    assert "Progress: 5 / 450 fragments" in summary_field.value
    strategic_field = next(field for field in embed.fields if field.name == "\u200b")
    assert "**Fragments Progress**" in strategic_field.value
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


def test_build_progress_share_embed_uses_dynamic_reward_unit():
    titan = replace(_fusion_row(), reward_type="points", available=1750, fusion_type="titan")
    event = replace(_event_row("e1", "Dungeon Dash"), reward_amount=25.0, reward_type="points")
    embed = progress_share.build_progress_share_embed(
        target=titan,
        events=[event],
        progress_by_event={"e1": "done"},
        user_display_name="Tester",
        mode="summary",
    )

    summary_field = next(field for field in embed.fields if field.name == "Event/Tournament Progress")
    strategic_field = next(field for field in embed.fields if field.name == "\u200b")
    assert "Progress: 25 / 1750 points" in summary_field.value
    assert "**Points Progress**" in strategic_field.value


def test_traditional_progress_share_includes_event_and_champion_prep():
    prep = fusion_sheets.FusionTraditionalUserProgressRow(
        fusion_id="f-1",
        user_id="42",
        rares_owned=7,
        rares_level_40=3,
        rares_ascended=2,
        epics_fused=1,
        epics_level_50=1,
        epics_ascended=0,
        target_ready=True,
    )
    target = replace(_fusion_row(), needed=16, available=17, reward_type="rares")
    events = [replace(_event_row(f"e{i}", f"Event {i}"), reward_type="rare", reward_amount=1.0) for i in range(1, 18)]
    progress = {"e1": "done", "e2": "done", "e3": "done", "e4": "in_progress"}

    embed = progress_share.build_progress_share_embed(
        target=target,
        events=events,
        progress_by_event=progress,
        traditional_prep=prep,
        user_display_name="Tester",
        mode="detailed",
    )

    event_field = next(field for field in embed.fields if field.name == "Event/Tournament Progress")
    assert "✅ Done: 3" in event_field.value
    assert "🟡 In Progress: 1" in event_field.value
    assert "⬜ Not Started: 13" in event_field.value
    rare_field = next(field for field in embed.fields if field.name == "Rare Progress")
    assert "3 acquired" in rare_field.value
    assert not any(field.name == "\u200b" for field in embed.fields)
    prep_field = next(field for field in embed.fields if field.name == "Champion Preparation")
    assert "Rares level 40: 3" in prep_field.value
    assert "Epics ascended: 0" in prep_field.value
    assert "Rares acquired: 3 / 16" in prep_field.value
    assert "Rare sources available: 17" in prep_field.value
    assert "Manual inventory" not in prep_field.value
    assert "Known rare count" not in prep_field.value
    assert "Target ready: Yes" in prep_field.value
    assert any(field.name == "Event Breakdown" for field in embed.fields)


def test_traditional_progress_share_uses_event_acquired_required_rare_ratio():
    target = replace(_fusion_row(), needed=16, available=17, reward_type="rares")
    events = [replace(_event_row(f"rare_{idx}", f"Rare {idx}"), reward_type="rare", reward_amount=1.0) for idx in range(1, 18)]
    progress = {f"rare_{idx}": "done" for idx in range(1, 5)}
    progress["rare_5"] = "in_progress"
    prep = fusion_sheets.FusionTraditionalUserProgressRow(
        fusion_id="f-1",
        user_id="42",
        rares_owned=0,
        rares_level_40=2,
        rares_ascended=2,
        epics_fused=0,
        epics_level_50=0,
        epics_ascended=0,
        target_ready=False,
    )

    embed = progress_share.build_progress_share_embed(
        target=target,
        events=events,
        progress_by_event=progress,
        traditional_prep=prep,
        user_display_name="Tester",
        mode="summary",
    )

    rare_field = next(field for field in embed.fields if field.name == "Rare Progress")
    prep_field = next(field for field in embed.fields if field.name == "Champion Preparation")
    assert "4 acquired" in rare_field.value
    assert "0 skipped" in rare_field.value
    assert "12 to go" in rare_field.value
    assert "4 / 16 required rares" in rare_field.value
    assert "16 / 17 needed" not in rare_field.value
    assert "Rares acquired: 4 / 16" in prep_field.value
    assert "Rares still needed: 12" in prep_field.value
    assert "Rare sources available: 17" in prep_field.value
    assert "Manual inventory" not in prep_field.value
    assert "Known rare count" not in prep_field.value
