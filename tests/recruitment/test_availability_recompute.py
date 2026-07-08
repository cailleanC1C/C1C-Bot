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


@pytest.fixture(autouse=True)
def _default_recruitment_config(monkeypatch):
    monkeypatch.setattr(
        availability.recruitment, "get_clans_tab_name", lambda: "bot_info"
    )
    monkeypatch.setattr(
        availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet"
    )
    header = [""] * 42

    config_values = {
        "clans_header_manual_open_spots": "Manual open spots",
        "clans_header_open_spots": "Effective open spots",
        "clans_header_inactives": "Inactives",
        "clans_header_reservation_count": "Reservation count",
        "clans_header_reservation_summary": "Reservation summary",
        "clans_header_manual_open_spots_seen": "Manual open spots seen",
        "clans_header_clan_tag": "Clan Tag",
    }
    monkeypatch.setattr(
        availability.recruitment,
        "get_config_value",
        lambda key, default=None: config_values.get(key, default),
    )
    header[4] = "Manual open spots"
    header[20] = "Effective open spots"
    header[31] = "Effective open spots"
    header[32] = "Inactives"
    header[33] = "Reservation count"
    header[34] = "Reservation summary"
    header[35] = "Manual open spots seen"
    header[36] = "Clan Tag"
    monkeypatch.setattr(availability.recruitment, "get_clan_header_row", lambda: header)


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
        lambda tag, *, force=False: (
            7,
            [
                "",  # A
                "Clan Name",  # B
                "#AAA",  # C tag column
                "",  # D
                "3",  # E manual open spots
            ]
            + [""] * 30,
        ),
    )

    monkeypatch.setattr(
        availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet"
    )
    monkeypatch.setattr(
        availability.recruitment, "get_clans_tab_name", lambda: "bot_info"
    )
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

    monkeypatch.setattr(
        availability.recruitment, "update_cached_clan_row", capture_update
    )

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
        lambda tag, *, force=False: (9, list(base_row)),
    )
    monkeypatch.setattr(
        availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet"
    )
    monkeypatch.setattr(
        availability.recruitment, "get_clans_tab_name", lambda: "bot_info"
    )
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

    monkeypatch.setattr(
        availability.recruitment, "update_cached_clan_row", lambda *args, **kwargs: None
    )

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

    monkeypatch.setattr(
        reservations, "get_active_reservations_for_clan", fake_get_active_reservations
    )
    monkeypatch.setattr(reservations, "resolve_reservation_names", fake_resolve_names)
    row = ["", "Clan", "#EEE", "", "4"] + [""] * 40
    monkeypatch.setattr(
        availability.recruitment,
        "find_clan_row",
        lambda tag, *, force=False: (11, list(row)),
    )
    monkeypatch.setattr(
        availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet"
    )
    monkeypatch.setattr(
        availability.recruitment, "get_clans_tab_name", lambda: "bot_info"
    )
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
    monkeypatch.setattr(
        availability.recruitment, "update_cached_clan_row", lambda *args, **kwargs: None
    )

    asyncio.run(availability.recompute_clan_availability("#EEE"))

    assert worksheet.updates == [
        ("AF11", [[3]], {"value_input_option": "RAW"}),
        ("AJ11", [[4]], {"value_input_option": "RAW"}),
        ("AG11", [[""]], {"value_input_option": "RAW"}),
        ("AH11", [[1]], {"value_input_option": "RAW"}),
        ("AI11", [["1 -> Alice"]], {"value_input_option": "RAW"}),
    ]


def test_recompute_clan_availability_requires_reservation_headers(monkeypatch):
    monkeypatch.setattr(
        availability.recruitment,
        "find_clan_row",
        lambda tag, *, force=False: (10, ["", "Clan", "#AAA", "", "2"] + [""] * 40),
    )
    monkeypatch.setattr(
        availability.recruitment,
        "get_clan_header_map",
        lambda: {
            "manual_open_spots": 4,
            "open_spots": 31,
            "inactives": 32,
            "manual_open_spots_seen": 35,
        },
    )

    monkeypatch.setattr(
        availability.recruitment,
        "get_config_value",
        lambda key, default=None: (
            None
            if key == "clans_header_reservation_count"
            else {
                "clans_header_manual_open_spots": "Manual open spots",
                "clans_header_open_spots": "Effective open spots",
                "clans_header_inactives": "Inactives",
                "clans_header_reservation_summary": "Reservation summary",
                "clans_header_manual_open_spots_seen": "Manual open spots seen",
                "clans_header_clan_tag": "Clan Tag",
            }.get(key, default)
        ),
    )
    with pytest.raises(ValueError, match="clans_header_reservation_count"):
        asyncio.run(availability.recompute_clan_availability("#AAA"))


