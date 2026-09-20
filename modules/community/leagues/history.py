"""Append-only, config-driven weekly history capture for C1C Leagues."""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from shared.sheets.async_core import acall_with_backoff, afetch_records, afetch_values, aget_worksheet

log = logging.getLogger("c1c.community.leagues.history")

_REQUIRED_CONFIG_KEYS = (
    "cluster_capture_config_tab",
    "cluster_event_history_tab",
    "cluster_evaluation_tab",
)
_CLAN_TAB_KEYS = ("cluster_clans_tab", "cluster_clan_map_tab")
HISTORY_HEADERS = (
    "record_key", "week_key", "event_type", "clan_tag", "clan_name",
    "score", "score_unit", "result", "event_class", "evaluation_status",
    "captured_at_utc", "source_range", "source_row", "source_trigger",
)
SEMANTIC_EVENT_FIELDS = (
    "week_key", "event_type", "clan_tag", "score", "score_unit", "result",
    "event_class", "evaluation_status",
)
_NUMERIC_SEMANTIC_FIELDS = {"score"}
_EVENT_CONFIG_COLUMNS = {
    "hydra_clash": "hydra",
    "chimera_clash": "chimera",
    "cvc": "cvc",
    "siege": "siege",
}
_EXPECTATIONS = {"mandatory", "optional", "n/a"}
_CVC_ANCHOR = dt.date.fromisocalendar(2026, 31, 1)


class HistoryCaptureError(RuntimeError):
    """Raised when history cannot be validated or safely appended."""


@dataclass(frozen=True)
class CaptureSummary:
    week_key: str
    active_clans: int
    enabled_specs: int
    candidate_rows: int
    appended_rows: int
    identical_rows: int
    missing_rows: int
    ignored_source_clans: int
    error_rows: int = 0
    mandatory_missing_rows: int = 0
    optional_missing_rows: int = 0
    event_stats: Mapping[str, Mapping[str, int | str]] = field(default_factory=dict)

    @property
    def result_only_rows(self) -> int:
        """Compatibility shim for older callers/tests; result_only is retired."""
        return 0

    def status_text(self) -> str:
        lines = [f"**Cluster history — results for {self.week_key}**"]
        labels = (
            ("hydra_clash", "Hydra"),
            ("chimera_clash", "Chimera"),
            ("cvc", "CvC"),
            ("siege", "Siege"),
        )
        for event_type, label in labels:
            stats = self.event_stats.get(event_type, {})
            if stats.get("scheduled") == "no":
                lines.append(f"{label}: not scheduled")
                continue
            valid = int(stats.get("valid", 0))
            mandatory = int(stats.get("mandatory_missing", 0))
            optional = int(stats.get("optional_missing", 0))
            errors = int(stats.get("error", 0))
            bits = [f"{valid} valid"]
            if mandatory:
                bits.append(f"{mandatory} mandatory missing")
            if optional:
                bits.append(f"{optional} optional missing")
            if errors:
                bits.append(f"{errors} error{'s' if errors != 1 else ''}")
            lines.append(f"{label}: " + " · ".join(bits))
        if self.error_rows:
            lines.append(f"⚠️ Data errors: {self.error_rows}")
        return "\n".join(lines)


def normalize_alias(value: object) -> str:
    """Normalize clan aliases without case, whitespace, or punctuation."""
    return "".join(char for char in str(value or "").casefold() if char.isalnum())


def _field(row: Mapping[str, object], *names: str, default: str = "") -> str:
    wanted = {name.casefold() for name in names}
    for key, value in row.items():
        if str(key or "").strip().casefold() in wanted:
            return str(value or "").strip()
    return default


def _enabled(value: object) -> bool:
    return str(value or "").strip().casefold() in {"1", "true", "yes", "on"}


