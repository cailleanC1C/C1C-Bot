from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord
import pytest

from modules.community.live_arena import qualification
from modules.community.live_arena.organizer_panel import OrganizerPanelManager
from modules.community.live_arena.qualification import (
    MATCH_HEADERS,
    ROUND_HEADERS,
    QualificationRepository,
    QualificationService,
)
from modules.community.live_arena.qualification_panel import (
    QualificationPublisher,
    install_qualification,
)
from modules.community.live_arena.registration import RegistrationError

TID = "LA-2026-TRIAL-01"
NOW = datetime(2026, 8, 13, 12, 0, tzinfo=UTC)


def run(awaitable):
    return asyncio.run(awaitable)


def tournament(status="signup_closed", minimum="4"):
    return {
        "tournament_id": TID,
        "tournament_name": "C1C Live Arena Trial Cup",
        "status": status,
        "eligibility_scope": "selected_clans",
        "min_participants": minimum,
        "max_participants": "16",
        "signup_opens_at_utc": "2026-08-01T00:00:00Z",
        "signup_closes_at_utc": "2026-08-12T22:51:00Z",
        "notes": "",
    }


def slots():
    return [
        {
            "slot_id": "MON_A",
            "weekday_utc": "Monday",
            "start_time_utc": "18:00",
            "end_time_utc": "20:00",
            "enabled": "TRUE",
            "sort_order": "1",
            "display_label": "Monday 18:00–20:00 UTC",
        },
        {
            "slot_id": "TUE_A",
            "weekday_utc": "Tuesday",
            "start_time_utc": "18:00",
            "end_time_utc": "20:00",
            "enabled": "TRUE",
            "sort_order": "2",
            "display_label": "Tuesday 18:00–20:00 UTC",
        },
        {
            "slot_id": "OFF",
            "weekday_utc": "Wednesday",
            "start_time_utc": "18:00",
            "end_time_utc": "20:00",
            "enabled": "FALSE",
            "sort_order": "3",
            "display_label": "disabled",
        },
    ]


def participant(uid, name):
    return {
        "tournament_id": TID,
        "discord_user_id": str(uid),
        "display_name_at_signup": name,
        "clan_tag_at_signup": "C1CM",
        "timezone": "Europe/Vienna",
        "status": "confirmed",
        "signed_up_at_utc": "2026-08-01T00:00:00Z",
        "confirmed_at_utc": "2026-08-01T00:00:00Z",
        "withdrawn_at_utc": "",
        "withdrawal_reason": "",
        "updated_at_utc": "2026-08-01T00:00:00Z",
        "notes": "",
    }


def availability(mapping):
    return [
        {
            "tournament_id": TID,
            "discord_user_id": str(uid),
            "slot_id": slot_id,
            "created_at_utc": "2026-08-01T00:00:00Z",
            "updated_at_utc": "2026-08-01T00:00:00Z",
            "notes": "",
        }
        for uid, slot_ids in mapping.items()
        for slot_id in slot_ids
    ]


class MemoryRegistrationRepository:
    def __init__(self, participants, availability_rows):
        self.p = deepcopy(participants)
        self.a = deepcopy(availability_rows)
        self.audit = []

    async def initialize(self):
        pass

    async def participants(self):
        return deepcopy(self.p)

    async def availability(self):
        return deepcopy(self.a)

    async def append_audit(self, row):
        self.audit.append(deepcopy(row))


class MemoryQualificationRepository:
    def __init__(self):
        self.r = []
        self.m = []
        self.config = {
            "ROUNDS_TAB": "ROUNDS",
            "MATCHES_TAB": "MATCHES",
            "MATCH_FORUM_CHANNEL_ID": "10",
            "ROUND_OVERVIEW_CHANNEL_ID": "20",
        }

    async def initialize(self):
        pass

    async def rounds(self):
        return deepcopy(self.r)

    async def matches(self):
        return deepcopy(self.m)

    async def persist_state(
        self,
        rounds,
        matches,
        *,
        previous_rounds,
        previous_matches,
    ):
        self.r = deepcopy(rounds)
        self.m = deepcopy(matches)

    async def persist_rounds(self, rounds, *, previous_rounds):
        self.r = deepcopy(rounds)

    async def persist_matches(self, matches, *, previous_matches):
        self.m = deepcopy(matches)