def test_recompute_clan_availability_keeps_runtime_af_when_manual_seen_matches(
    monkeypatch,
):
    worksheet = StubWorksheet()

    async def fake_get_active_reservations(clan_tag: str):
        return []

    async def fake_resolve_names(_rows, *, guild=None, resolver=None):
        return []

    monkeypatch.setattr(
        reservations, "get_active_reservations_for_clan", fake_get_active_reservations
    )
    monkeypatch.setattr(reservations, "resolve_reservation_names", fake_resolve_names)
    row = ["", "Clan", "#AAA", "", "3"] + [""] * 26 + ["2", "", "", "", "3"] + [""] * 4
    monkeypatch.setattr(
        availability.recruitment,
        "find_clan_row",
        lambda tag, *, force=False: (12, list(row)),
    )
    monkeypatch.setattr(
        availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet"
    )
    monkeypatch.setattr(
        availability.recruitment, "get_clans_tab_name", lambda: "bot_info"
    )
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
        lambda sheet_row, row_values: captured.update(
            {"sheet_row": sheet_row, "row_values": list(row_values)}
        ),
    )

    asyncio.run(availability.recompute_clan_availability("#AAA"))
    assert worksheet.updates[0] == ("AF12", [[2]], {"value_input_option": "RAW"})
    assert worksheet.updates[1] == ("AJ12", [[3]], {"value_input_option": "RAW"})
    assert captured["row_values"][31] == "2"
    assert captured["row_values"][35] == "3"


def test_recompute_clan_availability_rebases_runtime_af_when_manual_changes(
    monkeypatch,
):
    worksheet = StubWorksheet()

    async def fake_get_active_reservations(clan_tag: str):
        return []

    async def fake_resolve_names(_rows, *, guild=None, resolver=None):
        return []

    monkeypatch.setattr(
        reservations, "get_active_reservations_for_clan", fake_get_active_reservations
    )
    monkeypatch.setattr(reservations, "resolve_reservation_names", fake_resolve_names)
    row = ["", "Clan", "#AAA", "", "4"] + [""] * 26 + ["2", "", "", "", "3"] + [""] * 4
    monkeypatch.setattr(
        availability.recruitment,
        "find_clan_row",
        lambda tag, *, force=False: (13, list(row)),
    )
    monkeypatch.setattr(
        availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet"
    )
    monkeypatch.setattr(
        availability.recruitment, "get_clans_tab_name", lambda: "bot_info"
    )
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
        lambda sheet_row, row_values: captured.update(
            {"sheet_row": sheet_row, "row_values": list(row_values)}
        ),
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
        lambda tag, *, force=False: (12, list(row)),
    )
    monkeypatch.setattr(
        availability.recruitment,
        "get_clan_header_map",
        lambda: {
            "manual_open_spots": 4,
            "open_spots": 20,
            "manual_open_spots_seen": 36,
        },
    )
    monkeypatch.setattr(
        availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet"
    )
    monkeypatch.setattr(
        availability.recruitment, "get_clans_tab_name", lambda: "bot_info"
    )
    header = [""] * 42
    header[2] = "Clan Tag"
    header[4] = "Manual open spots"
    header[20] = "Effective open spots"
    header[32] = "Inactives"
    header[33] = "Reservation count"
    header[34] = "Reservation summary"
    header[36] = "Manual open spots seen"
    monkeypatch.setattr(availability.recruitment, "get_clan_header_row", lambda: header)

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
        lambda tag, *, force=False: (12, ["", "Clan", "#CCC", "", "3"] + [""] * 30),
    )
    monkeypatch.setattr(
        availability.recruitment,
        "get_clan_header_map",
        lambda: {"manual_open_spots": 4, "open_spots": 20},
    )

    monkeypatch.setattr(
        availability.recruitment,
        "get_config_value",
        lambda key, default=None: (
            None
            if key == "clans_header_manual_open_spots_seen"
            else {
                "clans_header_manual_open_spots": "Manual open spots",
                "clans_header_open_spots": "Effective open spots",
                "clans_header_inactives": "Inactives",
                "clans_header_reservation_count": "Reservation count",
                "clans_header_reservation_summary": "Reservation summary",
                "clans_header_clan_tag": "Clan Tag",
            }.get(key, default)
        ),
    )
    with pytest.raises(ValueError, match="clans_header_manual_open_spots_seen"):
        asyncio.run(availability.adjust_manual_open_spots("#CCC", -1))


