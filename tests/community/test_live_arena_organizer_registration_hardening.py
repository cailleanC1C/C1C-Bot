from __future__ import annotations

import pytest

from modules.community.live_arena.organizer_registration_hardening import (
    _prune_registration_controls,
    _defer_now,
)


class _Item:
    def __init__(self, label):
        self.label = label


class _View:
    def __init__(self, labels):
        self.children = [_Item(label) for label in labels]

    def remove_item(self, item):
        self.children.remove(item)


class _Manager:
    _qualification_q1_status = ""


def _labels(view):
    return [item.label for item in view.children]


def test_signup_open_hides_future_tournament_controls():
    view = _View(
        [
            "Open Registration",
            "Close Registration",
            "Reopen Registration",
            "View Roster",
            "Reconcile Roles",
            "Generate Q1 Draw",
            "Preview Next Swiss",
            "Freeze Top 8",
            "Create Next Tournament",
            "Repair Discord State",
            "Player History",
        ]
    )

    result = _prune_registration_controls(view, _Manager(), "signup_open")

    assert _labels(result) == [
        "Close Registration",
        "View Roster",
        "Reconcile Roles",
        "Repair Discord State",
        "Player History",
    ]


def test_signup_closed_without_draw_only_exposes_q1_generation():
    view = _View(
        [
            "Close Registration",
            "Reopen Registration",
            "View Roster",
            "Generate Q1 Draw",
            "Approve Draw",
            "Regenerate Draw",
            "Swap Players",
            "Freeze Top 8",
            "Repair Discord State",
        ]
    )

    result = _prune_registration_controls(view, _Manager(), "signup_closed")

    assert _labels(result) == [
        "Reopen Registration",
        "View Roster",
        "Generate Q1 Draw",
        "Repair Discord State",
    ]


def test_signup_closed_proposed_draw_exposes_only_draw_review_controls():
    manager = _Manager()
    manager._qualification_q1_status = "proposed"
    view = _View(
        [
            "Reopen Registration",
            "Generate Q1 Draw",
            "Approve Draw",
            "Regenerate Draw",
            "Swap Players",
            "Preview Next Swiss",
        ]
    )

    result = _prune_registration_controls(view, manager, "signup_closed")

    assert _labels(result) == [
        "Reopen Registration",
        "Approve Draw",
        "Regenerate Draw",
        "Swap Players",
    ]


@pytest.mark.asyncio
async def test_defer_now_acknowledges_once_before_work():
    calls = []

    class Response:
        def __init__(self):
            self.done = False

        def is_done(self):
            return self.done

        async def defer(self, *, ephemeral):
            calls.append(("defer", ephemeral))
            self.done = True

    class Interaction:
        response = Response()

    interaction = Interaction()
    await _defer_now(interaction)
    await _defer_now(interaction)

    assert calls == [("defer", True)]
