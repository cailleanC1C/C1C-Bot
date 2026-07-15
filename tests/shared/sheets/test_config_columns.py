from shared.sheets import config_service, recruitment


def test_recruitment_config_parser_uses_only_key_and_value_columns():
    rows = [
        {"Key": "CLANS_TAB", "Value": "bot_info", "description": "human docs"},
        {"Key": "REPORTS_TAB", "Value": "", "description": "Statistics"},
    ]

    parsed = recruitment._parse_config_records(rows)

    assert parsed == {"clans_tab": "bot_info"}
    assert parsed.get("reports_tab") is None


def test_config_service_keeps_runtime_payload_to_key_value_columns():
    rows = [
        {
            "SPEC_KEY": "LEAGUE_LEGENDARY_TAB",
            "VALUE": "Legendary",
            "description": "human docs only",
        },
        {
            "KEY": "LEAGUE_LEGENDARY_HEADER",
            "VALUE": "A1:Z3",
            "notes": "ignored too",
        },
    ]

    parsed = config_service._filter_rows(rows)

    assert parsed == {
        "LEAGUE_LEGENDARY_TAB": {
            "SPEC_KEY": "LEAGUE_LEGENDARY_TAB",
            "VALUE": "Legendary",
        },
        "LEAGUE_LEGENDARY_HEADER": {
            "KEY": "LEAGUE_LEGENDARY_HEADER",
            "VALUE": "A1:Z3",
        },
    }
