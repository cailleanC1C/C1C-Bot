import logging

from shared.sheets import recruitment


def test_process_clan_sheet_logs_header_diagnostics(caplog):
    header = [""] * 37
    header[4] = " Roster "
    header[31] = "Open Spots"
    header[32] = "Inactives"
    header[34] = "Reserved"
    header[35] = "Manual Open Spots Seen"
    header[36] = "Manual Open Spots"
    rows = [
        ["title"],
        [""],
        header,
        ["", "Clan", "TAG", "", "Open"] + [""] * 32,
    ]

    with caplog.at_level(logging.INFO, logger=recruitment.log.name):
        recruitment._process_clan_sheet(
            rows,
            now=123.0,
            tab="ConfiguredClansTab",
            sheet_id="1234567890abcdef",
        )

    diagnostic = next(
        record
        for record in caplog.records
        if "recruitment clan header diagnostics" in record.message
    )
    assert diagnostic.sheet_id_masked == "1234…cdef"
    assert diagnostic.tab == "ConfiguredClansTab"
    assert diagnostic.header_row_index == 3
    assert diagnostic.raw_header_values == {
        "E": " Roster ",
        "AF": "Open Spots",
        "AG": "Inactives",
        "AH": "",
        "AI": "Reserved",
        "AJ": "Manual Open Spots Seen",
        "AK": "Manual Open Spots",
    }
    assert diagnostic.normalized_header_values == {
        "E": "roster",
        "AF": "open spots",
        "AG": "inactives",
        "AH": "",
        "AI": "reserved",
        "AJ": "manual open spots seen",
        "AK": "manual open spots",
    }
    assert diagnostic.header_map_columns["roster"] == "E"
    assert diagnostic.header_map_columns["open_spots"] == "AF"
    assert diagnostic.header_map_columns["inactives"] == "AG"
    assert diagnostic.header_map_columns["reserved"] == "AI"
    assert diagnostic.header_map_columns["manual_open_spots_seen"] == "AJ"
    assert diagnostic.header_map_columns["manual_open_spots"] == "AK"
    assert "reservation_count" in diagnostic.missing_required_keys


def test_process_clan_sheet_maps_underscore_reservation_headers_without_missing_diagnostics(caplog):
    header = [""] * 37
    header[31] = "open_spots"
    header[32] = "inactives"
    header[33] = "reservation_count"
    header[34] = "reservation_summary"
    header[35] = "manual_open_spots_seen"
    header[36] = "clan_tag"
    rows = [
        ["title"],
        [""],
        header,
        [""] * 37,
    ]

    with caplog.at_level(logging.INFO, logger=recruitment.log.name):
        recruitment._process_clan_sheet(
            rows,
            now=456.0,
            tab="ConfiguredClansTab",
            sheet_id="1234567890abcdef",
        )

    diagnostic = next(
        record
        for record in caplog.records
        if "recruitment clan header diagnostics" in record.message
    )
    assert diagnostic.header_map_columns["open_spots"] == "AF"
    assert diagnostic.header_map_columns["inactives"] == "AG"
    assert diagnostic.header_map_columns["reserved"] == "AH"
    assert diagnostic.header_map_columns["reservation_count"] == "AH"
    assert diagnostic.header_map_columns["reservation_summary"] == "AI"
    assert diagnostic.header_map_columns["clan_tag"] == "AK"
    assert "open_spots" not in diagnostic.missing_required_keys
    assert "reserved" not in diagnostic.missing_required_keys
    assert "reservation_count" not in diagnostic.missing_required_keys
    assert "reservation_summary" not in diagnostic.missing_required_keys
    assert "roster" not in diagnostic.missing_required_keys
