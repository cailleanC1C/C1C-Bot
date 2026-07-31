from __future__ import annotations

import pytest

from shared import config as shared_config
from shared.sheets import onboarding_questions


def test_normalise_records_trims_and_blanks_none() -> None:
    records = (
        {
            " Flow ": " welcome ",
            "Note": None,
            None: "ignored",
            "Spacing": "  keep  ",
        },
    )

    normalised = onboarding_questions._normalise_records(records)

    assert normalised == (
        {
            "flow": "welcome",
            "note": "",
            "spacing": "keep",
        },
    )


def test_describe_source_tails_sheet_id(monkeypatch: "pytest.MonkeyPatch") -> None:
    monkeypatch.setitem(shared_config._CONFIG, "ONBOARDING_SHEET_ID", "sheet-id-abcdef")
    monkeypatch.setitem(shared_config._CONFIG, "ONBOARDING_TAB", "Onboarding")

    metadata = onboarding_questions.describe_source()

    assert metadata["sheet"].startswith("…")
    assert metadata["sheet"].endswith("abcdef")
    assert metadata["tab"] == "Onboarding"


def test_normalise_type_maps_boolean_aliases() -> None:
    aliases = [
        "Boolean",
        "bool",
        "BOOL (Yes/No)",
        "Yes/No",
        "yes - no",
        "yes_no",
        "Y/N",
        "TrueFalse",
        "true/false",
    ]

    for alias in aliases:
        normalised, max_count = onboarding_questions._normalise_type(alias)
        assert normalised == "bool"
        assert max_count is None


def _option_pairs(
    options: tuple[onboarding_questions.Option, ...],
) -> list[tuple[str, str]]:
    return [(option.label, option.value) for option in options]


def test_parse_options_supports_numeric_range() -> None:
    options = onboarding_questions._parse_options("1-5")

    assert _option_pairs(options) == [
        ("1", "1"),
        ("2", "2"),
        ("3", "3"),
        ("4", "4"),
        ("5", "5"),
    ]


def test_parse_options_supports_en_dash_range() -> None:
    options = onboarding_questions._parse_options("1 – 3")

    assert _option_pairs(options) == [
        ("1", "1"),
        ("2", "2"),
        ("3", "3"),
    ]


def test_parse_options_supports_whitespace_separated_numbers() -> None:
    options = onboarding_questions._parse_options("1 2 3 4 5")

    assert _option_pairs(options) == [
        ("1", "1"),
        ("2", "2"),
        ("3", "3"),
        ("4", "4"),
        ("5", "5"),
    ]


def test_parse_options_supports_compact_digits() -> None:
    options = onboarding_questions._parse_options("12345")

    assert _option_pairs(options) == [
        ("1", "1"),
        ("2", "2"),
        ("3", "3"),
        ("4", "4"),
        ("5", "5"),
    ]


def test_build_questions_supports_promo_subflows() -> None:
    rows = (
        {"flow": "promo.r", "order": "1", "qid": "r1", "label": "R", "type": "short"},
        {"flow": "promo.m", "order": "2", "qid": "m1", "label": "M", "type": "short"},
        {"flow": "welcome", "order": "3", "qid": "w1", "label": "W", "type": "short"},
    )

    promo_r = onboarding_questions._build_questions("promo.r", rows)
    promo_m = onboarding_questions._build_questions("promo.m", rows)
    welcome = onboarding_questions._build_questions("welcome", rows)

    assert [question.qid for question in promo_r] == ["r1"]
    assert [question.qid for question in promo_m] == ["m1"]
    assert [question.qid for question in welcome] == ["w1"]


def test_build_questions_preserves_sheet_validation_without_qid_overrides() -> None:
    rows = (
        {
            "flow": "welcome",
            "order": "1",
            "qid": "w_hydra_clash",
            "label": "Hydra",
            "type": "number",
            "validate": "score:min=10000,max=1000000",
        },
        {
            "flow": "welcome",
            "order": "2",
            "qid": "w_chimera_clash",
            "label": "Chimera",
            "type": "number",
            "validate": "none",
        },
    )

    questions = onboarding_questions._build_questions("welcome", rows)

    assert [question.validate for question in questions] == [
        "score:min=10000,max=1000000",
        "none",
    ]


def test_schema_hash_varies_by_flow() -> None:
    rows = (
        {"flow": "promo.r", "order": "1", "qid": "r1", "label": "R", "type": "short"},
        {"flow": "promo.m", "order": "1", "qid": "m1", "label": "M", "type": "short"},
    )

    promo_r_hash = onboarding_questions._hash_payload(
        onboarding_questions._build_questions("promo.r", rows)
    )
    promo_m_hash = onboarding_questions._hash_payload(
        onboarding_questions._build_questions("promo.m", rows)
    )

    assert promo_r_hash != promo_m_hash
