from types import SimpleNamespace

from modules.community.live_arena import captains_table_diagnostics as diagnostics
from modules.community.live_arena import tournament_lifecycle


def test_captains_table_diagnostic_hook_owns_lifecycle_sync_before_manager_binding():
    assert tournament_lifecycle._sync_organizer_panel.__module__ == diagnostics.__name__


def test_view_labels_reports_exact_visible_component_labels():
    view = SimpleNamespace(
        children=[
            SimpleNamespace(label="View Standings"),
            SimpleNamespace(label="Review Match Issues"),
            SimpleNamespace(label=""),
            SimpleNamespace(label=None),
        ]
    )

    assert diagnostics._view_labels(view) == [
        "View Standings",
        "Review Match Issues",
    ]
