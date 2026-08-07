from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime

import pytest

from modules.community.live_arena import registration, repository, service

TID = "LA-1"
NOW = datetime(2026, 3, 1, 12, tzinfo=UTC)


def tournament(**changes):
    row = dict(
        tournament_id=TID,
        tournament_name="Trial",
        status="signup_open",
        eligibility_scope="selected_clans",
        min_participants="1",
        max_participants="3",
        signup_opens_at_utc="2026-02-01T00:00:00Z",
        signup_closes_at_utc="2026-03-08T12:00:00Z",
        notes="",
    )
    row.update(changes)
    return row


def slots():
    return [
        dict(
            slot_id="mon-a",
            weekday_utc="Monday",
            start_time_utc="23:00",
            end_time_utc="01:00",
            enabled="TRUE",
            sort_order="1",
            display_label="A",
        ),
        dict(
            slot_id="tue-a",
            weekday_utc="Tuesday",
            start_time_utc="01:00",
            end_time_utc="03:00",
            enabled="TRUE",
            sort_order="2",
            display_label="B",
        ),
        dict(
            slot_id="wed-a",
            weekday_utc="Wednesday",
            start_time_utc="02:00",
            end_time_utc="04:00",
            enabled="TRUE",
            sort_order="3",
            display_label="C",
        ),
        dict(
            slot_id="off",
            weekday_utc="Thursday",
            start_time_utc="00:00",
            end_time_utc="02:00",
            enabled="FALSE",
            sort_order="4",
            display_label="D",
        ),
    ]


CLANS = [
    dict(
        tournament_id=TID,
        clan_tag="C1CM",
        clan_name="Martyrs",
        discord_role_id="10",
        active="TRUE",
        notes="",
    )
]


class MemoryRepository:
    def __init__(self, participants=None, availability=None):
        self.p = deepcopy(participants or [])
        self.a = deepcopy(availability or [])
        self.audit = []
        self.fail_core = False
        self.fail_participants = False
        self.fail_audit = False

    async def initialize(self):
        pass

    async def participants(self):
        return deepcopy(self.p)

    async def availability(self):
        return deepcopy(self.a)

    async def persist_core_state(
        self,
        participants,
        availability,
        *,
        previous_participants,
        previous_availability,
    ):
        if self.fail_core:
            self.fail_core = False
            raise RuntimeError("write failed")
        self.p = deepcopy(participants)
        self.a = deepcopy(availability)

    async def persist_participants(self, participants, *, previous_participants):
        if self.fail_participants:
            self.fail_participants = False
            raise RuntimeError("write failed")
        self.p = deepcopy(participants)

    async def append_audit(self, row):
        if self.fail_audit:
            raise RuntimeError("audit failed")
        self.audit.append(deepcopy(row))


def make_service(repo, tour=None, slot_rows=None, clan_rows=None):
    instance = registration.RegistrationService("sheet", repo, lambda: NOW)

    async def context():
        return (
            {"ACTIVE_TOURNAMENT_ID": TID},
            tour or tournament(),
            CLANS if clan_rows is None else clan_rows,
            slots() if slot_rows is None else slot_rows,
        )

    instance._context = context
    return instance


def run(awaitable):
    return asyncio.run(awaitable)


def test_get_registration_returns_localized_read_only_snapshot():
    participant = dict(
        tournament_id=TID,
        discord_user_id="7",
        timezone="Asia/Kolkata",
        status="confirmed",
    )
    availability = [
        dict(tournament_id=TID, discord_user_id="7", slot_id=slot_id)
        for slot_id in ("mon-a", "tue-a", "wed-a")
    ]
    instance = make_service(MemoryRepository([participant], availability))
    result = run(instance.get_registration("7"))
    assert result.participant["status"] == "confirmed"
    assert result.status == "confirmed"
    assert result.timezone == "Asia/Kolkata"
    assert result.selected_slot_ids == ("mon-a", "tue-a", "wed-a")
    assert [slot.slot_id for slot in result.localized_slots] == [
        "mon-a",
        "tue-a",
        "wed-a",
    ]
    assert result.can_update is True and result.can_withdraw is True


