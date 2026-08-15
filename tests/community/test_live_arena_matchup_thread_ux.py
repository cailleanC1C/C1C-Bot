from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from modules.community.live_arena import matchup_thread_ux as ux
from modules.community.live_arena import qualification_panel, result_views


def run(awaitable):
    return asyncio.run(awaitable)


def _copy(sheet_id: str = "sheet-matchup"):
    values = {
        "match_thread_title": ("{round_name} · Match {match_number}", ""),
        "match_thread_matchup_line": ("", "{player_a_mention} vs {player_b_mention}"),
        "match_thread_actions_heading": ("⚔️ What you need to do", ""),
        "match_thread_action_1": (
            "1. Agree on a time to play",
            "Use one of the suggested times below, or arrange another time with your opponent **in this thread**.",
        ),
        "match_thread_action_2": ("2. Play all fights", "This match is **{match_format}**."),
        "match_thread_action_3": (
            "3. Post proof here",
            "After the match, post **at least one screenshot of the final result** in this thread.",
        ),
        "match_thread_action_4": (
            "4. Report the result",
            "Once the screenshot is posted, press **{report_result_button_label}** below.",
        ),
        "match_thread_deadline": ("", "**Round deadline:** {round_deadline}"),
        "match_thread_shared_availability": (
            "🕒 Suggested times",
            "{availability_windows}\n\n*These are times where both players marked themselves available. You may agree on a different time.*",
        ),
        "match_thread_no_shared_availability": (
            "⚠️ No shared time found",
            "Your saved availability does not currently overlap.\n\n**That does not stop the match.** Talk to your opponent in this thread and agree on a time that works for both of you.\n\nIf your saved availability is outdated, use **{update_availability_button_label}** below. If you genuinely cannot arrange the match before the deadline, use **{scheduling_problem_button_label}**.",
        ),
        "match_thread_availability_note": (
            "Weekly availability",
            "Your weekly availability is recurring. If your usual schedule changes, use **{update_availability_button_label}** below.",
        ),
        "match_format_standard": ("3 fights total · play all 3", ""),
        "match_format_final": ("5 fights total · play all 5", ""),
        "button_dispute_result": ("Dispute Result", ""),
        "button_scheduling_problem": ("Can't Arrange a Time", ""),
        "button_update_availability": ("Update My Availability", ""),
        "button_report_result": ("Report Match Result", ""),
        "scheduling_problem_recorded": (
            "Scheduling help requested",
            "The match is still active. You and your opponent have **24 hours** to keep trying to arrange a time before the case becomes available for organizer review.",
        ),
        "scheduling_problem_thread_notice": (
            "Scheduling assistance requested",
            "{reporter_mention} reported that they cannot arrange a match time. The match is still active; organizer review becomes available after the 24-hour grace period.",
        ),
        "screenshot_required": (
            "Screenshot required",
            "Post at least one screenshot of the match result in this thread, then use **{report_result_button_label}** again.",
        ),
    }
    ux._MATCHUP_COPY[sheet_id] = {
        key: ux.CopyTemplate(key, title, description, 0x1A73E8)
        for key, (title, description) in values.items()
    }
    ux._ACTIVE_SHEET_ID = sheet_id
    return sheet_id


def _round(stage="qualification"):
    return {
        "round_name": "Qualification Round 2" if stage == "qualification" else "Final",
        "round_stage": stage,
        "match_number": "1",
        "opens_at_utc": "2026-08-15T12:00:00Z",
        "deadline_at_utc": "2026-08-21T12:00:00Z",
    }


def _match(shared="slot-1"):
    return {
        "match_id": "T-Q2-M01",
        "match_number": "1",
        "player_a_discord_user_id": "111",
        "player_b_discord_user_id": "222",
        "shared_slot_ids_csv": shared,
        "thread_id": "777",
    }


def _slots():
    return [
        {
            "slot_id": "slot-1",
            "enabled": "TRUE",
            "display_label": "Monday 22:00-00:00",
            "weekday_utc": "Monday",
            "start_time_utc": "22:00:00",
            "end_time_utc": "00:00:00",
        }
    ]


def test_matchup_embed_is_action_first_and_plain_about_three_fights():
    _copy()

    embed = qualification_panel.match_embed({}, _round(), _match(), _slots())
    text = embed.description

    assert embed.title == "Qualification Round 2 · Match 1"
    assert "### ⚔️ What you need to do" in text
    assert "**1. Agree on a time to play**" in text
    assert "**3 fights total · play all 3**" in text
    assert "Best of 3" not in text
    assert "BO3" not in text
    assert "**Report Match Result**" in text
    assert text.index("What you need to do") < text.index("Suggested times")


def test_no_shared_time_explicitly_says_match_still_goes_ahead():
    _copy()

    embed = qualification_panel.match_embed({}, _round(), _match(shared=""), _slots())
    text = embed.description

    assert "### ⚠️ No shared time found" in text
    assert "**That does not stop the match.**" in text
    assert "**Update My Availability**" in text
    assert "**Can't Arrange a Time**" in text


def test_final_uses_sheet_backed_five_fight_format():
    _copy()

    embed = qualification_panel.match_embed({}, _round(stage="final"), _match(), _slots())

    assert "**5 fights total · play all 5**" in embed.description
    assert "Best of 5" not in embed.description


def test_final_match_view_uses_sheet_backed_button_labels():
    sheet_id = _copy()

    view = result_views.MatchResultView(sheet_id)
    labels = {
        item.custom_id: item.label
        for item in view.children
        if getattr(item, "custom_id", None)
    }

    assert labels["live_arena:match:dispute_result"] == "Dispute Result"
    assert labels["live_arena:match:report_scheduling_problem"] == "Can't Arrange a Time"
    assert labels["live_arena:availability:review_update"] == "Update My Availability"
    assert labels["live_arena:match:report_result"] == "Report Match Result"


def test_existing_q2_thread_starter_is_rerendered_from_sheet_copy():
    sheet_id = _copy()
    starter = SimpleNamespace(edit=AsyncMock())
    thread = SimpleNamespace(
        id=777,
        get_partial_message=Mock(return_value=starter),
    )
    bot = SimpleNamespace(
        get_channel=lambda _thread_id: thread,
        fetch_channel=AsyncMock(),
    )
    service = SimpleNamespace(
        sheet_id=sheet_id,
        context=AsyncMock(return_value=(None, (None, {}), None, _slots())),
    )
    snapshot = SimpleNamespace(
        round_row=_round(),
        matches=(_match(),),
    )

    warnings = run(ux._rerender_open_match_threads(bot, service, snapshot))

    assert warnings == []
    starter.edit.assert_awaited_once()
    embed = starter.edit.await_args.kwargs["embed"]
    assert "What you need to do" in embed.description
    assert "Report Match Result" in embed.description
