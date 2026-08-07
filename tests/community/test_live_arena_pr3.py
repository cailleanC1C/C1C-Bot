import asyncio
from datetime import date
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from modules.community.live_arena import messages, panel, registration, repository, views
from modules.community.live_arena.messages import load_messages, load_pr3_config
from modules.community.live_arena.registration import SignupPreparation, localize_availability
from modules.community.live_arena.service import LiveArenaConfigError, TournamentSnapshot
from modules.community.live_arena.views import AvailabilityView, JoinTournamentView


CONFIG = [
    ["Key", "Value", "Notes / clear name"],
    ["MESSAGES_TAB", "MESSAGES", ""],
    ["SIGNUP_CHANNEL_ID", "1535020329624146123", ""],
    ["PARTICIPANT_ROLE_ID", "1535031871191257189", ""],
    ["PUBLIC_PANEL_MESSAGE_ID", "", ""],
]
MESSAGE_ROWS = [
    ["message_key", "title", "description", "color_hex", "active", "notes"],
    [
        "signup_open",
        "Live Arena signups are open",
        "{tournament_name} by {signup_deadline}. Confirmed: {confirmed_count}/{max_participants}.",
        "#1A73E8",
        "TRUE",
        "",
    ],
    [
        "signup_confirmed",
        "You're aboard",
        "{participant}, {tournament_name} by {signup_deadline}.",
        "#34A853",
        "TRUE",
        "",
    ],
]


def test_literal_pr3_config_and_messages_render(monkeypatch):
    async def fetch(_sheet, tab):
        return CONFIG if tab == "CONFIG" else MESSAGE_ROWS

    monkeypatch.setattr(messages, "afetch_values", fetch)
    config, _ = asyncio.run(load_pr3_config("sheet"))
    loaded = asyncio.run(load_messages("sheet", config["MESSAGES_TAB"]))
    embed = loaded["signup_open"].embed(
        tournament_name="Trial Cup",
        signup_deadline="<t:1:F>",
        confirmed_count=4,
        max_participants=16,
    )
    assert set(loaded) == {"signup_open", "signup_confirmed"}
    assert "Trial Cup" in embed.description and "4/16" in embed.description


def test_unexpected_message_header_fails(monkeypatch):
    matrix = [MESSAGE_ROWS[0] + ["message"], *(row + [""] for row in MESSAGE_ROWS[1:])]

    async def fetch(*_args):
        return matrix

    monkeypatch.setattr(messages, "afetch_values", fetch)
    with pytest.raises(LiveArenaConfigError, match="unexpected header"):
        asyncio.run(load_messages("sheet", "MESSAGES"))


def test_localization_handles_utc_cross_midnight():
    slots = [
        dict(
            slot_id="real-slot",
            weekday_utc="Monday",
            start_time_utc="23:00",
            end_time_utc="01:00",
            enabled="TRUE",
        )
    ]
    localized = localize_availability("Asia/Kolkata", slots, "2026-08-09T20:00:00Z")
    assert localized[0].slot_id == "real-slot"
    assert localized[0].local_start.date() == date(2026, 8, 4)
    assert localized[0].local_start.hour == 4


def test_persistent_join_contract():
    view = JoinTournamentView(object())
    assert view.timeout is None
    button = view.children[0]
    assert button.custom_id == "live_arena:join"
    assert button.label == "Join Tournament"


def _snapshot(status="signup_open"):
    return TournamentSnapshot(
        "LA-1", "Trial Cup", status, "selected_clans", 1, 16,
        "2026-08-01T00:00:00Z", "2026-08-09T20:00:00Z", 1, 3, 99,
    )


class _Template:
    def embed(self, **values):
        return discord.Embed(title="Panel", description=str(values))


class _PanelRepository:
    async def initialize(self):
        return None

    async def participants(self):
        return []


class _Message:
    def __init__(self, message_id=42):
        self.id = message_id
        self.edit = AsyncMock()
        self.delete = AsyncMock()


class _Channel:
    def __init__(self, fetched=None, fetch_error=None):
        self.fetched, self.fetch_error = fetched, fetch_error
        self.sent = []

    async def fetch_message(self, _message_id):
        if self.fetch_error:
            raise self.fetch_error
        return self.fetched

    async def send(self, **_kwargs):
        message = _Message(100 + len(self.sent))
        self.sent.append(message)
        return message


def _not_found():
    return discord.NotFound(SimpleNamespace(status=404, reason="missing"), "missing")


def _panel_manager(monkeypatch, *, status="signup_open", panel_id="", channel=None):
    channel = channel or _Channel()
    matrix = [row[:] for row in CONFIG]
    matrix[4][1] = panel_id
    config = {row[0]: row[1] for row in matrix[1:]}
    monkeypatch.setattr(panel, "load_pr3_config", AsyncMock(return_value=(config, matrix)))
    monkeypatch.setattr(panel, "load_messages", AsyncMock(return_value={"signup_open": _Template()}))
    monkeypatch.setattr(panel, "load_tournament_snapshot", AsyncMock(return_value=_snapshot(status)))
    monkeypatch.setattr(panel, "LiveArenaRepository", lambda _sheet: _PanelRepository())
    bot = SimpleNamespace(get_channel=lambda _channel_id: channel)
    manager = panel.LiveArenaPanelManager(bot, f"sheet-{id(channel)}")
    manager._persist_message_id = AsyncMock()
    return manager, channel


