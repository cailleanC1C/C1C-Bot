from __future__ import annotations

import asyncio
from types import SimpleNamespace

from modules.community.live_arena import result_views
from modules.community.live_arena import simulation_ux_hardening as hardening


def run(awaitable):
    return asyncio.run(awaitable)


def test_tournament_resource_label_uses_year_month_and_short_name():
    tournament = {
        "tournament_name": "C1C Live Arena Trial Cup",
        "tournament_short_name": "Trial Cup",
        "signup_opens_at_utc": "2026-08-07T22:54:42Z",
    }

    assert hardening._tournament_resource_label(tournament) == "26-08 Trial Cup"


def test_match_thread_name_carries_tournament_and_preserves_both_players():
    tournament = {
        "tournament_name": "C1C Live Arena Trial Cup",
        "tournament_short_name": "Trial Cup",
        "signup_opens_at_utc": "2026-08-07T22:54:42Z",
    }
    round_row = {"round_stage": "qualification", "round_number": "1"}
    match = {
        "match_number": "2",
        "player_a_display_name": "Atlantic5penguin",
        "player_b_display_name": "Caillean",
    }

    name = hardening._match_thread_name(tournament, round_row, match)

    assert name == "26-08 Trial Cup • Q1 • M02 • Atlantic5penguin vs Caillean"
    assert len(name) <= 100


def test_match_thread_name_trims_both_long_players_instead_of_losing_second_name():
    tournament = {
        "tournament_short_name": "A Very Long Tournament Short Name",
        "signup_opens_at_utc": "2026-08-07T22:54:42Z",
    }
    round_row = {"round_stage": "qualification", "round_number": "1"}
    match = {
        "match_number": "12",
        "player_a_display_name": "A" * 80,
        "player_b_display_name": "B" * 80,
    }

    name = hardening._match_thread_name(tournament, round_row, match)

    assert len(name) <= 100
    assert "A" in name
    assert " vs " in name
    assert "B" in name


def test_screenshot_gate_accepts_image_from_any_thread_member():
    attachment = SimpleNamespace(content_type="image/png", filename="result.png")
    message = SimpleNamespace(
        author=SimpleNamespace(id=999999),
        attachments=[attachment],
    )

    class Channel:
        def history(self, *, limit=None):
            assert limit is None

            async def iterator():
                yield message

            return iterator()

    assert run(hardening._thread_has_result_screenshot(Channel(), {"1", "2"})) is True


def test_result_view_uses_hardened_report_button_after_all_installers():
    view = result_views.MatchResultView("sheet")
    report = next(
        item
        for item in view.children
        if getattr(item, "custom_id", "") == "live_arena:match:report_result"
    )

    assert isinstance(report, hardening.HardenedReportResultButton)
