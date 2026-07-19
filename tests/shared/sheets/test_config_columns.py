from shared.sheets import config_service, recruitment


def test_recruitment_config_parser_uses_only_key_and_value_columns():
    rows = [
        {"Key": "CLANS_TAB", "Value": "bot_info", "description": "human docs"},
        {"Key": "REPORTS_TAB", "Value": "", "description": "Statistics"},
    ]

    parsed = recruitment._parse_config_records(rows)

    assert parsed == {"clans_tab": "bot_info"}
    assert parsed.get("reports_tab") is None


def test_recruitment_config_parser_preserves_comma_separated_role_ids():
    role_ids = (
        "1448269393082454076,1447924607842652232,"
        "1447919520751681548,1298349996374229045"
    )

    parsed = recruitment._parse_config_records(
        [{"Key": "REALMWALKER_GAME_ROLE_IDS", "Value": role_ids}]
    )

    assert parsed["realmwalker_game_role_ids"] == role_ids


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