@pytest.mark.parametrize(
    "headers,key",
    [
        (repository.PARTICIPANT_HEADERS, "PARTICIPANTS_TAB"),
        (repository.PARTICIPANT_AVAILABILITY_HEADERS, "PARTICIPANT_AVAILABILITY_TAB"),
        (repository.AUDIT_LOG_HEADERS, "AUDIT_LOG_TAB"),
    ],
)
def test_exact_header_only_write_schemas_and_missing_header(headers, key):
    assert service._rows([list(headers)], headers, key) == []
    with pytest.raises(service.LiveArenaConfigError, match="required header missing"):
        service._rows([list(headers[:-1])], headers, key)


def test_repository_routes_all_write_tabs_from_config(monkeypatch):
    seen = []

    async def config(_):
        return {
            "PARTICIPANTS_TAB": "PEOPLE",
            "PARTICIPANT_AVAILABILITY_TAB": "TIMES",
            "AUDIT_LOG_TAB": "EVENTS",
        }

    async def fetch(_, tab):
        seen.append(tab)
        return {
            "PEOPLE": [list(repository.PARTICIPANT_HEADERS)],
            "TIMES": [list(repository.PARTICIPANT_AVAILABILITY_HEADERS)],
            "EVENTS": [list(repository.AUDIT_LOG_HEADERS)],
        }[tab]

    monkeypatch.setattr(repository, "load_config", config)
    monkeypatch.setattr(repository, "afetch_values", fetch)
    run(repository.LiveArenaRepository("sheet").initialize())
    assert seen == ["PEOPLE", "TIMES", "EVENTS"]


def test_timezone_slot_count_local_days_cross_midnight_and_dst_anchor():
    assert registration.validate_availability(
        "America/New_York",
        ["mon-a", "tue-a", "wed-a", "mon-a"],
        slots(),
        "2026-03-08T12:00:00Z",
    ) == ["mon-a", "tue-a", "wed-a"]
    with pytest.raises(registration.RegistrationError, match="IANA"):
        registration.validate_availability(
            "Mars/Olympus", ["mon-a", "tue-a", "wed-a"], slots(), "2026-03-08T12:00:00Z"
        )
    with pytest.raises(registration.RegistrationError, match="at least 3"):
        registration.validate_availability(
            "UTC", ["mon-a", "tue-a"], slots(), "2026-03-08T12:00:00Z"
        )
    same_day = [
        dict(
            row,
            weekday_utc="Monday",
            start_time_utc=f"{hour:02}:00",
            end_time_utc=f"{hour + 2:02}:00",
        )
        for row, hour in zip(slots()[:3], (0, 2, 4))
    ]
    with pytest.raises(registration.RegistrationError, match="2 local"):
        registration.validate_availability(
            "UTC", ["mon-a", "tue-a", "wed-a"], same_day, "2026-07-01T00:00:00Z"
        )
    with pytest.raises(registration.RegistrationError, match="disabled"):
        registration.validate_availability(
            "UTC", ["mon-a", "tue-a", "off"], slots(), "2026-03-08T12:00:00Z"
        )
    with pytest.raises(registration.RegistrationError, match="unknown"):
        registration.validate_availability(
            "UTC", ["mon-a", "tue-a", "missing"], slots(), "2026-03-08T12:00:00Z"
        )

    dst_boundary_slots = [
        dict(
            slot_id=f"dst-{hour}",
            weekday_utc="Monday",
            start_time_utc=f"{hour:02}:00",
            end_time_utc=f"{hour + 2:02}:00",
            enabled="TRUE",
        )
        for hour in (0, 1, 4)
    ]
    selected = ["dst-0", "dst-1", "dst-4"]
    # The same UTC slots cross two New York start-dates only in the week after
    # DST begins, proving that signup_closes_at_utc selects the anchor week.
    assert (
        registration.validate_availability(
            "America/New_York", selected, dst_boundary_slots, "2026-03-15T12:00:00Z"
        )
        == selected
    )
    with pytest.raises(registration.RegistrationError, match="2 local"):
        registration.validate_availability(
            "America/New_York", selected, dst_boundary_slots, "2026-03-08T12:00:00Z"
        )


