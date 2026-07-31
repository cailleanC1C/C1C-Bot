"""Parsing and canonical formatting for sheet-declared score answers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


class ScoreInputError(ValueError):
    """Raised when a score answer is malformed, ambiguous, or out of bounds."""


@dataclass(frozen=True, slots=True)
class ParsedScore:
    """A score in base units and its canonical compact representation."""

    value: Decimal
    compact: str


_QUALIFIER = re.compile(
    r"^(?:(?:over|about|around|roughly|approximately|approx)\s+|~\s*)",
    re.IGNORECASE,
)
_SCORE = re.compile(r"^(?P<number>[0-9][0-9.,]*)(?:\s*(?P<suffix>[a-z]+))?$", re.I)
_MULTIPLIERS = {
    "k": Decimal("1000"),
    "thousand": Decimal("1000"),
    "m": Decimal("1000000"),
    "mn": Decimal("1000000"),
    "mil": Decimal("1000000"),
    "mill": Decimal("1000000"),
    "million": Decimal("1000000"),
    "b": Decimal("1000000000"),
    "bn": Decimal("1000000000"),
    "bil": Decimal("1000000000"),
    "bill": Decimal("1000000000"),
    "billion": Decimal("1000000000"),
}


def _decimal_text(number: str, *, has_suffix: bool) -> str:
    if "," not in number:
        if number.count(".") > 1:
            raise ScoreInputError("malformed score")
        return number

    if "." in number:
        raise ScoreInputError("malformed score")
    if has_suffix:
        if number.count(",") != 1:
            raise ScoreInputError("malformed score")
        whole, fraction = number.split(",")
        if not whole or not fraction:
            raise ScoreInputError("malformed score")
        return f"{whole}.{fraction}"

    groups = number.split(",")
    if not groups[0] or not (1 <= len(groups[0]) <= 3):
        raise ScoreInputError("malformed score")
    if len(groups) < 2 or any(len(group) != 3 for group in groups[1:]):
        raise ScoreInputError("malformed score")
    return "".join(groups)


def _format_decimal(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _compact(value: Decimal) -> str:
    for multiplier, suffix in (
        (Decimal("1000000000"), "B"),
        (Decimal("1000000"), "M"),
        (Decimal("1000"), "K"),
    ):
        if value >= multiplier:
            return f"{_format_decimal(value / multiplier)}{suffix}"
    return _format_decimal(value)


def parse_score(
    raw: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> ParsedScore:
    """Parse a human-friendly score and enforce inclusive base-unit bounds."""

    text = "" if raw is None else str(raw).strip()
    if not text:
        raise ScoreInputError("empty score")
    text = _QUALIFIER.sub("", text, count=1).strip()
    if text.endswith("+"):
        text = text[:-1].strip()

    match = _SCORE.fullmatch(text)
    if not match:
        raise ScoreInputError("malformed score")
    number = match.group("number")
    suffix = (match.group("suffix") or "").lower()
    if suffix and suffix not in _MULTIPLIERS:
        raise ScoreInputError("unknown score suffix")

    decimal_text = _decimal_text(number, has_suffix=bool(suffix))
    if not suffix and (not decimal_text.isdigit() or len(decimal_text) < 4):
        raise ScoreInputError("ambiguous shortened score")
    try:
        value = Decimal(decimal_text) * _MULTIPLIERS.get(suffix, Decimal(1))
    except InvalidOperation as exc:
        raise ScoreInputError("malformed score") from exc
    if not value.is_finite() or value <= 0:
        raise ScoreInputError("score must be positive")
    if minimum is not None and value < minimum:
        raise ScoreInputError("score is below the configured minimum")
    if maximum is not None and value > maximum:
        raise ScoreInputError("score is above the configured maximum")
    return ParsedScore(value=value, compact=_compact(value))
