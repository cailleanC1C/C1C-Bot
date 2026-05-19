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
        (
            "AF7:AI7",
            [[2, "", 1, "1 -> Alice"]],
            {"value_input_option": "RAW"},
        )
    ]
    assert updated_rows["sheet_row"] == 7
    assert updated_rows["row"][31] == "2"
    assert updated_rows["row"][33] == "1"
    assert updated_rows["row"][34] == "1 -> Alice"


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

    async def fake_aget(sheet_id: str, tab_name: str):
        return worksheet

    async def fake_acall(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(availability.async_core, "aget_worksheet", fake_aget)
    monkeypatch.setattr(availability.async_core, "acall_with_backoff", fake_acall)

    monkeypatch.setattr(availability.recruitment, "update_cached_clan_row", lambda *args, **kwargs: None)

    asyncio.run(availability.recompute_clan_availability("#BBB"))

    assert worksheet.updates == [
        (
            "AF9:AI9",
            [[5, "", 0, ""]],
            {"value_input_option": "RAW"},
        )
    ]


def test_adjust_manual_open_spots_applies_delta_to_resolved_column(monkeypatch):
    worksheet = StubWorksheet()
    row = ["", "Clan", "#CCC", "", "legacy", "", "3"] + [""] * 30

    monkeypatch.setattr(
        availability.recruitment,
        "find_clan_row",
        lambda tag: (12, list(row)),
    )
    monkeypatch.setattr(
        availability.recruitment,
        "get_clan_header_map",
        lambda: {"open_spots": 6},
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
    assert worksheet.updates == [("G12", [["2"]], {"value_input_option": "RAW"})]
    assert updated_rows["sheet_row"] == 12
    assert updated_rows["row_values"][6] == "2"


def test_adjust_manual_open_spots_requires_open_spots_header(monkeypatch):
    monkeypatch.setattr(
        availability.recruitment,
        "find_clan_row",
        lambda tag: (12, ["", "Clan", "#CCC", "", "3"] + [""] * 30),
    )
    monkeypatch.setattr(availability.recruitment, "get_clan_header_map", lambda: {})

    with pytest.raises(ValueError, match="open_spots"):
        asyncio.run(availability.adjust_manual_open_spots("#CCC", -1))