def test_draft_creates_no_public_panel(monkeypatch):
    manager, channel = _panel_manager(monkeypatch, status="draft")
    asyncio.run(manager.sync())
    assert channel.sent == []


def test_blank_panel_creates_once_and_persists_existing_config_cell(monkeypatch):
    manager, channel = _panel_manager(monkeypatch)
    asyncio.run(manager.sync())
    assert len(channel.sent) == 1
    matrix, message_id = manager._persist_message_id.await_args.args
    assert matrix[4][0] == "PUBLIC_PANEL_MESSAGE_ID"
    assert message_id == str(channel.sent[0].id)


def test_existing_panel_edits_without_duplicate(monkeypatch):
    existing = _Message(55)
    manager, channel = _panel_manager(monkeypatch, panel_id="55", channel=_Channel(existing))
    asyncio.run(manager.sync())
    existing.edit.assert_awaited_once()
    assert channel.sent == []


@pytest.mark.parametrize("fetch_error,creates", [(_not_found(), True), (discord.Forbidden(SimpleNamespace(status=403, reason="no"), "no"), False)])
def test_fetch_failure_only_not_found_can_recreate(monkeypatch, fetch_error, creates):
    manager, channel = _panel_manager(monkeypatch, panel_id="55", channel=_Channel(fetch_error=fetch_error))
    asyncio.run(manager.sync())
    assert bool(channel.sent) is creates


def test_failed_panel_id_persistence_deletes_untracked_message(monkeypatch):
    manager, channel = _panel_manager(monkeypatch)
    manager._persist_message_id.side_effect = RuntimeError("write failed")
    with pytest.raises(RuntimeError, match="write failed"):
        asyncio.run(manager.sync())
    channel.sent[0].delete.assert_awaited_once()


def test_concurrent_managers_for_workbook_create_exactly_one_panel(monkeypatch):
    manager, channel = _panel_manager(monkeypatch)
    config = {row[0]: row[1] for row in CONFIG[1:]}

    async def persist(_matrix, message_id):
        config["PUBLIC_PANEL_MESSAGE_ID"] = message_id

    async def load(_sheet):
        matrix = [row[:] for row in CONFIG]
        matrix[4][1] = config["PUBLIC_PANEL_MESSAGE_ID"]
        return dict(config), matrix

    async def fetch(message_id):
        return next(message for message in channel.sent if message.id == int(message_id))

    monkeypatch.setattr(panel, "load_pr3_config", load)
    channel.fetch_message = fetch
    manager._persist_message_id = persist
    second = panel.LiveArenaPanelManager(manager.bot, manager.sheet_id)
    second._persist_message_id = persist
    async def concurrent_sync():
        await asyncio.gather(manager.sync(), second.sync())

    asyncio.run(concurrent_sync())
    assert len(channel.sent) == 1


def test_join_opens_timezone_modal_before_sheet_io():
    interaction = SimpleNamespace(response=SimpleNamespace(send_modal=AsyncMock()))
    view = JoinTournamentView(object())
    asyncio.run(view.children[0].callback(interaction))
    interaction.response.send_modal.assert_awaited_once()


def test_timezone_submit_initializes_real_repository_before_preflight(monkeypatch):
    initialized = False

    async def repo_config(_sheet):
        nonlocal initialized
        initialized = True
        return {"PARTICIPANTS_TAB": "PARTICIPANTS", "PARTICIPANT_AVAILABILITY_TAB": "PARTICIPANT_AVAILABILITY", "AUDIT_LOG_TAB": "AUDIT_LOG"}

    async def repo_fetch(_sheet, tab):
        headers = {"PARTICIPANTS": repository.PARTICIPANT_HEADERS, "PARTICIPANT_AVAILABILITY": repository.PARTICIPANT_AVAILABILITY_HEADERS, "AUDIT_LOG": repository.AUDIT_LOG_HEADERS}[tab]
        return [list(headers)]

    slot_rows = [
        {"slot_id": "mon", "weekday_utc": "Monday", "start_time_utc": "18:00", "end_time_utc": "20:00", "enabled": "TRUE"}
    ]
    preparation = SignupPreparation(
        {"ACTIVE_TOURNAMENT_ID": "LA-1"},
        {"tournament_name": "Trial", "signup_closes_at_utc": "2026-08-09T20:00:00Z"},
        tuple(slot_rows),
        tuple(localize_availability("UTC", slot_rows, "2026-08-09T20:00:00Z")),
    )

    class RealService(registration.RegistrationService):
        async def prepare_signup(self, *_args):
            assert initialized and self.repository.config["PARTICIPANTS_TAB"] == "PARTICIPANTS"
            return preparation

    monkeypatch.setattr(repository, "load_config", repo_config)
    monkeypatch.setattr(repository, "afetch_values", repo_fetch)
    monkeypatch.setattr(views, "load_pr3_config", AsyncMock(return_value=({"MESSAGES_TAB": "MESSAGES"}, CONFIG)))
    monkeypatch.setattr(views, "load_messages", AsyncMock(return_value={}))
    manager = SimpleNamespace(sheet_id="sheet", service_factory=RealService)
    modal = views.TimezoneModal(manager)
    modal.timezone_input._value = "UTC"
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=7, roles=[]),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    asyncio.run(modal.on_submit(interaction))
    sent = interaction.followup.send.await_args.kwargs
    assert isinstance(sent["view"], AvailabilityView)
    assert isinstance(sent["embed"], discord.Embed)
