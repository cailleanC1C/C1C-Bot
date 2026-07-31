from decimal import Decimal

import pytest

from modules.onboarding.controllers.welcome_controller import validate_answer
from modules.onboarding.score_input import ScoreInputError, parse_score


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("40b", "40B"),
        ("40 B", "40B"),
        ("40bn", "40B"),
        ("40 bil", "40B"),
        ("40 bill", "40B"),
        ("40 billion", "40B"),
        ("40 BILLION", "40B"),
        ("40 BiLl", "40B"),
        ("40.5b", "40.5B"),
        ("40,5b", "40.5B"),
        ("500K", "500K"),
        ("12.6M", "12.6M"),
        ("150K+", "150K"),
        ("40,000,000,000", "40B"),
        ("40000000000", "40B"),
        ("12600000", "12.6M"),
        ("1 thousand", "1K"),
        ("2mn", "2M"),
        ("2 mil", "2M"),
        ("2 mill", "2M"),
        ("2 million", "2M"),
        ("Over 40b", "40B"),
        ("about 40b", "40B"),
        ("around 40b", "40B"),
        ("roughly 40b", "40B"),
        ("approximately 40b", "40B"),
        ("approx 40b", "40B"),
        ("~40b", "40B"),
        ("40.500b", "40.5B"),
    ],
)
def test_parse_score_accepts_and_normalizes_supported_inputs(
    raw: str, expected: str
) -> None:
    assert parse_score(raw).compact == expected


@pytest.mark.parametrize(
    "raw",
    ["", " ", "40", "0", "0K", "-40B", "score 40B", "40B-ish", "40MM", "1,23,000"],
)
def test_parse_score_rejects_invalid_or_ambiguous_inputs(raw: str) -> None:
    with pytest.raises(ScoreInputError):
        parse_score(raw)


def test_parse_score_enforces_inclusive_decimal_bounds() -> None:
    assert parse_score("10K", minimum=Decimal("10000")).compact == "10K"
    assert parse_score("1M", maximum=Decimal("1000000")).compact == "1M"
    with pytest.raises(ScoreInputError):
        parse_score("9999", minimum=Decimal("10000"))
    with pytest.raises(ScoreInputError):
        parse_score("1.1M", maximum=Decimal("1000000"))


def test_validate_answer_dispatches_score_and_bounds() -> None:
    assert validate_answer({"validate": "score"}, "Over 40b") == (True, "40B", None)
    assert validate_answer({"validate": "score:min=10000,max=1000000"}, "10K") == (
        True,
        "10K",
        None,
    )
    assert validate_answer({"validate": "score:min=10000,max=1000000"}, "1M") == (
        True,
        "1M",
        None,
    )
    assert (
        validate_answer({"validate": "score:min=10000,max=1000000"}, "9999")[0] is False
    )
    assert (
        validate_answer({"validate": "score:min=10000,max=1000000"}, "1.1M")[0] is False
    )


def test_validate_answer_leaves_regex_and_unrelated_numbers_unchanged() -> None:
    player_power = {"type": "number", "validate": r"regex:^[0-9]+(\.[0-9]{1,2})?[Mm]?$"}
    assert validate_answer(player_power, "40M") == (True, "40M", None)
    assert validate_answer(player_power, "Over 40M")[0] is False
    assert validate_answer(player_power, "40,5M")[0] is False

    unrelated = {"type": "number", "validate": "none"}
    assert validate_answer(unrelated, "40") == (True, "40", None)
    assert validate_answer(unrelated, "not a score") == (True, "not a score", None)