def make_service(monkeypatch, *, mapping=None, roster=None):
    roster = roster or [
        participant(1, "One"),
        participant(2, "Two"),
        participant(3, "Three"),
        participant(4, "Four"),
    ]
    mapping = mapping or {
        1: ["MON_A"],
        2: ["MON_A"],
        3: ["TUE_A"],
        4: ["TUE_A"],
    }
    registration_repo = MemoryRegistrationRepository(roster, availability(mapping))
    qrepo = MemoryQualificationRepository()
    service = QualificationService(
        "sheet",
        registration_repository=registration_repo,
        qualification_repository=qrepo,
        clock=lambda: NOW,
        rng=SimpleNamespace(choice=lambda values: values[0]),
    )
    service.context = AsyncMock(
        return_value=(
            {"ACTIVE_TOURNAMENT_ID": TID},
            (2, tournament()),
            [],
            slots(),
        )
    )
    monkeypatch.setattr(
        qualification,
        "load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": TID}),
    )
    return service, registration_repo, qrepo


def test_q1_generation_chooses_zero_conflict_complete_draw_and_exact_rows(monkeypatch):
    service, registration_repo, qrepo = make_service(monkeypatch)
    run(service.initialize())

    snapshot = run(service.generate_draw("999"))

    assert snapshot.status == "proposed"
    assert snapshot.round_row == qrepo.r[0]
    assert snapshot.round_row["round_id"] == f"{TID}-Q1"
    assert snapshot.round_row["generated_by_discord_user_id"] == "999"
    assert snapshot.round_row["deadline_at_utc"] == ""
    assert len(snapshot.matches) == 2
    pairs = {
        frozenset(
            (row["player_a_discord_user_id"], row["player_b_discord_user_id"])
        )
        for row in snapshot.matches
    }
    assert pairs == {frozenset(("1", "2")), frozenset(("3", "4"))}
    assert all(
        row["has_scheduling_conflict"] == "FALSE" for row in snapshot.matches
    )
    assert all(set(row) == set(MATCH_HEADERS) for row in snapshot.matches)
    assert set(snapshot.round_row) == set(ROUND_HEADERS)
    assert registration_repo.audit[-1]["event_type"] == "q1_draw_generated"


def test_q1_generation_minimizes_unavoidable_conflicts(monkeypatch):
    mapping = {1: ["MON_A"], 2: ["MON_A"], 3: ["TUE_A"], 4: []}
    service, _, _ = make_service(monkeypatch, mapping=mapping)
    run(service.initialize())

    snapshot = run(service.generate_draw("999"))

    assert (
        sum(
            row["has_scheduling_conflict"] == "TRUE" for row in snapshot.matches
        )
        == 1
    )
    assert any(
        {row["player_a_discord_user_id"], row["player_b_discord_user_id"]}
        == {"1", "2"}
        for row in snapshot.matches
    )


def test_q1_generation_blocks_odd_roster_without_bye(monkeypatch):
    service, _, qrepo = make_service(
        monkeypatch,
        roster=[
            participant(1, "One"),
            participant(2, "Two"),
            participant(3, "Three"),
        ],
    )
    service.context = AsyncMock(
        return_value=(
            {"ACTIVE_TOURNAMENT_ID": TID},
            (2, tournament(minimum="3")),
            [],
            slots(),
        )
    )
    run(service.initialize())

    with pytest.raises(RegistrationError, match="even confirmed roster"):
        run(service.generate_draw("999"))
    assert qrepo.r == [] and qrepo.m == []


