"""Sheet-driven onboarding question schema loader."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence, Tuple

from shared.config import (
    cfg,
    get_onboarding_sheet_id,
    resolve_onboarding_tab,
)
from shared.sheets.async_core import afetch_records

__all__ = [
    "Question",
    "Option",
    "cached_rows",
    "fetch_question_rows_async",
    "resolve_source",
    "describe_source",
    "get_questions",
    "register_cache_buckets",
    "schema_hash",
]

log = logging.getLogger(__name__)

_cached_rows_snapshot: Tuple[dict[str, str], ...] | None = None
_cached_questions_by_flow: dict[str, Tuple[Question, ...]] = {}


def _question_tab() -> str:
    """Return the configured onboarding question tab name."""

    return resolve_onboarding_tab(cfg)


@dataclass(frozen=True, slots=True)
class Option:
    """Selectable option with a canonical token value."""

    label: str
    value: str


@dataclass(frozen=True, slots=True)
class Question:
    """Normalized representation of an onboarding question row."""

    flow: str
    order: str
    qid: str
    label: str
    type: str
    required: bool
    maxlen: int | None
    validate: str | None
    help: str | None
    options: tuple[Option, ...]
    multi_max: int | None
    visibility_rules: str | None = None
    nav_rules: str | None = None
    rules: str | None = None


def _sheet_id() -> str:
    sheet_id = get_onboarding_sheet_id().strip()
    if not sheet_id:
        raise KeyError("missing config key: ONBOARDING_SHEET_ID")
    return sheet_id


def resolve_source() -> tuple[str, str]:
    """Return the configured onboarding sheet identifier and tab name."""

    sheet_id = _sheet_id()
    tab = resolve_onboarding_tab(cfg)
    return sheet_id, tab


def describe_source() -> dict[str, str]:
    """Return metadata describing the onboarding question source."""

    sheet_id, tab = resolve_source()
    sheet_tail = sheet_id[-6:] if len(sheet_id) >= 6 else sheet_id
    redacted = f"…{sheet_tail}" if len(sheet_id) > len(sheet_tail) else sheet_tail
    return {"sheet": redacted, "tab": tab}


def _normalise_records(
    records: Iterable[Mapping[str, object]],
) -> Tuple[dict[str, str], ...]:
    parsed: list[dict[str, str]] = []
    for record in records:
        normalized: dict[str, str] = {}
        for key, value in record.items():
            key_norm = (key or "").strip().lower()
            if not key_norm:
                continue
            text = "" if value is None else str(value)
            normalized[key_norm] = text.strip()
        if normalized:
            parsed.append(normalized)
    return tuple(parsed)


async def fetch_question_rows_async() -> Tuple[dict[str, str], ...]:
    """Fetch and normalise onboarding question rows from Sheets."""

    sheet_id = _sheet_id()
    tab = _question_tab()
    sheet_tail = sheet_id[-6:] if len(sheet_id) >= 6 else sheet_id
    sheet_display = f"…{sheet_tail}" if len(sheet_id) > len(sheet_tail) else sheet_tail
    try:
        config_keys_count = len(cfg.keys())
    except Exception:
        config_keys_count = 0
    has_onboarding_tab = "ONBOARDING_TAB" in cfg
    log.info(
        "📦 Cache = bucket=onboarding_questions • sheet=%s • tab=%s • source=resolved",
        sheet_display,
        tab,
        extra={
            "sheet_tail": sheet_tail,
            "onboarding_tab": tab,
            "config_keys_count": config_keys_count,
            "has_ONBOARDING_TAB": "true" if has_onboarding_tab else "false",
        },
    )
    records = await afetch_records(sheet_id, tab)
    return _normalise_records(records)


def _coerce_rows(value: object) -> Tuple[dict[str, str], ...] | None:
    """Best-effort conversion of cached payloads into row tuples."""

    if value is None:
        return None
    if isinstance(value, tuple):
        return value
    if isinstance(value, list):
        return tuple(value)
    if isinstance(value, Iterable):
        return tuple(dict(row) for row in value)  # defensive copy for iterables
    return None


def cached_rows() -> Tuple[dict[str, str], ...] | None:
    """Return cached onboarding question rows if available, otherwise ``None``."""

    from shared.sheets.cache_service import cache

    bucket = cache.get_bucket("onboarding_questions")
    if bucket is None:
        return None
    return _coerce_rows(bucket.value)


def _cached_rows() -> Tuple[dict[str, str], ...]:
    """Return the cached onboarding question rows."""

    global _cached_rows_snapshot, _cached_questions_by_flow

    from shared.sheets.cache_service import cache

    bucket = cache.get_bucket("onboarding_questions")
    if bucket is None:
        _cached_rows_snapshot = None
        _cached_questions_by_flow.clear()
        raise RuntimeError("onboarding_questions cache bucket is not registered")

    rows = _coerce_rows(bucket.value)
    if rows is None:
        _cached_rows_snapshot = None
        _cached_questions_by_flow.clear()
        raise RuntimeError("onboarding_questions cache is empty (should be preloaded)")
    return rows


def _canonicalize_required(value: str | None) -> bool:
    text = (value or "").strip().lower()
    if not text:
        return False
    return text in {"1", "true", "yes", "y", "required"}


def _parse_int(value: str | None) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _normalise_whitespace(text: str | None) -> str | None:
    if text is None:
        return None
    collapsed = " ".join(text.strip().split())
    return collapsed or None


def _normalise_multiline(text: str | None) -> str | None:
    if text is None:
        return None
    lines = [segment.strip() for segment in str(text).splitlines()]
    cleaned = [line for line in lines if line]
    return "\n".join(cleaned) or None


def _canonicalise_option(label: str) -> Option:
    display = label.strip()
    return Option(label=display, value=display)


_RANGE_PATTERN = re.compile(r"^(?P<start>\d+)\s*[\-\u2013\u2014]\s*(?P<end>\d+)$")
_WHITESPACE_NUMBERS = re.compile(r"^\d+(?:\s+\d+)+$")
_COMPACT_DIGITS = re.compile(r"^\d{2,}$")


def _parse_options(raw_options: str | None) -> tuple[Option, ...]:
    if not raw_options:
        return ()

    text = raw_options.strip()
    if not text:
        return ()

    match = _RANGE_PATTERN.match(text)
    if match:
        start = int(match.group("start"))
        end = int(match.group("end"))
        if start <= end:
            return tuple(
                _canonicalise_option(str(number)) for number in range(start, end + 1)
            )

    raw_segments = re.split(r"[\n,]", text)
    segments = [segment.strip() for segment in raw_segments if segment.strip()]

    if len(segments) == 1:
        segment = segments[0]
        if _WHITESPACE_NUMBERS.match(segment):
            segments = segment.split()
        elif _COMPACT_DIGITS.match(segment):
            segments = list(segment)

    return tuple(_canonicalise_option(segment) for segment in segments)


def _normalise_type(raw_type: str | None) -> tuple[str, int | None]:
    text = (raw_type or "").strip().lower()
    if not text:
        raise ValueError("Question type is required")

    alias = text.replace("_", "-")
    compact = alias.replace(" ", "")

    # Canonicalise boolean-style labels from Sheets (yes/no, boolean, etc.).
    if alias.startswith("bool") or compact in {
        "boolean",
        "yes/no",
        "yes-no",
        "yesno",
        "y/n",
        "yn",
        "true/false",
        "true-false",
        "truefalse",
    }:
        return "bool", None

    if alias.startswith("multi-select"):
        parts = alias.split("-")
        max_count = None
        if len(parts) >= 3:
            try:
                max_count = int(parts[-1])
            except ValueError:
                max_count = None
        return "multi-select", max_count

    return alias, None


def _order_sort_key(order: str) -> tuple[int, str]:
    prefix = ""
    suffix = ""
    for idx, char in enumerate(order):
        if not char.isdigit():
            prefix = order[:idx]
            suffix = order[idx:]
            break
    else:
        prefix = order
        suffix = ""
    try:
        prefix_num = int(prefix)
    except ValueError:
        prefix_num = 0
    return prefix_num, suffix


def _build_questions(flow: str, rows: Sequence[Mapping[str, str]]) -> list[Question]:
    questions: list[Question] = []
    for row in rows:
        row_flow = row.get("flow")
        # Flow names are opaque strings (e.g., "welcome", "promo.r"); compare exactly.
        if _normalise_whitespace(row_flow) != flow:
            continue
        qid = row.get("qid") or ""
        label = row.get("label") or ""
        order = row.get("order") or ""
        qtype_raw = row.get("type")
        if not qid or not label or not order or not qtype_raw:
            continue
        try:
            qtype, multi_max = _normalise_type(qtype_raw)
        except ValueError:
            log.warning(
                "onboarding question missing type", extra={"qid": qid, "order": order}
            )
            continue
        options = (
            _parse_options(row.get("options"))
            if qtype in {"single-select", "multi-select"}
            else ()
        )
        validate_text = _normalise_whitespace(row.get("validate"))
        question = Question(
            flow=flow,
            order=order.strip(),
            qid=qid.strip(),
            label=label.strip(),
            type=qtype,
            required=_canonicalize_required(row.get("required")),
            maxlen=_parse_int(row.get("maxlen")),
            validate=validate_text,
            help=_normalise_whitespace(row.get("help")),
            options=options,
            multi_max=multi_max,
            visibility_rules=_normalise_multiline(row.get("visibility_rules")),
            nav_rules=_normalise_multiline(row.get("nav_rules")),
            rules=_normalise_whitespace(row.get("rules")),
        )
        questions.append(question)
    questions.sort(key=lambda q: _order_sort_key(q.order))
    return questions


def get_questions(flow: str) -> list[Question]:
    """Return the parsed onboarding questions for ``flow``."""

    questions = _questions_tuple(flow)
    return list(questions)


def _hash_payload(questions: Iterable[Question]) -> str:
    payload: list[dict[str, object]] = []
    for question in questions:
        payload.append(
            {
                "flow": question.flow,
                "order": question.order,
                "qid": question.qid,
                "label": question.label,
                "type": question.type,
                "required": question.required,
                "maxlen": question.maxlen,
                "validate": question.validate,
                "help": question.help,
                "options": [
                    (option.value, option.label) for option in question.options
                ],
                "visibility_rules": question.visibility_rules,
                "nav_rules": question.nav_rules,
            }
        )
    data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.md5(data, usedforsecurity=False).hexdigest()


def _questions_tuple(flow: str) -> Tuple[Question, ...]:
    global _cached_rows_snapshot, _cached_questions_by_flow

    rows = _cached_rows()
    if rows is not _cached_rows_snapshot:
        _cached_rows_snapshot = rows
        _cached_questions_by_flow = {}

    if flow not in _cached_questions_by_flow:
        _cached_questions_by_flow[flow] = tuple(_build_questions(flow, rows))
    return _cached_questions_by_flow.get(flow, ())


def schema_hash(flow: str) -> str:
    """Return the stable schema hash for the onboarding question flow."""

    questions = _questions_tuple(flow)
    return _hash_payload(questions)


def register_cache_buckets() -> None:
    """Register cache buckets used by onboarding questions."""

    from shared.sheets.cache_service import register_onboarding_questions_bucket

    register_onboarding_questions_bucket()