def resolve_tab_names(config_rows: Iterable[Mapping[str, object]]) -> dict[str, str]:
    values: dict[str, str] = {}
    for row in config_rows:
        key = _field(row, "spec_key", "key", "name").casefold()
        if key in {*_REQUIRED_CONFIG_KEYS, *_CLAN_TAB_KEYS}:
            values[key] = _field(row, "sheet_name", "sheet", "tab", "value", "val")
    missing = [key for key in _REQUIRED_CONFIG_KEYS if not values.get(key)]
    clan_tab = values.get("cluster_clans_tab") or values.get("cluster_clan_map_tab")
    if not clan_tab:
        missing.append("cluster_clans_tab")
    if missing:
        raise HistoryCaptureError(f"history config missing tab keys: {', '.join(missing)}")
    values["cluster_clans_tab"] = clan_tab
    return values


def build_active_clan_map(
    rows: Iterable[Mapping[str, object]],
) -> tuple[dict[str, tuple[str, str, dict[str, str]]], dict[str, str]]:
    clans: dict[str, tuple[str, str, dict[str, str]]] = {}
    aliases: dict[str, str] = {}
    for row in rows:
        if not _enabled(_field(row, "active", "enabled")):
            continue
        tag = _field(row, "canonical_clan_tag", "clan_tag", "tag")
        name = _field(row, "canonical_clan_name", "clan_name", "name")
        if not tag:
            raise HistoryCaptureError("active ClusterClans row has no clan_tag")
        expectations: dict[str, str] = {}
        for event_type, column in _EVENT_CONFIG_COLUMNS.items():
            expectation = _field(row, column, default="mandatory").casefold()
            if expectation not in _EXPECTATIONS:
                raise HistoryCaptureError(
                    f"{tag}: invalid {column} expectation {expectation!r}; "
                    "expected Mandatory, Optional, or N/A"
                )
            expectations[event_type] = expectation
        previous_clan = clans.get(tag)
        if previous_clan is None:
            clans[tag] = (tag, name, expectations)
        else:
            _, previous_name, previous_expectations = previous_clan
            if previous_name != name or previous_expectations != expectations:
                raise HistoryCaptureError(f"active clan {tag} has conflicting ClusterClans rows")
        raw_aliases = _field(row, "source_alias", "source_aliases", "aliases", "alias")
        values = [tag, name, *re.split(r"[,;|\n]", raw_aliases)]
        for value in values:
            alias = normalize_alias(value)
            if not alias:
                continue
            previous = aliases.get(alias)
            if previous is not None and previous != tag:
                raise HistoryCaptureError(
                    f"active clan alias collision: {value!r} maps to both {previous} and {tag}"
                )
            aliases[alias] = tag
    return clans, aliases


def _parse_week_key(week_key: str) -> dt.date:
    match = re.fullmatch(r"(\d{4})-W(\d{2})", str(week_key or "").strip())
    if not match:
        raise HistoryCaptureError(f"invalid ISO week key: {week_key!r}")
    try:
        return dt.date.fromisocalendar(int(match.group(1)), int(match.group(2)), 1)
    except ValueError as exc:
        raise HistoryCaptureError(f"invalid ISO week key: {week_key!r}") from exc


def previous_iso_week_key(week_key: str) -> str:
    """Return the ISO week immediately before a posting/job week."""
    previous = _parse_week_key(week_key) - dt.timedelta(days=7)
    iso = previous.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def event_occurs(event_type: str, week_key: str) -> bool:
    """Return whether an event belongs to the supplied result week."""
    if event_type in {"hydra_clash", "chimera_clash"}:
        return True
    week_start = _parse_week_key(week_key)
    delta_weeks = (week_start - _CVC_ANCHOR).days // 7
    if event_type == "cvc":
        return delta_weeks % 2 == 0
    if event_type == "siege":
        return delta_weeks % 2 != 0
    return True


def cvc_event_class(week_key: str) -> str:
    """Return PR / Non-PR for a CvC result week (W31 2026 is PR)."""
    week_start = _parse_week_key(week_key)
    delta_weeks = (week_start - _CVC_ANCHOR).days // 7
    if delta_weeks % 2:
        raise HistoryCaptureError(f"{week_key} is not a CvC result week")
    cvc_index = delta_weeks // 2
    return "PR" if cvc_index % 2 == 0 else "Non-PR"


def _column_number(label: str) -> int:
    clean = str(label or "").strip().replace("$", "").upper()
    if not re.fullmatch(r"[A-Z]+", clean):
        raise HistoryCaptureError(f"invalid source column: {label!r}")
    number = 0
    for char in clean:
        number = number * 26 + ord(char) - 64
    return number