def test_new_registration_deduplicates_slots_and_writes_exact_audit():
    repo = MemoryRepository()
    svc = make_service(repo)
    run(
        svc.register("1", "Player", ["10"], "UTC", ["mon-a", "tue-a", "wed-a", "mon-a"])
    )
    assert len(repo.p) == 1 and repo.p[0]["status"] == "confirmed" and len(repo.a) == 3
    assert repo.audit[0]["event_type"] == "registration_confirmed"
    assert repo.audit[0]["created_at_utc"] == "2026-03-01T12:00:00Z"
    assert (
        repo.audit[0]["actor_discord_user_id"]
        == repo.audit[0]["target_discord_user_id"]
        == "1"
    )
    assert all(
        value.endswith("Z")
        for value in (
            repo.p[0]["signed_up_at_utc"],
            repo.p[0]["confirmed_at_utc"],
            repo.p[0]["updated_at_utc"],
            repo.audit[0]["created_at_utc"],
        )
    )
    with pytest.raises(registration.RegistrationError, match="already"):
        run(svc.register("1", "Player", ["10"], "UTC", ["mon-a", "tue-a", "wed-a"]))
    assert len(repo.p) == 1


@pytest.mark.parametrize("status", ["removed", "disqualified", "mystery"])
def test_non_restorable_and_unknown_statuses_reject(status):
    repo = MemoryRepository(
        [dict(tournament_id=TID, discord_user_id="1", status=status)]
    )
    with pytest.raises(
        registration.RegistrationError,
        match=status if status != "mystery" else "unsupported",
    ):
        run(
            make_service(repo).register(
                "1", "P", ["10"], "UTC", ["mon-a", "tue-a", "wed-a"]
            )
        )


def test_eligibility_and_scope_rules():
    with pytest.raises(registration.RegistrationError, match="eligible clan"):
        run(
            make_service(MemoryRepository()).register(
                "1", "P", ["99"], "UTC", ["mon-a", "tue-a", "wed-a"]
            )
        )
    with pytest.raises(registration.RegistrationError, match="unsupported eligibility"):
        run(
            make_service(
                MemoryRepository(), tournament(eligibility_scope="everyone")
            ).register("1", "P", ["10"], "UTC", ["mon-a", "tue-a", "wed-a"])
        )


def test_reregister_updates_same_row_preserves_signup_and_replaces_availability():
    old = dict(
        tournament_id=TID,
        discord_user_id="1",
        display_name_at_signup="Old",
        clan_tag_at_signup="OLD",
        timezone="UTC",
        status="withdrawn",
        signed_up_at_utc="2025-01-01T00:00:00Z",
        confirmed_at_utc="2025-01-01T00:00:00Z",
        withdrawn_at_utc="x",
        withdrawal_reason="x",
        updated_at_utc="x",
        notes="",
    )
    repo = MemoryRepository(
        [old],
        [
            dict(
                tournament_id=TID,
                discord_user_id="1",
                slot_id="mon-a",
                created_at_utc="old",
                updated_at_utc="old",
                notes="",
            )
        ],
    )
    run(
        make_service(repo).register(
            "1", "New", ["10"], "Europe/London", ["mon-a", "tue-a", "wed-a"]
        )
    )
    assert len(repo.p) == 1 and repo.p[0]["signed_up_at_utc"] == "2025-01-01T00:00:00Z"
    assert (
        repo.p[0]["withdrawn_at_utc"],
        repo.p[0]["withdrawal_reason"],
        repo.p[0]["clan_tag_at_signup"],
    ) == ("", "", "C1CM")
    assert next(r for r in repo.a if r["slot_id"] == "mon-a")["created_at_utc"] == "old"
    assert next(r for r in repo.a if r["slot_id"] == "tue-a")[
        "created_at_utc"
    ].endswith("Z")
    assert repo.audit[0]["event_type"] == "registration_reconfirmed"
    assert repo.audit[0]["created_at_utc"] == "2026-03-01T12:00:00Z"


def test_capacity_and_concurrent_last_place_race():
    existing = [dict(tournament_id=TID, discord_user_id="0", status="confirmed")]
    repo = MemoryRepository(existing)
    svc = make_service(repo, tournament(max_participants="2"))

    async def race():
        return await asyncio.gather(
            svc.register("1", "A", ["10"], "UTC", ["mon-a", "tue-a", "wed-a"]),
            svc.register("2", "B", ["10"], "UTC", ["mon-a", "tue-a", "wed-a"]),
            return_exceptions=True,
        )

    results = run(race())
    assert (
        sum(isinstance(item, registration.RegistrationError) for item in results) == 1
    )
    assert sum(r.get("status") == "confirmed" for r in repo.p) == 2


