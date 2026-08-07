import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from modules.community.live_arena import messages, panel, registration, repository, views
from modules.community.live_arena.messages import load_messages, load_pr3_config
from modules.community.live_arena.registration import (
    LocalizedSlot,
    RegistrationError,
    SignupPreparation,
    localize_availability,
)
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
    [
        "availability_updated",
        "Availability updated",
        "{participant}, your timezone and availability for {tournament_name} were updated.",
        "#34A853",
        "TRUE",
        "",
    ],
    [
        "withdrawal_confirmed",
        "Withdrawal recorded",
        "{participant} has withdrawn from {tournament_name}.",
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
    assert set(loaded) == {
        "signup_open",
        "signup_confirmed",
        "availability_updated",
        "withdrawal_confirmed",
    }
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


@pytest.mark.parametrize(
    ("key", "change", "match"),
    [
        ("signup_open", "remove", "signup_open"),
        ("signup_confirmed", "remove", "signup_confirmed"),
        ("signup_open", "inactive", "signup_open"),
        ("signup_confirmed", "inactive", "signup_confirmed"),
        ("availability_updated", "remove", "availability_updated"),
        ("withdrawal_confirmed", "inactive", "withdrawal_confirmed"),
        ("signup_open", "color", "color_hex"),
    ],
)
def test_required_message_rows_are_strict(monkeypatch, key, change, match):
    matrix = [row[:] for row in MESSAGE_ROWS]
    row_index = next(i for i, row in enumerate(matrix) if row[0] == key)
    if change == "remove":
        matrix.pop(row_index)
    elif change == "inactive":
        matrix[row_index][4] = "FALSE"
    else:
        matrix[row_index][3] = "#nothex"
    monkeypatch.setattr(messages, "afetch_values", AsyncMock(return_value=matrix))
    with pytest.raises(LiveArenaConfigError, match=match):
        asyncio.run(load_messages("sheet", "MESSAGES"))


def test_signup_confirmed_embed_uses_exact_template_contract(monkeypatch):
    monkeypatch.setattr(messages, "afetch_values", AsyncMock(return_value=MESSAGE_ROWS))
    loaded = asyncio.run(load_messages("sheet", "MESSAGES"))
    embed = loaded["signup_confirmed"].embed(
        participant="<@7>", tournament_name="Trial Cup", signup_deadline="<t:1:F>"
    )
    assert embed.title == "You're aboard"
    assert embed.description == "<@7>, Trial Cup by <t:1:F>."
    assert embed.color.value == int("34A853", 16)


class _Worksheet:
    def __init__(self):
        self.update_cell = AsyncMock()


def test_persist_message_id_updates_only_existing_value_cell(monkeypatch):
    manager, _ = _panel_manager(monkeypatch)
    worksheet = _Worksheet()
    matrix = [row[:] for row in CONFIG]
    original = [row[:] for row in matrix]
    monkeypatch.setattr(panel, "aget_worksheet", AsyncMock(return_value=worksheet))

    async def call(function, *args):
        return await function(*args)

    monkeypatch.setattr(panel, "acall_with_backoff", call)
    asyncio.run(manager.__class__._persist_message_id(manager, matrix, "987"))
    worksheet.update_cell.assert_awaited_once_with(5, 2, "987")
    assert matrix == original


@pytest.mark.parametrize("matrix", [[*CONFIG[:4]], [*CONFIG, CONFIG[4][:]]])
def test_persist_message_id_rejects_missing_or_duplicate_key(monkeypatch, matrix):
    manager, _ = _panel_manager(monkeypatch)
    monkeypatch.setattr(panel, "aget_worksheet", AsyncMock())
    with pytest.raises(RuntimeError, match="must occur exactly once"):
        asyncio.run(manager.__class__._persist_message_id(manager, matrix, "987"))
    panel.aget_worksheet.assert_not_awaited()


def _preparation(slot_count=4):
    base = datetime(2026, 8, 3, 18, tzinfo=UTC)
    localized = []
    rows = []
    for index in range(slot_count):
        start = base + timedelta(days=index // 2, hours=2 * (index % 2))
        slot_id = f"real-{index}"
        localized.append(LocalizedSlot(slot_id, start, start + timedelta(hours=2)))
        rows.append(
            {
                "slot_id": slot_id,
                "weekday_utc": start.strftime("%A"),
                "start_time_utc": start.strftime("%H:%M"),
                "end_time_utc": (start + timedelta(hours=2)).strftime("%H:%M"),
                "enabled": "TRUE",
            }
        )
    return SignupPreparation(
        {"ACTIVE_TOURNAMENT_ID": "LA-1"},
        {
            "tournament_name": "Trial Cup",
            "signup_closes_at_utc": "2026-08-09T20:00:00Z",
        },
        tuple(rows),
        tuple(localized),
    )


def _flow(preparation=None, service=None, manager=None, member=None):
    manager = manager or SimpleNamespace(sheet_id="sheet", sync=AsyncMock())
    service = service or SimpleNamespace(register=AsyncMock())
    member = member or SimpleNamespace(id=7, display_name="Player", roles=[])
    return AvailabilityView(manager, service, preparation or _preparation(), "UTC", member)


def _edit_interaction():
    return SimpleNamespace(response=SimpleNamespace(edit_message=AsyncMock(), send_message=AsyncMock()))


def test_local_day_above_discord_option_limit_fails_clearly():
    preparation = _preparation(26)
    same_day = preparation.localized_slots[0].local_start
    preparation = SignupPreparation(
        preparation.config,
        preparation.tournament,
        preparation.slots,
        tuple(
            LocalizedSlot(slot.slot_id, same_day + timedelta(minutes=i), same_day + timedelta(hours=2, minutes=i))
            for i, slot in enumerate(preparation.localized_slots)
        ),
    )
    with pytest.raises(RegistrationError, match="25-option"):
        _flow(preparation)


def test_navigation_defaults_and_clear_day_preserve_real_slot_ids():
    flow = _flow()
    flow.selected.update({"real-0", "real-2"})
    interaction = _edit_interaction()
    asyncio.run(flow.next(interaction))
    assert flow.index == 1
    assert {option.value for option in flow.children[0].options if option.default} == {"real-2"}
    asyncio.run(flow.clear_day(interaction))
    assert flow.selected == {"real-0"}
    asyncio.run(flow.previous(interaction))
    assert {option.value for option in flow.children[0].options if option.default} == {"real-0"}


def test_review_gating_and_valid_grouped_content_are_embeds():
    flow = _flow()
    interaction = _edit_interaction()
    flow.selected.update({"real-0", "real-1"})
    asyncio.run(flow.review(interaction))
    error = interaction.response.send_message.await_args.kwargs
    assert error["ephemeral"] is True and isinstance(error["embed"], discord.Embed)
    interaction.response.send_message.reset_mock()
    flow.selected.add("real-2")
    asyncio.run(flow.review(interaction))
    rendered = interaction.response.edit_message.await_args.kwargs["embed"]
    assert isinstance(rendered, discord.Embed)
    for expected in ("Trial Cup", "UTC", "**Windows:** 3", "**Local days:** 2", "Monday", "Tuesday"):
        assert expected in rendered.description


def test_invalid_timezone_preflight_is_embed_only_and_write_free(monkeypatch):
    service = SimpleNamespace(
        initialize=AsyncMock(), prepare_signup=AsyncMock(side_effect=RegistrationError("timezone must be a valid IANA timezone")), register=AsyncMock()
    )
    manager = SimpleNamespace(sheet_id="sheet", service_factory=lambda _sheet: service)
    modal = views.TimezoneModal(manager)
    modal.timezone_input._value = "Mars/Olympus"
    interaction = SimpleNamespace(
        user=SimpleNamespace(id=7, roles=[]),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )
    asyncio.run(modal.on_submit(interaction))
    result = interaction.followup.send.await_args.kwargs
    assert result["ephemeral"] is True and isinstance(result["embed"], discord.Embed)
    service.register.assert_not_awaited()


def _submit_interaction(*, roles=(), configured_role=None, add_error=None):
    member = SimpleNamespace(
        id=7,
        display_name="Player",
        mention="<@7>",
        roles=list(roles),
        add_roles=AsyncMock(side_effect=add_error),
    )
    guild = SimpleNamespace(get_role=lambda _role_id: configured_role)
    return SimpleNamespace(
        user=member,
        guild=guild,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def _run_submit(monkeypatch, *, register_error=None, role=None, held=False, add_error=None, sync_error=None):
    service = SimpleNamespace(register=AsyncMock(side_effect=register_error))
    manager = SimpleNamespace(sheet_id="sheet", sync=AsyncMock(side_effect=sync_error))
    flow = _flow(service=service, manager=manager)
    flow.selected.update({"real-0", "real-1", "real-2"})
    role = role if role is not None else SimpleNamespace(id=99)
    interaction = _submit_interaction(roles=[role] if held else [], configured_role=role, add_error=add_error)
    config = {"MESSAGES_TAB": "MESSAGES", "PARTICIPANT_ROLE_ID": "99"}
    monkeypatch.setattr(views, "load_pr3_config", AsyncMock(return_value=(config, CONFIG)))
    monkeypatch.setattr(views, "load_messages", AsyncMock(return_value={"signup_confirmed": messages.MessageTemplate("signup_confirmed", "Confirmed", "{participant} joined {tournament_name} by {signup_deadline}.", 0x34A853)}))
    review = views.ReviewView(flow)
    asyncio.run(review.children[1].callback(interaction))
    return flow, service, manager, interaction


def test_submit_passes_exact_pr2_contract_and_refreshes_panel(monkeypatch):
    held_role = SimpleNamespace(id=8)
    service = SimpleNamespace(register=AsyncMock())
    manager = SimpleNamespace(sheet_id="sheet", sync=AsyncMock())
    flow = _flow(service=service, manager=manager)
    flow.selected.update({"real-0", "real-1", "real-2"})
    participant_role = SimpleNamespace(id=99)
    interaction = _submit_interaction(roles=[held_role], configured_role=participant_role)
    monkeypatch.setattr(views, "load_pr3_config", AsyncMock(return_value=({"MESSAGES_TAB": "MESSAGES", "PARTICIPANT_ROLE_ID": "99"}, CONFIG)))
    monkeypatch.setattr(views, "load_messages", AsyncMock(return_value={"signup_confirmed": messages.MessageTemplate("signup_confirmed", "Confirmed", "{participant} joined {tournament_name} by {signup_deadline}.", 0x34A853)}))
    review = views.ReviewView(flow)
    asyncio.run(review.children[1].callback(interaction))
    args = service.register.await_args.args
    assert args[:4] == ("7", "Player", ["8"], "UTC")
    assert set(args[4]) == {"real-0", "real-1", "real-2"}
    interaction.user.add_roles.assert_awaited_once_with(participant_role, reason="Live Arena registration confirmed")
    manager.sync.assert_awaited_once()
    assert isinstance(interaction.followup.send.await_args.kwargs["embed"], discord.Embed)


def test_pr2_rejection_is_embed_and_has_no_role_or_refresh(monkeypatch):
    flow, _, manager, interaction = _run_submit(monkeypatch, register_error=RegistrationError("closed"))
    interaction.user.add_roles.assert_not_awaited()
    manager.sync.assert_not_awaited()
    result = interaction.followup.send.await_args.kwargs
    assert result["ephemeral"] is True and isinstance(result["embed"], discord.Embed)


@pytest.mark.parametrize(("held", "add_error", "warning"), [(True, None, False), (False, RuntimeError("denied"), True)])
def test_role_assignment_is_nonredundant_and_failure_is_warning(monkeypatch, held, add_error, warning):
    _, service, manager, interaction = _run_submit(monkeypatch, held=held, add_error=add_error)
    service.register.assert_awaited_once()
    manager.sync.assert_awaited_once()
    if held:
        interaction.user.add_roles.assert_not_awaited()
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert isinstance(embed, discord.Embed)
    assert bool(embed.fields) is warning


def test_missing_role_and_panel_refresh_failure_keep_success(monkeypatch):
    _, service, manager, interaction = _run_submit(
        monkeypatch, role=SimpleNamespace(id=99), sync_error=RuntimeError("refresh failed")
    )
    interaction.guild.get_role = lambda _role_id: None
    # Re-run with the missing role because the helper submits immediately.
    interaction.followup.send.reset_mock()
    flow = _flow(service=service, manager=manager)
    flow.selected.update({"real-0", "real-1", "real-2"})
    review = views.ReviewView(flow)
    asyncio.run(review.children[1].callback(interaction))
    assert service.register.await_count == 2
    assert manager.sync.await_count == 2
    embed = interaction.followup.send.await_args.kwargs["embed"]
    assert isinstance(embed, discord.Embed) and embed.fields
