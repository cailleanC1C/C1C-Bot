import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.community.live_arena import organizer_panel, panel
from modules.community.live_arena.entry_views import (
    MyRegistrationShortcutView,
    RegistrationEntryView,
)
from modules.community.live_arena.views import TimezoneSelectView


def _snapshot(status="", *, participant=None):
    return SimpleNamespace(
        participant=participant,
        status=status,
        tournament={"tournament_name": "C1C Live Arena Trial Cup"},
        timezone="Europe/Vienna",
        localized_slots=(),
        selected_slot_ids=(),
        tournament_status="signup_open",
        can_update=status == "confirmed",
        can_withdraw=status == "confirmed",
    )


def _manager(snapshot):
    service = SimpleNamespace(
        initialize=AsyncMock(),
        get_registration=AsyncMock(return_value=snapshot),
    )
    manager = SimpleNamespace(
        sheet_id="sheet",
        service_factory=lambda _sheet: service,
    )
    return manager, service


def _interaction():
    return SimpleNamespace(
        user=SimpleNamespace(id=77),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _button(view, custom_id):
    return next(child for child in view.children if child.custom_id == custom_id)


def test_registration_entry_keeps_persistent_public_custom_ids():
    manager, _ = _manager(_snapshot())
    view = RegistrationEntryView(manager)
    assert view.timeout is None
    assert [child.custom_id for child in view.children] == [
        "live_arena:join",
        "live_arena:my_registration",
    ]


def test_confirmed_join_exits_before_timezone_and_routes_to_my_registration():
    manager, service = _manager(_snapshot("confirmed", participant={"status": "confirmed"}))
    interaction = _interaction()
    view = RegistrationEntryView(manager)

    asyncio.run(_button(view, "live_arena:join").callback(interaction))

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    service.initialize.assert_awaited_once()
    service.get_registration.assert_awaited_once_with("77")
    sent = interaction.followup.send.await_args.kwargs
    assert sent["embed"].title == "Already registered"
    assert "My Registration" in sent["embed"].description
    assert isinstance(sent["view"], MyRegistrationShortcutView)
    assert not isinstance(sent["view"], TimezoneSelectView)


@pytest.mark.parametrize(
    ("status", "participant"),
    [
        ("", None),
        ("withdrawn", {"status": "withdrawn"}),
    ],
)
def test_join_still_allows_new_or_withdrawn_player_to_choose_timezone(
    status, participant
):
    manager, _ = _manager(_snapshot(status, participant=participant))
    interaction = _interaction()
    view = RegistrationEntryView(manager)

    asyncio.run(_button(view, "live_arena:join").callback(interaction))

    sent = interaction.followup.send.await_args.kwargs
    assert isinstance(sent["view"], TimezoneSelectView)
    assert sent["embed"].title == "Choose your timezone"


@pytest.mark.parametrize("status", ["removed", "disqualified"])
def test_join_blocks_removed_or_disqualified_without_timezone_flow(status):
    manager, _ = _manager(_snapshot(status, participant={"status": status}))
    interaction = _interaction()
    view = RegistrationEntryView(manager)

    asyncio.run(_button(view, "live_arena:join").callback(interaction))

    sent = interaction.followup.send.await_args.kwargs
    assert status in sent["embed"].description
    assert "view" not in sent


def test_already_registered_shortcut_opens_saved_registration():
    manager, service = _manager(_snapshot("confirmed", participant={"status": "confirmed"}))
    interaction = _interaction()
    view = MyRegistrationShortcutView(manager)

    asyncio.run(view.children[0].callback(interaction))

    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    service.get_registration.assert_awaited_once_with("77")
    sent = interaction.followup.send.await_args.kwargs
    assert sent["embed"].title == "My Live Arena registration"
    assert sent["ephemeral"] is True


def test_register_live_arena_wires_controls_before_deferred_startup_sync(monkeypatch):
    monkeypatch.setattr(panel, "_managers", {})
    monkeypatch.setattr(
        panel,
        "cfg",
        SimpleNamespace(get=lambda _key, _default=None: "sheet"),
    )

    class PublicManager:
        def __init__(self, bot, sheet_id):
            self.bot = bot
            self.sheet_id = sheet_id
            self.service_factory = None
            self.sync = AsyncMock(return_value=panel.PanelSyncResult(True))

    created = []

    class OrganizerManager:
        def __init__(self, bot, sheet_id, public_manager):
            self.bot = bot
            self.sheet_id = sheet_id
            self.public_manager = public_manager
            self.sync = AsyncMock(side_effect=RuntimeError("startup organizer sync failed"))
            created.append(self)

        def view(self):
            return SimpleNamespace()

    monkeypatch.setattr(panel, "LiveArenaPanelManager", PublicManager)
    monkeypatch.setattr(organizer_panel, "OrganizerPanelManager", OrganizerManager)

    scheduled = []
    monkeypatch.setattr(panel, "_schedule_startup_sync", lambda *args: scheduled.append(args))

    registered_views = []
    bot = SimpleNamespace(add_view=registered_views.append)

    result = asyncio.run(panel.register_live_arena(bot))

    public_manager = next(iter(panel._managers.values()))
    assert result is public_manager
    assert public_manager.organizer_manager is created[0]
    public_manager.sync.assert_not_awaited()
    created[0].sync.assert_not_awaited()
    assert len(registered_views) == 4
    persistent_create_next = registered_views[-1]
    assert persistent_create_next.timeout is None
    assert [child.custom_id for child in persistent_create_next.children] == [
        "live_arena:organizer:tournament:create_next"
    ]
    assert len(scheduled) == 1