def _range_bounds(a1_range: str) -> tuple[int, int, int]:
    clean = str(a1_range or "").strip().replace("$", "")
    match = re.fullmatch(r"([A-Za-z]+)(\d+):([A-Za-z]+)(\d+)", clean)
    if not match:
        raise HistoryCaptureError(f"source range must be a bounded A1 range: {a1_range!r}")
    left = _column_number(match.group(1))
    right = _column_number(match.group(3))
    start_row, end_row = int(match.group(2)), int(match.group(4))
    if left > right or start_row > end_row:
        raise HistoryCaptureError(f"source range boundaries are reversed: {a1_range!r}")
    return left, right, start_row


def _validate_source_columns(
    event_type: str, a1_range: str, columns: Mapping[str, str],
) -> tuple[int, int]:
    left, right, start_row = _range_bounds(a1_range)
    for field_name, label in columns.items():
        if not label:
            raise HistoryCaptureError(f"{event_type}: {field_name} is required")
        number = _column_number(label)
        if number < left or number > right:
            raise HistoryCaptureError(
                f"{event_type}: {field_name} column {label!r} is outside configured range {a1_range!r}"
            )
    return left, start_row


def _cell(row: list[Any], column: str, origin_column: int) -> Any:
    index = _column_number(column) - origin_column
    if index < 0:
        raise HistoryCaptureError(f"configured column {column} is outside source range")
    return row[index] if index < len(row) else ""


def _number(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool) or str(value).strip() == "":
        return None
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return result if result.is_finite() else None


def _blank(value: object) -> bool:
    return value is None or str(value).strip() == ""


def _display_number(value: Decimal | None) -> object:
    if value is None:
        return ""
    return int(value) if value == value.to_integral_value() else float(value)


def _semantic_value(field_name: str, value: object) -> object:
    if field_name in _NUMERIC_SEMANTIC_FIELDS:
        numeric = _number(value)
        return numeric if numeric is not None else ""
    return str(value or "").strip()


def _same_event_payload(old: Mapping[str, object], candidate: Mapping[str, object]) -> bool:
    return all(
        _semantic_value(field_name, old.get(field_name, ""))
        == _semantic_value(field_name, candidate.get(field_name, ""))
        for field_name in SEMANTIC_EVENT_FIELDS
    )


def _candidate(
    *, week_key: str, event_type: str, tag: str, name: str,
    score: object = "", score_unit: str = "", result: str = "",
    event_class: str = "", status: str, captured_at: str,
    source_range: str, source_row: object = "", trigger: str,
) -> dict[str, object]:
    values: dict[str, object] = {header: "" for header in HISTORY_HEADERS}
    values.update({
        "record_key": f"{week_key}|{event_type}|{tag}",
        "week_key": week_key,
        "event_type": event_type,
        "clan_tag": tag,
        "clan_name": name,
        "score": score,
        "score_unit": score_unit,
        "result": result,
        "event_class": event_class,
        "evaluation_status": status,
        "captured_at_utc": captured_at,
        "source_range": source_range,
        "source_row": source_row,
        "source_trigger": trigger,
    })
    return values


async def _read_unformatted(sheet_id: str, tab: str, cell_range: str) -> list[list[Any]]:
    worksheet = await aget_worksheet(sheet_id, tab)
    values = await acall_with_backoff(
        worksheet.get, cell_range, value_render_option="UNFORMATTED_VALUE"
    )
    return list(values or [])


def _spec_source(spec: Mapping[str, object]) -> tuple[str, str]:
    tab = _field(spec, "source_worksheet", "source_sheet", "source_tab", "worksheet", "sheet_name")
    cell_range = _field(spec, "source_range", "range", "cell_range")
    if not tab or not cell_range:
        raise HistoryCaptureError("enabled capture spec requires source worksheet and range")
    return tab, cell_range


