import asyncio
from typing import Any

import pytest

from modules.recruitment import availability
from shared.sheets import reservations


class StubWorksheet:
    def __init__(self):
        self.updates: list[tuple[str, list[list[Any]], dict[str, Any]]] = []

    def update(self, range_name: str, values: list[list[Any]], **kwargs: Any) -> None:
        self.updates.append((range_name, values, kwargs))
        return None


def test_recompute_clan_availability_updates_sheet(monkeypatch):
    worksheet = StubWorksheet()

    async def fake_get_active_reservations(clan_tag: str):
        row = reservations.ReservationRow(
            row_number=2,
            thread_id="t1",
            ticket_user_id=1,
            recruiter_id=123,
            clan_tag=clan_tag,
            reserved_until=None,
            created_at=None,
            status="active",
            notes="",
            username_snapshot="Alice",
            raw=[],
        )
        return [row]

    async def fake_resolve_names(res_rows, *, guild=None, resolver=None):
        return ["Alice"]

    monkeypatch.setattr(
        reservations, "get_active_reservations_for_clan", fake_get_active_reservations
    )
    monkeypatch.setattr(reservations, "resolve_reservation_names", fake_resolve_names)

    monkeypatch.setattr(
        availability.recruitment,
        "find_clan_row",
        lambda tag: (
            7,
            [
                "",  # A
                "Clan Name",  # B
                "#AAA",  # C tag column
                "",  # D
                "3",  # E manual open spots
            ]
            + [""] * 30
        ),
    )

    monkeypatch.setattr(availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet")
    monkeypatch.setattr(availability.recruitment, "get_clans_tab_name", lambda: "bot_info")
    monkeypatch.setattr(
        availability.recruitment,
        "get_clan_header_map",
        lambda: {
            "manual_open_spots": 4,
            "open_spots": 31,
            "manual_open_spots_seen": 35,
            "inactives": 32,
            "reservation_count": 33,
            "reservation_summary": 34,
        },
    )
    async def fake_aget(sheet_id: str, tab_name: str):
        assert sheet_id == "sheet"
        assert tab_name == "bot_info"
        return worksheet

    async def fake_acall(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(availability.async_core, "aget_worksheet", fake_aget)
    monkeypatch.setattr(availability.async_core, "acall_with_backoff", fake_acall)

    updated_rows = {}

    def capture_update(sheet_row: int, row_values):
        updated_rows["row"] = list(row_values)
        updated_rows["sheet_row"] = sheet_row

    monkeypatch.setattr(availability.recruitment, "update_cached_clan_row", capture_update)

    asyncio.run(availability.recompute_clan_availability("#AAA"))

    assert worksheet.updates == [
        ("AF7", [[2]], {"value_input_option": "RAW"}),
        ("AJ7", [[3]], {"value_input_option": "RAW"}),
        ("AG7", [[""]], {"value_input_option": "RAW"}),
        ("AH7", [[1]], {"value_input_option": "RAW"}),
        ("AI7", [["1 -> Alice"]], {"value_input_option": "RAW"}),
    ]
    assert updated_rows["sheet_row"] == 7
    assert updated_rows["row"][31] == "2"
    assert updated_rows["row"][33] == "1"
    assert updated_rows["row"][34] == "1 -> Alice"
    assert updated_rows["row"][35] == "3"


def test_recompute_clan_availability_zero_reservations(monkeypatch):
    worksheet = StubWorksheet()

    async def fake_get_active_reservations(clan_tag: str):
        return []

    async def fake_resolve_names(res_rows, *, guild=None, resolver=None):
        return []

    monkeypatch.setattr(
        reservations, "get_active_reservations_for_clan", fake_get_active_reservations
    )
    monkeypatch.setattr(reservations, "resolve_reservation_names", fake_resolve_names)

    base_row = ["", "Clan", "#BBB", "", "5"] + [""] * 30
    monkeypatch.setattr(
        availability.recruitment,
        "find_clan_row",
        lambda tag: (9, list(base_row)),
    )
    monkeypatch.setattr(availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet")
    monkeypatch.setattr(availability.recruitment, "get_clans_tab_name", lambda: "bot_info")
    monkeypatch.setattr(
        availability.recruitment,
        "get_clan_header_map",
        lambda: {
            "manual_open_spots": 4,
            "open_spots": 31,
            "manual_open_spots_seen": 35,
            "inactives": 32,
            "reservation_count": 33,
            "reservation_summary": 34,
        },
    )

    async def fake_aget(sheet_id: str, tab_name: str):
        return worksheet

    async def fake_acall(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(availability.async_core, "aget_worksheet", fake_aget)
    monkeypatch.setattr(availability.async_core, "acall_with_backoff", fake_acall)

    monkeypatch.setattr(availability.recruitment, "update_cached_clan_row", lambda *args, **kwargs: None)

    asyncio.run(availability.recompute_clan_availability("#BBB"))

    assert worksheet.updates == [
        ("AF9", [[5]], {"value_input_option": "RAW"}),
        ("AJ9", [[5]], {"value_input_option": "RAW"}),
        ("AG9", [[""]], {"value_input_option": "RAW"}),
        ("AH9", [[0]], {"value_input_option": "RAW"}),
        ("AI9", [[""]], {"value_input_option": "RAW"}),
    ]


def test_recompute_clan_availability_uses_reordered_reservation_columns(monkeypatch):
    worksheet = StubWorksheet()

    async def fake_get_active_reservations(clan_tag: str):
        return [
            reservations.ReservationRow(
                row_number=2,
                thread_id="t1",
                ticket_user_id=1,
                recruiter_id=123,
                clan_tag=clan_tag,
                reserved_until=None,
                created_at=None,
                status="active",
                notes="",
                username_snapshot="Alice",
                raw=[],
            )
        ]

    async def fake_resolve_names(_rows, *, guild=None, resolver=None):
        return ["Alice"]

    monkeypatch.setattr(reservations, "get_active_reservations_for_clan", fake_get_active_reservations)
    monkeypatch.setattr(reservations, "resolve_reservation_names", fake_resolve_names)
    row = ["", "Clan", "#EEE", "", "4"] + [""] * 40
    monkeypatch.setattr(availability.recruitment, "find_clan_row", lambda tag: (11, list(row)))
    monkeypatch.setattr(availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet")
    monkeypatch.setattr(availability.recruitment, "get_clans_tab_name", lambda: "bot_info")
    monkeypatch.setattr(
        availability.recruitment,
        "get_clan_header_map",
        lambda: {
            "manual_open_spots": 4,
            "open_spots": 20,
            "manual_open_spots_seen": 41,
            "inactives": 32,
            "reservation_count": 36,
            "reservation_summary": 25,
        },
    )

    async def fake_aget(_sheet_id: str, _tab_name: str):
        return worksheet

    async def fake_acall(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(availability.async_core, "aget_worksheet", fake_aget)
    monkeypatch.setattr(availability.async_core, "acall_with_backoff", fake_acall)
    monkeypatch.setattr(availability.recruitment, "update_cached_clan_row", lambda *args, **kwargs: None)

    asyncio.run(availability.recompute_clan_availability("#EEE"))

    assert worksheet.updates == [
        ("U11", [[3]], {"value_input_option": "RAW"}),
        ("AP11", [[4]], {"value_input_option": "RAW"}),
        ("AG11", [[""]], {"value_input_option": "RAW"}),
        ("AK11", [[1]], {"value_input_option": "RAW"}),
        ("Z11", [["1 -> Alice"]], {"value_input_option": "RAW"}),
    ]


def test_recompute_clan_availability_requires_reservation_headers(monkeypatch):
    monkeypatch.setattr(
        availability.recruitment,
        "find_clan_row",
        lambda tag: (10, ["", "Clan", "#AAA", "", "2"] + [""] * 40),
    )
    monkeypatch.setattr(
        availability.recruitment,
        "get_clan_header_map",
        lambda: {"manual_open_spots": 4, "open_spots": 31, "inactives": 32, "manual_open_spots_seen": 35},
    )

    with pytest.raises(ValueError, match="reservation_count/reservation_summary"):
        asyncio.run(availability.recompute_clan_availability("#AAA"))


def test_recompute_clan_availability_keeps_runtime_af_when_manual_seen_matches(monkeypatch):
    worksheet = StubWorksheet()

    async def fake_get_active_reservations(clan_tag: str):
        return []

    async def fake_resolve_names(_rows, *, guild=None, resolver=None):
        return []

    monkeypatch.setattr(reservations, "get_active_reservations_for_clan", fake_get_active_reservations)
    monkeypatch.setattr(reservations, "resolve_reservation_names", fake_resolve_names)
    row = ["", "Clan", "#AAA", "", "3"] + [""] * 26 + ["2", "", "", "", "3"] + [""] * 4
    monkeypatch.setattr(availability.recruitment, "find_clan_row", lambda tag: (12, list(row)))
    monkeypatch.setattr(availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet")
    monkeypatch.setattr(availability.recruitment, "get_clans_tab_name", lambda: "bot_info")
    monkeypatch.setattr(
        availability.recruitment,
        "get_clan_header_map",
        lambda: {
            "manual_open_spots": 4,
            "open_spots": 31,
            "manual_open_spots_seen": 35,
            "inactives": 32,
            "reservation_count": 33,
            "reservation_summary": 34,
        },
    )

    async def fake_aget(_sheet_id: str, _tab_name: str):
        return worksheet

    async def fake_acall(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(availability.async_core, "aget_worksheet", fake_aget)
    monkeypatch.setattr(availability.async_core, "acall_with_backoff", fake_acall)
    captured = {}
    monkeypatch.setattr(
        availability.recruitment,
        "update_cached_clan_row",
        lambda sheet_row, row_values: captured.update({"sheet_row": sheet_row, "row_values": list(row_values)}),
    )

    asyncio.run(availability.recompute_clan_availability("#AAA"))
    assert worksheet.updates[0] == ("AF12", [[2]], {"value_input_option": "RAW"})
    assert worksheet.updates[1] == ("AJ12", [[3]], {"value_input_option": "RAW"})
    assert captured["row_values"][31] == "2"
    assert captured["row_values"][35] == "3"


def test_recompute_clan_availability_rebases_runtime_af_when_manual_changes(monkeypatch):
    worksheet = StubWorksheet()

    async def fake_get_active_reservations(clan_tag: str):
        return []

    async def fake_resolve_names(_rows, *, guild=None, resolver=None):
        return []

    monkeypatch.setattr(reservations, "get_active_reservations_for_clan", fake_get_active_reservations)
    monkeypatch.setattr(reservations, "resolve_reservation_names", fake_resolve_names)
    row = ["", "Clan", "#AAA", "", "4"] + [""] * 26 + ["2", "", "", "", "3"] + [""] * 4
    monkeypatch.setattr(availability.recruitment, "find_clan_row", lambda tag: (13, list(row)))
    monkeypatch.setattr(availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet")
    monkeypatch.setattr(availability.recruitment, "get_clans_tab_name", lambda: "bot_info")
    monkeypatch.setattr(
        availability.recruitment,
        "get_clan_header_map",
        lambda: {
            "manual_open_spots": 4,
            "open_spots": 31,
            "manual_open_spots_seen": 35,
            "inactives": 32,
            "reservation_count": 33,
            "reservation_summary": 34,
        },
    )

    async def fake_aget(_sheet_id: str, _tab_name: str):
        return worksheet

    async def fake_acall(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(availability.async_core, "aget_worksheet", fake_aget)
    monkeypatch.setattr(availability.async_core, "acall_with_backoff", fake_acall)
    captured = {}
    monkeypatch.setattr(
        availability.recruitment,
        "update_cached_clan_row",
        lambda sheet_row, row_values: captured.update({"sheet_row": sheet_row, "row_values": list(row_values)}),
    )

    asyncio.run(availability.recompute_clan_availability("#AAA"))
    assert worksheet.updates[0] == ("AF13", [[4]], {"value_input_option": "RAW"})
    assert worksheet.updates[1] == ("AJ13", [[4]], {"value_input_option": "RAW"})
    assert captured["row_values"][31] == "4"
    assert captured["row_values"][35] == "4"


def test_adjust_manual_open_spots_applies_delta_to_resolved_column(monkeypatch):
    worksheet = StubWorksheet()
    row = ["", "Clan", "#CCC", "", "3"] + [""] * 15 + ["3"] + [""] * 15 + ["3"]

    monkeypatch.setattr(
        availability.recruitment,
        "find_clan_row",
        lambda tag: (12, list(row)),
    )
    monkeypatch.setattr(
        availability.recruitment,
        "get_clan_header_map",
        lambda: {"manual_open_spots": 4, "open_spots": 20, "manual_open_spots_seen": 36},
    )
    monkeypatch.setattr(availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet")
    monkeypatch.setattr(availability.recruitment, "get_clans_tab_name", lambda: "bot_info")

    async def fake_aget(_sheet_id: str, _tab_name: str):
        return worksheet

    async def fake_acall(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(availability.async_core, "aget_worksheet", fake_aget)
    monkeypatch.setattr(availability.async_core, "acall_with_backoff", fake_acall)

    updated_rows = {}
    monkeypatch.setattr(
        availability.recruitment,
        "update_cached_clan_row",
        lambda sheet_row, row_values: updated_rows.update(
            {"sheet_row": sheet_row, "row_values": list(row_values)}
        ),
    )

    new_value = asyncio.run(availability.adjust_manual_open_spots("#CCC", -1))

    assert new_value == 2
    assert worksheet.updates == [
        ("U12", [["2"]], {"value_input_option": "RAW"}),
        ("AK12", [["3"]], {"value_input_option": "RAW"}),
    ]
    assert updated_rows["sheet_row"] == 12
    assert updated_rows["row_values"][20] == "2"
    assert updated_rows["row_values"][36] == "3"


def test_adjust_manual_open_spots_requires_open_spots_header(monkeypatch):
    monkeypatch.setattr(
        availability.recruitment,
        "find_clan_row",
        lambda tag: (12, ["", "Clan", "#CCC", "", "3"] + [""] * 30),
    )
    monkeypatch.setattr(availability.recruitment, "get_clan_header_map", lambda: {"manual_open_spots": 4, "open_spots": 20})

    with pytest.raises(ValueError, match="manual_open_spots_seen"):
        asyncio.run(availability.adjust_manual_open_spots("#CCC", -1))


def test_adjust_manual_open_spots_rebases_when_manual_changes(monkeypatch):
    worksheet = StubWorksheet()
    row = ["", "Clan", "#DDD", "", "4"] + [""] * 15 + ["2"] + [""] * 16 + ["3"]
    monkeypatch.setattr(availability.recruitment, "find_clan_row", lambda tag: (15, list(row)))
    monkeypatch.setattr(
        availability.recruitment,
        "get_clan_header_map",
        lambda: {"manual_open_spots": 4, "open_spots": 20, "manual_open_spots_seen": 36},
    )
    monkeypatch.setattr(availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet")
    monkeypatch.setattr(availability.recruitment, "get_clans_tab_name", lambda: "bot_info")

    async def fake_aget(_sheet_id: str, _tab_name: str):
        return worksheet

    async def fake_acall(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(availability.async_core, "aget_worksheet", fake_aget)
    monkeypatch.setattr(availability.async_core, "acall_with_backoff", fake_acall)
    monkeypatch.setattr(availability.recruitment, "update_cached_clan_row", lambda *args, **kwargs: None)

    new_value = asyncio.run(availability.adjust_manual_open_spots("#DDD", -1))
    assert new_value == 3
    assert worksheet.updates == [
        ("U15", [["3"]], {"value_input_option": "RAW"}),
        ("AK15", [["4"]], {"value_input_option": "RAW"}),
    ]


def test_adjust_manual_open_spots_rebase_without_delta(monkeypatch):
    worksheet = StubWorksheet()
    row = ["", "Clan", "#DDD", "", "4"] + [""] * 15 + ["2"] + [""] * 16 + ["3"]
    monkeypatch.setattr(availability.recruitment, "find_clan_row", lambda tag: (15, list(row)))
    monkeypatch.setattr(
        availability.recruitment,
        "get_clan_header_map",
        lambda: {"manual_open_spots": 4, "open_spots": 20, "manual_open_spots_seen": 36},
    )
    monkeypatch.setattr(availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet")
    monkeypatch.setattr(availability.recruitment, "get_clans_tab_name", lambda: "bot_info")
    async def fake_aget(_sheet_id: str, _tab_name: str):
        return worksheet
    async def fake_acall(func, *args, **kwargs):
        return func(*args, **kwargs)
    monkeypatch.setattr(availability.async_core, "aget_worksheet", fake_aget)
    monkeypatch.setattr(availability.async_core, "acall_with_backoff", fake_acall)
    monkeypatch.setattr(availability.recruitment, "update_cached_clan_row", lambda *args, **kwargs: None)

    new_value = asyncio.run(availability.adjust_manual_open_spots("#DDD", 0))
    assert new_value == 4
    assert worksheet.updates == [
        ("U15", [["4"]], {"value_input_option": "RAW"}),
        ("AK15", [["4"]], {"value_input_option": "RAW"}),
    ]
