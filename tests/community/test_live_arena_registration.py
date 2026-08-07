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
        self.fail_availability = False
        self.fail_audit = False

    async def initialize(self):
        pass

    async def participants(self):
        return deepcopy(self.p)

    async def availability(self):
        return deepcopy(self.a)

    async def replace_participants(self, rows):
        self.p = deepcopy(rows)

    async def replace_availability(self, rows):
        if self.fail_availability:
            self.fail_availability = False
            raise RuntimeError("write failed")
        self.a = deepcopy(rows)

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


def test_new_registration_deduplicates_slots_and_writes_exact_audit():
    repo = MemoryRepository()
    svc = make_service(repo)
    run(
        svc.register("1", "Player", ["10"], "UTC", ["mon-a", "tue-a", "wed-a", "mon-a"])
    )
    assert len(repo.p) == 1 and repo.p[0]["status"] == "confirmed" and len(repo.a) == 3
    assert repo.audit[0]["event_type"] == "registration_confirmed"
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
    repo.fail_availability = True
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


def test_failed_new_core_write_has_no_partial_state():
    repo = MemoryRepository()
    repo.fail_availability = True
    with pytest.raises(RuntimeError):
        run(
            make_service(repo).register(
                "1", "P", ["10"], "UTC", ["mon-a", "tue-a", "wed-a"]
            )
        )
    assert repo.p == [] and repo.a == []


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