async def capture_weekly_history(
    sheet_id: str, *, config_tab: str, week_key: str, trigger: str,
    captured_at: dt.datetime | None = None,
) -> CaptureSummary:
    """Capture one result week using clan expectations and the event calendar."""

    _parse_week_key(week_key)
    config_rows = await afetch_records(sheet_id, config_tab)
    tabs = resolve_tab_names(config_rows)
    capture_rows = await afetch_records(sheet_id, tabs["cluster_capture_config_tab"])
    clan_rows = await afetch_records(sheet_id, tabs["cluster_clans_tab"])
    history_values = await afetch_values(sheet_id, tabs["cluster_event_history_tab"])
    _evaluation_tab = tabs["cluster_evaluation_tab"]

    clans, aliases = build_active_clan_map(clan_rows)
    if not clans:
        raise HistoryCaptureError("ClusterClans resolves to zero active clans")
    specs = [row for row in capture_rows if _enabled(_field(row, "enabled", "active"))]
    if not specs:
        raise HistoryCaptureError("ClusterCaptureConfig has no enabled capture specs")
    if not history_values:
        raise HistoryCaptureError("ClusterEventHistory header row is missing")
    headers = [str(value or "").strip() for value in history_values[0]]
    missing_headers = [header for header in HISTORY_HEADERS if header not in headers]
    if missing_headers:
        raise HistoryCaptureError(f"ClusterEventHistory missing headers: {', '.join(missing_headers)}")

    now = (captured_at or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc).isoformat()
    candidates: list[dict[str, object]] = []
    ignored = 0
    event_stats: dict[str, dict[str, int | str]] = {}

    for spec in specs:
        mode = _field(spec, "capture_mode", "mode").casefold()
        event_type = _field(spec, "event_type", "event", "spec_key", "key").casefold()
        if mode not in {"weekly_score", "cumulative_win_delta"} or not event_type:
            raise HistoryCaptureError(f"unsupported or incomplete capture spec: {event_type or '<unnamed>'}")
        if event_type not in _EVENT_CONFIG_COLUMNS:
            raise HistoryCaptureError(f"unsupported cluster event_type: {event_type}")
        if not event_occurs(event_type, week_key):
            event_stats[event_type] = {"scheduled": "no"}
            continue

        stats: dict[str, int | str] = {
            "scheduled": "yes", "valid": 0, "mandatory_missing": 0,
            "optional_missing": 0, "error": 0,
        }
        event_stats[event_type] = stats
        tab, source_range = _spec_source(spec)
        current_clan_col = _field(spec, "current_clan_column", "clan_column")
        columns = {"current_clan_column": current_clan_col}
        if mode == "weekly_score":
            score_col = _field(spec, "score_column", "current_score_column")
            columns["score_column"] = score_col
        else:
            previous_clan_col = _field(spec, "previous_clan_column", "prior_clan_column")
            previous_total_col = _field(spec, "previous_total_column", "prior_total_column")
            current_total_col = _field(spec, "current_total_column", "total_column")
            columns.update({
                "current_total_column": current_total_col,
                "previous_clan_column": previous_clan_col,
                "previous_total_column": previous_total_col,
            })
        origin_col, origin_row = _validate_source_columns(event_type, source_range, columns)
        matrix = await _read_unformatted(sheet_id, tab, source_range)
        found: dict[str, tuple[list[Any], int]] = {}
        populated_source_clans = 0
        for offset, row in enumerate(matrix):
            source_name = _cell(row, current_clan_col, origin_col)
            if not str(source_name or "").strip():
                continue
            populated_source_clans += 1
            tag = aliases.get(normalize_alias(source_name))
            if tag is None:
                ignored += 1
                continue
            if tag in found:
                raise HistoryCaptureError(f"{event_type}: active clan {tag} appears more than once")
            found[tag] = (row, origin_row + offset)
        if populated_source_clans and not found:
            raise HistoryCaptureError(
                f"{event_type}: populated source contains clan names but zero clans "
                "match the active ClusterClans registry"
            )

        previous: dict[str, tuple[object, Decimal | None]] = {}
        if mode == "cumulative_win_delta":
            for row in matrix:
                tag = aliases.get(normalize_alias(_cell(row, previous_clan_col, origin_col)))
                if tag:
                    raw = _cell(row, previous_total_col, origin_col)
                    previous[tag] = (raw, _number(raw))

        for tag, (_canonical_tag, name, expectations) in clans.items():
            expectation = expectations[event_type]
            if expectation == "n/a":
                continue
            source = found.get(tag)
            score: object = ""
            result = ""
            status = "error" if source is None else "missing"
            unit = _field(spec, "score_unit", "unit")
            source_row: object = ""
            if source is not None:
                row, source_row = source
                if mode == "weekly_score":
                    raw_score = _cell(row, score_col, origin_col)
                    numeric = _number(raw_score)
                    if _blank(raw_score) or numeric == 0:
                        status = "missing"
                    elif numeric is None or numeric < 0:
                        status = "error"
                    else:
                        score, status = _display_number(numeric), "valid"
                else:
                    raw_current = _cell(row, current_total_col, origin_col)
                    current = _number(raw_current)
                    prior_pair = previous.get(tag)
                    if _blank(raw_current):
                        status = "missing"
                    elif current is None:
                        status = "error"
                    elif prior_pair is None or prior_pair[1] is None:
                        status = "error"
                    else:
                        prior = prior_pair[1]
                        assert prior is not None
                        delta = current - prior
                        if delta < 0:
                            status = "error"
                        else:
                            score, unit = _display_number(delta), "wins"
                            result = "win" if delta > 0 else "loss"
                            status = "valid"

            if status == "valid":
                stats["valid"] = int(stats["valid"]) + 1
            elif status == "error":
                stats["error"] = int(stats["error"]) + 1
            elif expectation == "mandatory":
                stats["mandatory_missing"] = int(stats["mandatory_missing"]) + 1
            else:
                stats["optional_missing"] = int(stats["optional_missing"]) + 1

            candidates.append(_candidate(
                week_key=week_key,
                event_type=event_type,
                tag=tag,
                name=name,
                score=score,
                score_unit=unit if score != "" else "",
                result=result,
                event_class=cvc_event_class(week_key) if event_type == "cvc" else "",
                status=status,
                captured_at=now,
                source_range=f"{tab}!{source_range}",
                source_row=source_row,
                trigger=trigger,
            ))

    existing: dict[str, dict[str, object]] = {}
    candidate_keys = [str(candidate["record_key"]) for candidate in candidates]
    if len(candidate_keys) != len(set(candidate_keys)):
        raise HistoryCaptureError("enabled capture specs produce duplicate week/event/clan record keys")
    for row in history_values[1:]:
        values = {
            header: row[index] if index < len(row) else ""
            for index, header in enumerate(headers)
        }
        key = str(values.get("record_key", "")).strip()
        if key:
            existing[key] = values

    append: list[dict[str, object]] = []
    identical = 0
    for candidate in candidates:
        old = existing.get(str(candidate["record_key"]))
        if old is None:
            append.append(candidate)
        elif _same_event_payload(old, candidate):
            identical += 1
        else:
            raise HistoryCaptureError(f"history-conflict for record_key {candidate['record_key']}")

    if append:
        worksheet = await aget_worksheet(sheet_id, tabs["cluster_event_history_tab"])
        rows = [[candidate.get(header, "") for header in headers] for candidate in append]
        await acall_with_backoff(worksheet.append_rows, rows, value_input_option="RAW")

    missing_rows = sum(row["evaluation_status"] == "missing" for row in candidates)
    error_rows = sum(row["evaluation_status"] == "error" for row in candidates)
    mandatory_missing = sum(
        int(stats.get("mandatory_missing", 0)) for stats in event_stats.values()
    )
    optional_missing = sum(
        int(stats.get("optional_missing", 0)) for stats in event_stats.values()
    )
    summary = CaptureSummary(
        week_key=week_key,
        active_clans=len(clans),
        enabled_specs=len(specs),
        candidate_rows=len(candidates),
        appended_rows=len(append),
        identical_rows=identical,
        missing_rows=missing_rows,
        ignored_source_clans=ignored,
        error_rows=error_rows,
        mandatory_missing_rows=mandatory_missing,
        optional_missing_rows=optional_missing,
        event_stats=event_stats,
    )
    log.info("league history capture completed", extra=summary.__dict__)
    return summary
