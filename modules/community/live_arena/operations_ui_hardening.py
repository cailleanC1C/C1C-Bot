"""State-aware polish for Live Arena scheduling controls."""

from __future__ import annotations

from modules.community.live_arena import result_views

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    original = result_views.MatchResultView.__init__

    def init_with_scheduling_state(self, sheet_id: str, **kwargs):
        original(self, sheet_id, **kwargs)
        disabled = bool(kwargs.get("report_disabled", False))
        for item in self.children:
            if (
                getattr(item, "custom_id", "")
                == "live_arena:match:report_scheduling_problem"
            ):
                item.disabled = disabled

    result_views.MatchResultView.__init__ = init_with_scheduling_state