def test_swap_players_recalculates_only_affected_pair_availability(monkeypatch):
    service, _, _ = make_service(monkeypatch)
    run(service.initialize())
    first = run(service.generate_draw("999"))
    one = next(
        row
        for row in first.matches
        if "1" in {row["player_a_discord_user_id"], row["player_b_discord_user_id"]}
    )
    three = next(
        row
        for row in first.matches
        if "3" in {row["player_a_discord_user_id"], row["player_b_discord_user_id"]}
    )
    first_partner = (
        one["player_b_discord_user_id"]
        if one["player_a_discord_user_id"] == "1"
        else one["player_a_discord_user_id"]
    )
    second_partner = (
        three["player_b_discord_user_id"]
        if three["player_a_discord_user_id"] == "3"
        else three["player_a_discord_user_id"]
    )

    swapped = run(service.swap_players("999", first_partner, second_partner))

    assert all(row["has_scheduling_conflict"] == "TRUE" for row in swapped.matches)


def test_approve_publishes_sheet_state_with_six_day_deadline(monkeypatch):
    service, registration_repo, qrepo = make_service(monkeypatch)
    run(service.initialize())
    run(service.generate_draw("999"))

    snapshot = run(service.approve_draw("999"))

    assert snapshot.status == "active"
    assert snapshot.round_row["opens_at_utc"] == "2026-08-13T12:00:00Z"
    assert snapshot.round_row["deadline_at_utc"] == "2026-08-19T12:00:00Z"
    assert snapshot.round_row["approved_by_discord_user_id"] == "999"
    assert all(row["status"] == "published" for row in snapshot.matches)
    assert all(
        row["deadline_at_utc"] == "2026-08-19T12:00:00Z"
        for row in snapshot.matches
    )
    assert registration_repo.audit[-1]["event_type"] == "q1_draw_approved"
    assert qrepo.r[0]["overview_message_id"] == ""


def test_approve_rejects_roster_change_and_requires_regenerate(monkeypatch):
    service, registration_repo, _ = make_service(monkeypatch)
    run(service.initialize())
    run(service.generate_draw("999"))
    registration_repo.p[-1]["status"] = "removed"
    service.context = AsyncMock(
        return_value=(
            {"ACTIVE_TOURNAMENT_ID": TID},
            (2, tournament(minimum="3")),
            [],
            slots(),
        )
    )

    with pytest.raises(RegistrationError, match="even confirmed roster"):
        run(service.approve_draw("999"))


def test_qualification_repository_supports_frozen_match_width_through_AH():
    repo = QualificationRepository("sheet")
    repo.config = {"ROUNDS_TAB": "ROUNDS", "MATCHES_TAB": "MATCHES"}
    body = repo._batch_body(
        (("MATCHES_TAB", MATCH_HEADERS, [], []),), use_previous=False
    )
    assert body["data"][0]["range"] == "'MATCHES'!A2:AH2"


def test_install_qualification_adds_persistent_q1_controls_with_state_gating():
    manager = OrganizerPanelManager(SimpleNamespace(), "sheet", SimpleNamespace())
    assert install_qualification(manager) is True

    initial = manager.view("signup_closed")
    buttons = {child.custom_id: child for child in initial.children}
    assert buttons["live_arena:organizer:q1:generate"].disabled is False
    assert buttons["live_arena:organizer:q1:approve"].disabled is True

    manager._qualification_q1_status = "proposed"
    proposed = manager.view("signup_closed")
    buttons = {child.custom_id: child for child in proposed.children}
    assert buttons["live_arena:organizer:q1:generate"].disabled is True
    assert buttons["live_arena:organizer:q1:approve"].disabled is False
    assert buttons["live_arena:organizer:q1:regenerate"].disabled is False
    assert buttons["live_arena:organizer:q1:swap"].disabled is False