def test_withdraw_preserves_availability_and_frees_capacity():
    participant = dict(
        tournament_id=TID,
        discord_user_id="1",
        status="confirmed",
        signed_up_at_utc="old",
    )
    saved = [dict(tournament_id=TID, discord_user_id="1", slot_id="mon-a")]
    repo = MemoryRepository([participant], saved)
    svc = make_service(repo, tournament(max_participants="1"))
    run(svc.withdraw("1", "busy"))
    assert repo.p[0]["status"] == "withdrawn" and repo.a == saved
    run(svc.register("2", "B", ["10"], "UTC", ["mon-a", "tue-a", "wed-a"]))
    assert repo.audit[0]["event_type"] == "registration_withdrawn"
    assert repo.audit[0]["created_at_utc"] == "2026-03-01T12:00:00Z"


def test_update_preserves_identity_fields_and_rolls_back_core_failure():
    participant = dict(
        tournament_id=TID,
        discord_user_id="1",
        status="confirmed",
        signed_up_at_utc="signed",
        confirmed_at_utc="confirmed",
        clan_tag_at_signup="C1CM",
        timezone="UTC",
        updated_at_utc="old",
    )
    saved = [
        dict(
            tournament_id=TID,
            discord_user_id="1",
            slot_id="mon-a",
            created_at_utc="created",
            updated_at_utc="old",
            notes="",
        )
    ]
    repo = MemoryRepository([participant], saved)
    svc = make_service(repo)
    repo.fail_core = True
    with pytest.raises(RuntimeError):
        run(svc.update_availability("1", "Europe/London", ["mon-a", "tue-a", "wed-a"]))
    assert repo.p == [participant] and repo.a == saved
    run(svc.update_availability("1", "Europe/London", ["mon-a", "tue-a", "wed-a"]))
    assert {
        key: repo.p[0][key]
        for key in (
            "signed_up_at_utc",
            "confirmed_at_utc",
            "clan_tag_at_signup",
            "status",
        )
    } == {
        key: participant[key]
        for key in (
            "signed_up_at_utc",
            "confirmed_at_utc",
            "clan_tag_at_signup",
            "status",
        )
    }
    assert repo.audit[0]["event_type"] == "availability_updated"
    assert repo.audit[0]["created_at_utc"] == "2026-03-01T12:00:00Z"


def test_failed_new_core_write_has_no_partial_state():
    repo = MemoryRepository()
    repo.fail_core = True
    with pytest.raises(RuntimeError):
        run(
            make_service(repo).register(
                "1", "P", ["10"], "UTC", ["mon-a", "tue-a", "wed-a"]
            )
        )
    assert repo.p == [] and repo.a == []


def test_failed_reregistration_restores_exact_withdrawn_state():
    old_p, old_a = core_rows(status="withdrawn", slots=("old-a", "old-b"))
    repo = MemoryRepository(old_p, old_a)
    repo.fail_core = True
    with pytest.raises(RuntimeError, match="write failed"):
        run(
            make_service(repo).register(
                "1", "New", ["10"], "UTC", ["mon-a", "tue-a", "wed-a"]
            )
        )
    assert repo.p == old_p
    assert repo.a == old_a


def test_audit_failure_keeps_core_and_logs(caplog):
    repo = MemoryRepository()
    repo.fail_audit = True
    run(
        make_service(repo).register(
            "1", "P", ["10"], "UTC", ["mon-a", "tue-a", "wed-a"]
        )
    )
    assert repo.p[0]["status"] == "confirmed"
    assert (
        "tournament=LA-1" in caplog.text
        and "event=registration_confirmed" in caplog.text
    )


class FakeSpreadsheet:
    def __init__(self, state, failure="before"):
        self.state = deepcopy(state)
        self.failure = failure
        self.calls = 0

    def values_batch_update(self, body):
        self.calls += 1
        if self.calls == 1 and self.failure == "before":
            raise RuntimeError("participant write failed")
        for index, item in enumerate(body["data"]):
            self.state[item["range"].split("!", 1)[0].strip("'")] = deepcopy(
                item["values"]
            )
            if (
                self.calls == 1
                and self.failure in {"availability", "rollback"}
                and index == 0
            ):
                raise RuntimeError("availability write failed")
        if self.calls == 2 and self.failure == "rollback":
            raise RuntimeError("rollback failed")


