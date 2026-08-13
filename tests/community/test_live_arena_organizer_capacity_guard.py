from types import SimpleNamespace

import discord

from modules.community.live_arena.organizer_capacity_guard import (
    _capacity_safe_history_view,
)


def _base_view_with(count):
    def view(_status=None):
        result = discord.ui.View(timeout=None)
        for index in range(count):
            result.add_item(
                discord.ui.Button(
                    label=f"Control {index}",
                    custom_id=f"capacity:{index}",
                    row=index // 5,
                )
            )
        return result

    return view


def test_history_shortcut_never_becomes_invalid_26th_component():
    manager = SimpleNamespace()
    view = _capacity_safe_history_view(manager, _base_view_with(25))(None)

    assert len(view.children) == 25
    assert "Player History" not in {
        getattr(child, "label", None) for child in view.children
    }


def test_history_shortcut_is_added_when_capacity_exists():
    manager = SimpleNamespace()
    view = _capacity_safe_history_view(manager, _base_view_with(4))("signup_open")

    assert len(view.children) == 5
    assert "Player History" in {
        getattr(child, "label", None) for child in view.children
    }