class FakeMessage:
    def __init__(self, message_id):
        self.id = message_id
        self.edit = AsyncMock()
        self.delete = AsyncMock()


class FakeThread:
    def __init__(self, thread_id):
        self.id = thread_id
        self.delete = AsyncMock()


class FakeForum:
    def __init__(self, bot):
        self.bot = bot
        self.calls = []

    async def create_thread(self, **kwargs):
        self.calls.append(kwargs)
        thread = FakeThread(1000 + len(self.calls))
        self.bot.channels[thread.id] = thread
        return SimpleNamespace(thread=thread, message=FakeMessage(thread.id))


class FakeOverviewChannel:
    def __init__(self):
        self.sent = []
        self.messages = {}

    async def send(self, **kwargs):
        message = FakeMessage(2000 + len(self.sent))
        self.sent.append((message, kwargs))
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id):
        return self.messages[message_id]


class FakeBot:
    def __init__(self):
        self.channels = {}

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)

    async def fetch_channel(self, channel_id):
        return self.channels[channel_id]


class PublisherService:
    def __init__(self):
        self.repository = SimpleNamespace(
            config={
                "MATCH_FORUM_CHANNEL_ID": "10",
                "ROUND_OVERVIEW_CHANNEL_ID": "20",
            }
        )
        self.round = {header: "" for header in ROUND_HEADERS}
        self.round.update(
            tournament_id=TID,
            round_id=f"{TID}-Q1",
            round_name="Qualification Round 1",
            status="active",
            opens_at_utc="2026-08-13T12:00:00Z",
            deadline_at_utc="2026-08-19T12:00:00Z",
        )
        self.matches = []
        for number, (a, b) in enumerate((("1", "2"), ("3", "4")), 1):
            row = {header: "" for header in MATCH_HEADERS}
            row.update(
                tournament_id=TID,
                round_id=f"{TID}-Q1",
                match_id=f"{TID}-Q1-M{number:02d}",
                match_number=str(number),
                player_a_discord_user_id=a,
                player_a_display_name=f"Player {a}",
                player_b_discord_user_id=b,
                player_b_display_name=f"Player {b}",
                status="published",
                shared_slot_ids_csv="MON_A",
                has_scheduling_conflict="FALSE",
            )
            self.matches.append(row)

    async def snapshot(self):
        return qualification.QualificationSnapshot(
            deepcopy(self.round), tuple(deepcopy(self.matches))
        )

    async def context(self):
        return (
            {"ACTIVE_TOURNAMENT_ID": TID},
            (2, tournament()),
            [],
            slots(),
        )

    async def record_thread_id(self, match_id, thread_id):
        row = next(row for row in self.matches if row["match_id"] == match_id)
        row["thread_id"] = str(thread_id)
        return row

    async def record_overview_message_id(self, round_id, message_id):
        assert round_id == self.round["round_id"]
        self.round["overview_message_id"] = str(message_id)
        return self.round


def test_publisher_creates_one_forum_post_per_match_requires_screenshot_and_is_idempotent():
    bot = FakeBot()
    forum = FakeForum(bot)
    overview = FakeOverviewChannel()
    bot.channels[10] = forum
    bot.channels[20] = overview
    service = PublisherService()
    publisher = QualificationPublisher(bot, service)

    warnings = run(publisher.reconcile())

    assert warnings == []
    assert len(forum.calls) == 2
    assert len(overview.sent) == 1
    assert all(row["thread_id"] for row in service.matches)
    assert service.round["overview_message_id"] == "2000"
    first_embed = forum.calls[0]["embed"]
    assert "post at least one screenshot" in first_embed.description
    assert "Best of 3" in first_embed.description

    warnings = run(publisher.reconcile())

    assert warnings == []
    assert len(forum.calls) == 2
    assert len(overview.sent) == 1
    overview.messages[2000].edit.assert_awaited_once()
