import asyncio
import datetime as dt
import types

import pytest

from shared.sheets import reservations


def test_get_active_reservations_for_clan_filters_status(monkeypatch):
    header = list(reservations.RESERVATIONS_HEADERS)
    matrix = [
        header,
        ["t1", "1", "", "#AAA", "", "", "active", "", "Alice"],
        ["t2", "2", "", "#AAA", "", "", "expired", "", "Bob"],
        ["t3", "3", "", "#BBB", "", "", "ACTIVE", "", "Cara"],
    ]

    async def fake_fetch():
        return matrix

    monkeypatch.setattr(reservations, "_fetch_reservations_matrix", fake_fetch)

    rows = asyncio.run(reservations.get_active_reservations_for_clan("#AAA"))
    assert len(rows) == 1
    assert rows[0].thread_id == "t1"
    assert rows[0].ticket_user_id == 1
    assert rows[0].username_snapshot == "Alice"

    rows_other = asyncio.run(reservations.get_active_reservations_for_clan("#bbb"))
    assert len(rows_other) == 1
    assert rows_other[0].thread_id == "t3"
    assert rows_other[0].ticket_user_id == 3


def test_reservation_row_parsing_handles_dates():
    row = [
        "12345",
        "987654321",
        "555",
        "#XYZ",
        "2025-12-01",
        "2025-11-10T14:30:00Z",
        "Active",
        "hold",
        "TestUser",
    ]

    parsed = reservations.ReservationRow(
        row_number=5,
        **reservations._parse_reservation_row(row),
        raw=row,
    )

    assert parsed.row_number == 5
    assert parsed.ticket_user_id == 987654321
    assert parsed.recruiter_id == 555
    assert parsed.clan_tag == "#XYZ"
    assert parsed.reserved_until == dt.date(2025, 12, 1)
    assert parsed.created_at == dt.datetime(2025, 11, 10, 14, 30, tzinfo=dt.timezone.utc)
    assert parsed.is_active is True
    assert parsed.username_snapshot == "TestUser"


def test_resolve_reservation_names_prefers_resolver(monkeypatch):
    header = list(reservations.RESERVATIONS_HEADERS)
    matrix = [
        header,
        ["t1", "1", "", "#AAA", "", "", "active", "", "fallback"],
    ]

    async def fake_fetch():
        return matrix

    monkeypatch.setattr(reservations, "_fetch_reservations_matrix", fake_fetch)

    async def resolver(user_id: int) -> str:
        return {1: "Resolved"}.get(user_id, "")

    names = asyncio.run(
        reservations.get_active_reservation_names_for_clan("#AAA", resolver=resolver)
    )
    assert names == ["Resolved"]


def test_resolve_reservation_names_uses_guild_cache(monkeypatch):
    header = list(reservations.RESERVATIONS_HEADERS)
    matrix = [
        header,
        ["t1", "1", "", "#AAA", "", "", "active", "", ""],
    ]

    async def fake_fetch():
        return matrix

    monkeypatch.setattr(reservations, "_fetch_reservations_matrix", fake_fetch)

    member = types.SimpleNamespace(display_name="GuildName", name="Fallback")

    class DummyGuild:
        def get_member(self, member_id: int):
            return {1: member}.get(member_id)

    names = asyncio.run(
        reservations.get_active_reservation_names_for_clan("#AAA", guild=DummyGuild())
    )
    assert names == ["GuildName"]


def test_load_reservation_ledger_rejects_bad_header(monkeypatch):
    bad_header = ["Thread ID", "Ticket User ID", "Wrong"]
    matrix = [bad_header, ["a", "b", "c"]]

    async def fake_fetch():
        return matrix

    monkeypatch.setattr(reservations, "_fetch_reservations_matrix", fake_fetch)
    async def fake_tab_name():
        return "RESERVATIONS_TAB"

    monkeypatch.setattr(
        reservations.recruitment, "get_reservations_tab_name_async", fake_tab_name
    )

    with pytest.raises(reservations.ReservationSchemaError):
        asyncio.run(reservations.load_reservation_ledger())