def test_adjust_manual_open_spots_rebases_when_manual_changes(monkeypatch):
    worksheet = StubWorksheet()
    row = ["", "Clan", "#DDD", "", "4"] + [""] * 15 + ["2"] + [""] * 15 + ["3"]
    monkeypatch.setattr(
        availability.recruitment,
        "find_clan_row",
        lambda tag, *, force=False: (15, list(row)),
    )
    monkeypatch.setattr(
        availability.recruitment,
        "get_clan_header_map",
        lambda: {
            "manual_open_spots": 4,
            "open_spots": 20,
            "manual_open_spots_seen": 36,
        },
    )
    monkeypatch.setattr(
        availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet"
    )
    monkeypatch.setattr(
        availability.recruitment, "get_clans_tab_name", lambda: "bot_info"
    )

    async def fake_aget(_sheet_id: str, _tab_name: str):
        return worksheet

    async def fake_acall(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(availability.async_core, "aget_worksheet", fake_aget)
    monkeypatch.setattr(availability.async_core, "acall_with_backoff", fake_acall)
    monkeypatch.setattr(
        availability.recruitment, "update_cached_clan_row", lambda *args, **kwargs: None
    )

    new_value = asyncio.run(availability.adjust_manual_open_spots("#DDD", -1))
    assert new_value == 3
    assert worksheet.updates == [
        ("AF15", [["3"]], {"value_input_option": "RAW"}),
        ("AJ15", [["4"]], {"value_input_option": "RAW"}),
    ]


def test_adjust_manual_open_spots_rebase_without_delta(monkeypatch):
    worksheet = StubWorksheet()
    row = ["", "Clan", "#DDD", "", "4"] + [""] * 15 + ["2"] + [""] * 15 + ["3"]
    monkeypatch.setattr(
        availability.recruitment,
        "find_clan_row",
        lambda tag, *, force=False: (15, list(row)),
    )
    monkeypatch.setattr(
        availability.recruitment,
        "get_clan_header_map",
        lambda: {
            "manual_open_spots": 4,
            "open_spots": 20,
            "manual_open_spots_seen": 36,
        },
    )
    monkeypatch.setattr(
        availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet"
    )
    monkeypatch.setattr(
        availability.recruitment, "get_clans_tab_name", lambda: "bot_info"
    )

    async def fake_aget(_sheet_id: str, _tab_name: str):
        return worksheet

    async def fake_acall(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(availability.async_core, "aget_worksheet", fake_aget)
    monkeypatch.setattr(availability.async_core, "acall_with_backoff", fake_acall)
    monkeypatch.setattr(
        availability.recruitment, "update_cached_clan_row", lambda *args, **kwargs: None
    )

    new_value = asyncio.run(availability.adjust_manual_open_spots("#DDD", 0))
    assert new_value == 4
    assert worksheet.updates == [
        ("AF15", [["4"]], {"value_input_option": "RAW"}),
        ("AJ15", [["4"]], {"value_input_option": "RAW"}),
    ]


def _patch_adjust_common(monkeypatch, *, row, header_map=None, worksheet=None):
    worksheet = worksheet or StubWorksheet()
    monkeypatch.setattr(
        availability.recruitment,
        "find_clan_row",
        lambda tag, *, force=False: (12, list(row)) if tag != "#MISS" else None,
    )
    monkeypatch.setattr(
        availability.recruitment,
        "get_clan_header_map",
        lambda: header_map
        or {"manual_open_spots": 4, "open_spots": 20, "manual_open_spots_seen": 36},
    )
    header = [""] * 42
    header[2] = "Clan Tag"
    header[4] = "Manual open spots"
    header[20] = "Effective open spots"
    header[32] = "Inactives"
    header[33] = "Reservation count"
    header[34] = "Reservation summary"
    header[36] = "Manual open spots seen"
    monkeypatch.setattr(availability.recruitment, "get_clan_header_row", lambda: header)
    monkeypatch.setattr(
        availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet"
    )
    monkeypatch.setattr(
        availability.recruitment, "get_clans_tab_name", lambda: "bot_info"
    )

    async def fake_aget(_sheet_id: str, _tab_name: str):
        return worksheet

    async def fake_acall(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(availability.async_core, "aget_worksheet", fake_aget)
    monkeypatch.setattr(availability.async_core, "acall_with_backoff", fake_acall)
    monkeypatch.setattr(
        availability.recruitment,
        "update_cached_clan_row",
        lambda *_args, **_kwargs: None,
    )
    return worksheet


def test_adjust_manual_open_spots_missing_bot_info_row_logs_context(
    monkeypatch, caplog
):
    _patch_adjust_common(monkeypatch, row=[""] * 37)
    with pytest.raises(ValueError, match="Unknown clan tag"):
        asyncio.run(availability.preflight_manual_open_spots_adjustment("#MISS", -1))
    assert "bot_info availability header diagnostics" in caplog.text
    assert any(
        getattr(r, "reason", None) == "bot_info_row_not_found" for r in caplog.records
    )
    assert any(
        getattr(r, "configured_tab_name", None) == "bot_info" for r in caplog.records
    )


def test_adjust_manual_open_spots_missing_configured_manual_column_logs_context(
    monkeypatch, caplog
):
    row = [""] * 37
    row[20] = "3"
    row[36] = "3"
    _patch_adjust_common(monkeypatch, row=row)
    monkeypatch.setattr(
        availability.recruitment,
        "get_config_value",
        lambda key, default=None: (
            None
            if key == "clans_header_manual_open_spots"
            else {
                "clans_header_open_spots": "Effective open spots",
                "clans_header_inactives": "Inactives",
                "clans_header_reservation_count": "Reservation count",
                "clans_header_reservation_summary": "Reservation summary",
                "clans_header_manual_open_spots_seen": "Manual open spots seen",
                "clans_header_clan_tag": "Clan Tag",
            }.get(key, default)
        ),
    )
    with pytest.raises(ValueError, match="clans_header_manual_open_spots"):
        asyncio.run(availability.preflight_manual_open_spots_adjustment("#CCC", -1))
    assert any(
        getattr(r, "missing_config_key", None) == "clans_header_manual_open_spots"
        for r in caplog.records
    )


def test_adjust_manual_open_spots_non_numeric_manual_value_logs_context(
    monkeypatch, caplog
):
    row = [""] * 37
    row[4] = "TBD"
    row[20] = "3"
    row[36] = "3"
    _patch_adjust_common(monkeypatch, row=row)
    with pytest.raises(ValueError, match="non_numeric_manual_open_spots_value"):
        asyncio.run(availability.preflight_manual_open_spots_adjustment("#CCC", -1))
    assert any(getattr(r, "raw_cell_value", None) == "TBD" for r in caplog.records)
    assert any(getattr(r, "attempted_delta", None) == -1 for r in caplog.records)


def test_adjust_manual_open_spots_sheet_update_exception_logs_range(
    monkeypatch, caplog
):
    class FailingWorksheet(StubWorksheet):
        def update(self, range_name, values, **kwargs):  # type: ignore[no-untyped-def]
            raise RuntimeError(f"boom {range_name}")

    row = [""] * 37
    row[4] = "3"
    row[20] = "3"
    row[36] = "3"
    _patch_adjust_common(monkeypatch, row=row, worksheet=FailingWorksheet())
    with pytest.raises(RuntimeError, match="boom U12"):
        asyncio.run(availability.adjust_manual_open_spots("#CCC", -1))
    assert "range=U12" in caplog.text
    assert "boom U12" in caplog.text
    assert any(getattr(r, "write_range", None) == "U12" for r in caplog.records)


def test_availability_header_config_resolves_production_columns(monkeypatch):
    header = [""] * 37
    header[4] = "manual_open_spots"
    header[31] = "open_spots"
    header[32] = "inactives"
    header[33] = "reservation_count"
    header[34] = "reservation_summary"
    header[35] = "manual_open_spots_seen"
    header[36] = "clan_tag"
    config_values = {
        f"clans_header_{field}": field for field in availability.AVAILABILITY_FIELDS
    }
    monkeypatch.setattr(
        availability.recruitment, "get_clans_tab_name", lambda: "bot_info"
    )
    monkeypatch.setattr(
        availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet"
    )
    monkeypatch.setattr(
        availability.recruitment, "get_clan_header_row", lambda force=False: header
    )
    monkeypatch.setattr(
        availability.recruitment,
        "get_config_value",
        lambda key, default=None: config_values.get(key, default),
    )

    resolved = availability._resolve_availability_headers()

    assert {
        key: availability._column_label(index)
        for key, index in resolved.header_map.items()
    } == {
        "manual_open_spots": "E",
        "open_spots": "AF",
        "inactives": "AG",
        "reservation_count": "AH",
        "reservation_summary": "AI",
        "manual_open_spots_seen": "AJ",
        "clan_tag": "AK",
    }


def test_adjust_and_recompute_accept_same_configured_row_resolver(monkeypatch):
    worksheet = StubWorksheet()
    row = [""] * 42
    row[5] = "C1CD"
    row[6] = "4"
    row[20] = "4"
    row[32] = "0"
    row[33] = "0"
    row[34] = ""
    row[36] = "4"
    header = [""] * 42
    header[5] = "Live Clan Tag"
    header[6] = "Manual open spots"
    header[20] = "Effective open spots"
    header[32] = "Inactives"
    header[33] = "Reservation count"
    header[34] = "Reservation summary"
    header[36] = "Manual open spots seen"
    config = {
        "clans_header_clan_tag": "Live Clan Tag",
        "clans_header_manual_open_spots": "Manual open spots",
        "clans_header_open_spots": "Effective open spots",
        "clans_header_inactives": "Inactives",
        "clans_header_reservation_count": "Reservation count",
        "clans_header_reservation_summary": "Reservation summary",
        "clans_header_manual_open_spots_seen": "Manual open spots seen",
    }
    monkeypatch.setattr(availability.recruitment, "get_config_value", lambda key, default=None: config.get(key, default))
    monkeypatch.setattr(availability.recruitment, "get_clan_header_row", lambda force=True: header)
    monkeypatch.setattr(availability.recruitment, "get_clans_tab_name", lambda: "bot_info")
    monkeypatch.setattr(availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet123456")
    monkeypatch.setattr(availability.recruitment, "fetch_clans", lambda force=True: [list(row)])
    monkeypatch.setattr(availability.recruitment, "update_cached_clan_row", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(availability.recruitment, "get_clan_header_map", lambda: {"clan_tag": 2})
    monkeypatch.setattr(availability.reservations, "get_active_reservations_for_clan", lambda tag: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(availability.reservations, "resolve_reservation_names", lambda *a, **k: asyncio.sleep(0, result=[]))

    async def fake_aget(_sheet_id: str, _tab_name: str):
        return worksheet

    async def fake_acall(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(availability.async_core, "aget_worksheet", fake_aget)
    monkeypatch.setattr(availability.async_core, "acall_with_backoff", fake_acall)

    calls = []

    def resolver(tag, headers):
        calls.append((tag, headers.header_map["clan_tag"]))
        return availability.resolve_availability_clan_row(tag, headers)

    assert asyncio.run(availability.adjust_manual_open_spots("C1CD", -1, find_clan_row_fn=resolver)) == 3
    asyncio.run(availability.recompute_clan_availability("C1CD", find_clan_row_fn=resolver))
    assert calls == [("C1CD", 5), ("C1CD", 5), ("C1CD", 5)]
