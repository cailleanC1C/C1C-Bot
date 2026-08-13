from types import SimpleNamespace

from modules.community.live_arena.competition_operations_runtime import CompetitionOperationsView


def test_organizer_actions_use_human_friendly_labels():
    view = CompetitionOperationsView(SimpleNamespace())
    labels = [item.label for item in view.children]

    assert labels == [
        "Scheduling Queue",
        "Extend Round",
        "Mandatory Time",
        "Resolve Scheduling Issue",
        "Withdraw Player",
    ]