def repository_with_spreadsheet(monkeypatch, state, failure):
    spreadsheet = FakeSpreadsheet(state, failure)
    worksheet = type("Worksheet", (), {"spreadsheet": spreadsheet})()

    async def get_worksheet(*_):
        return worksheet

    async def call(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(repository, "aget_worksheet", get_worksheet)
    monkeypatch.setattr(repository, "acall_with_backoff", call)
    repo = repository.LiveArenaRepository("sheet")
    repo.config = {
        "PARTICIPANTS_TAB": "PEOPLE",
        "PARTICIPANT_AVAILABILITY_TAB": "TIMES",
    }
    return repo, spreadsheet


def core_rows(status="confirmed", slots=("old",)):
    participant = [dict(tournament_id=TID, discord_user_id="1", status=status)]
    availability = [
        dict(tournament_id=TID, discord_user_id="1", slot_id=slot) for slot in slots
    ]
    return participant, availability


@pytest.mark.parametrize("failure", ["before", "availability"])
def test_repository_core_failure_restores_exact_prior_tables(monkeypatch, failure):
    old_p, old_a = core_rows(status="withdrawn", slots=("old-a", "old-b"))
    new_p, new_a = core_rows(status="confirmed", slots=("new-a", "new-b", "new-c"))
    state = {"PEOPLE": [["prior participant"]], "TIMES": [["prior availability"]]}
    repo, spreadsheet = repository_with_spreadsheet(monkeypatch, state, failure)

    with pytest.raises(RuntimeError, match="write failed"):
        run(
            repo.persist_core_state(
                new_p,
                new_a,
                previous_participants=old_p,
                previous_availability=old_a,
            )
        )

    rollback = repo._batch_body(
        (
            ("PARTICIPANTS_TAB", repository.PARTICIPANT_HEADERS, new_p, old_p),
            (
                "PARTICIPANT_AVAILABILITY_TAB",
                repository.PARTICIPANT_AVAILABILITY_HEADERS,
                new_a,
                old_a,
            ),
        ),
        use_previous=True,
    )
    assert spreadsheet.state == {
        item["range"].split("!", 1)[0].strip("'"): item["values"]
        for item in rollback["data"]
    }


def test_repository_new_registration_failure_leaves_no_partial_core(monkeypatch):
    new_p, new_a = core_rows(slots=("one", "two", "three"))
    repo, spreadsheet = repository_with_spreadsheet(
        monkeypatch, {"PEOPLE": [], "TIMES": []}, "availability"
    )
    with pytest.raises(RuntimeError, match="availability write failed"):
        run(
            repo.persist_core_state(
                new_p,
                new_a,
                previous_participants=[],
                previous_availability=[],
            )
        )
    assert spreadsheet.state["PEOPLE"] == [[""] * len(repository.PARTICIPANT_HEADERS)]
    assert spreadsheet.state["TIMES"] == [
        [""] * len(repository.PARTICIPANT_AVAILABILITY_HEADERS)
        for _ in range(len(new_a))
    ]


def test_repository_failed_withdrawal_preserves_confirmed_participant(monkeypatch):
    old_p, _ = core_rows()
    new_p, _ = core_rows(status="withdrawn")
    repo, spreadsheet = repository_with_spreadsheet(
        monkeypatch, {"PEOPLE": [["prior"]]}, "before"
    )
    with pytest.raises(RuntimeError, match="participant write failed"):
        run(repo.persist_participants(new_p, previous_participants=old_p))
    assert spreadsheet.state["PEOPLE"][0][5] == "confirmed"


def test_repository_compensation_failure_is_surfaced(monkeypatch):
    old_p, old_a = core_rows()
    new_p, new_a = core_rows(status="withdrawn", slots=("new",))
    repo, _ = repository_with_spreadsheet(
        monkeypatch, {"PEOPLE": [], "TIMES": []}, "rollback"
    )
    with pytest.raises(repository.CoreStatePersistenceError) as caught:
        run(
            repo.persist_core_state(
                new_p,
                new_a,
                previous_participants=old_p,
                previous_availability=old_a,
            )
        )
    assert "compensation both failed" in str(caught.value)
    assert isinstance(caught.value.write_error, RuntimeError)
    assert isinstance(caught.value.rollback_error, RuntimeError)
