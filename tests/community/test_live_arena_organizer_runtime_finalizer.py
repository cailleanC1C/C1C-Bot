from types import SimpleNamespace

import discord

from modules.community.live_arena.organizer_registration_runtime_finalizer import (
    _finalize_manager_view,
)


def _decorated_manager():
    manager = SimpleNamespace(
        sheet_id="sheet",
        _qualification_q1_status="",
        _organizer_runtime_finalized=False,
    )

    def pre_history_view(status=None):
        view = discord.ui.View(timeout=None)
        labels = [
            "Open Registration",
            "Close Registration",
            "Reopen Registration",
            "View Roster",
            "Reconcile Roles",
            "Complete Tournament",
            "Archive Tournament",
            "Generate Q1 Draw",
            "Approve Draw",
            "Regenerate Draw",
            "Swap Players",
            "Close Current Round",
            "Review Result Issues",
            "Reopen Closed Round",
            "Repair Discord State",
            "View Standings",
            "Preview Next Swiss",
            "Regenerate Swiss Preview",
            "Approve & Publish Swiss",
            "Repair Swiss Conflict",
            "Freeze Top 8",
            "Approve & Open Knockout",
            "Record BO3 Tiebreak",
            "Competition Ops",
            "Create Next Tournament",
        ]
        for index, label in enumerate(labels):
            view.add_item(
                discord.ui.Button(
                    label=label,
                    custom_id=f"test:{index}",
                    row=index // 5,
                )
            )
        return view

    def history_view(status=None):
        result = pre_history_view(status)
        result.add_item(discord.ui.Button(label="Player History", custom_id="test:history"))
        return result

    history_view.__module__ = "modules.community.live_arena.hall_of_fame"

    def roster_lock_view(status=None):
        return history_view(status)

    manager.view = roster_lock_view
    return manager


def test_finalizer_keeps_persistent_view_within_discord_component_limit():
    manager = _decorated_manager()

    assert _finalize_manager_view(manager) is True

    view = manager.view(None)
    assert len(view.children) == 25
    assert "Player History" not in {
        getattr(child, "label", None) for child in view.children
    }


def test_finalizer_prunes_signup_open_before_adding_player_history():
    manager = _decorated_manager()
    assert _finalize_manager_view(manager) is True

    view = manager.view("signup_open")
    labels = {getattr(child, "label", None) for child in view.children}

    assert labels == {
        "Close Registration",
        "View Roster",
        "Reconcile Roles",
        "Repair Discord State",
        "Player History",
    }
    assert len(view.children) == 5
