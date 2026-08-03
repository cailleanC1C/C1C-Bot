"""Append-only, config-driven weekly history capture for C1C Leagues."""

from __future__ import annotations

import datetime as dt
import logging
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from shared.sheets.async_core import acall_with_backoff, afetch_records, afetch_values, aget_worksheet

log = logging.getLogger("c1c.community.leagues.history")

CONFIG_KEYS = (
    "cluster_capture_config_tab",
    "cluster_clan_map_tab",
    "cluster_event_history_tab",
    "cluster_evaluation_tab",
)
HISTORY_HEADERS = (
    "record_key", "week_key", "event_type", "cycle_start", "cycle_end",
    "clan_tag", "clan_name", "score", "score_unit", "result",
    "participation", "actions_used", "actions_available", "event_class",
    "evaluation_status", "captured_at_utc", "source_range", "source_row",
    "source_trigger",
)


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
    result_only_rows: int

    def status_text(self) -> str:
        return (
            f"History: week={self.week_key} • candidates={self.candidate_rows} "
            f"• appended={self.appended_rows} • existing={self.identical_rows} "
            f"• missing={self.missing_rows}"
        )


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
    tabs: dict[str, str] = {}
    for row in config_rows:
        key = _field(row, "spec_key", "key", "name").casefold()
        if key in CONFIG_KEYS:
            tabs[key] = _field(row, "sheet_name", "sheet", "tab", "value", "val")
    missing = [key for key in CONFIG_KEYS if not tabs.get(key)]
    if missing:
        raise HistoryCaptureError(f"history config missing tab keys: {', '.join(missing)}")
    return tabs


def build_active_clan_map(rows: Iterable[Mapping[str, object]]) -> tuple[dict[str, tuple[str, str]], dict[str, str]]:
    clans: dict[str, tuple[str, str]] = {}
    aliases: dict[str, str] = {}
    for row in rows:
        if not _enabled(_field(row, "active", "enabled")):
            continue
        tag = _field(row, "clan_tag", "tag")
        name = _field(row, "clan_name", "name")
        if not tag:
            raise HistoryCaptureError("active ClusterClanMap row has no clan_tag")
        clans[tag] = (tag, name)
        raw_aliases = _field(row, "aliases", "alias", "source_aliases")
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


def _column_number(label: str) -> int:
    clean = re.sub(r"[^A-Za-z]", "", label).upper()
    if not clean:
        raise HistoryCaptureError(f"invalid source column: {label!r}")
    number = 0
    for char in clean:
        number = number * 26 + ord(char) - 64
    return number


def _range_origin(a1_range: str) -> tuple[int, int]:
    first = a1_range.split(":", 1)[0].replace("$", "")
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", first)
    if not match:
        raise HistoryCaptureError(f"source range must start with a cell reference: {a1_range!r}")
    return _column_number(match.group(1)), int(match.group(2))


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


def _display_number(value: Decimal | None) -> object:
    if value is None:
        return ""
    return int(value) if value == value.to_integral_value() else float(value)


def _candidate(*, week_key: str, event_type: str, tag: str, name: str,
               score: object = "", score_unit: str = "", result: str = "",
               status: str, captured_at: str, source_range: str,
               source_row: object = "", trigger: str) -> dict[str, object]:
    values: dict[str, object] = {header: "" for header in HISTORY_HEADERS}
    values.update({
        "record_key": f"{week_key}|{event_type}|{tag}", "week_key": week_key,
        "event_type": event_type, "clan_tag": tag, "clan_name": name,
        "score": score, "score_unit": score_unit, "result": result,
        "evaluation_status": status, "captured_at_utc": captured_at,
        "source_range": source_range, "source_row": source_row,
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
    """Build and atomically append one candidate per active clan/spec."""

    config_rows = await afetch_records(sheet_id, config_tab)
    tabs = resolve_tab_names(config_rows)
    # Resolve all four keys up front. Evaluation is intentionally not read yet:
    # result-only inputs cannot support a complete mode rating.
    capture_rows = await afetch_records(sheet_id, tabs["cluster_capture_config_tab"])
    clan_rows = await afetch_records(sheet_id, tabs["cluster_clan_map_tab"])
    history_values = await afetch_values(sheet_id, tabs["cluster_event_history_tab"])
    _evaluation_tab = tabs["cluster_evaluation_tab"]

    clans, aliases = build_active_clan_map(clan_rows)
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
    for spec in specs:
        mode = _field(spec, "capture_mode", "mode").casefold()
        event_type = _field(spec, "event_type", "event", "spec_key", "key")
        if mode not in {"weekly_score", "cumulative_win_delta"} or not event_type:
            raise HistoryCaptureError(f"unsupported or incomplete capture spec: {event_type or '<unnamed>'}")
        tab, source_range = _spec_source(spec)
        matrix = await _read_unformatted(sheet_id, tab, source_range)
        origin_col, origin_row = _range_origin(source_range)
        found: dict[str, tuple[list[Any], int]] = {}

        current_clan_col = _field(spec, "current_clan_column", "clan_column")
        if not current_clan_col:
            raise HistoryCaptureError(f"{event_type}: current_clan_column is required")
        for offset, row in enumerate(matrix):
            source_name = _cell(row, current_clan_col, origin_col)
            if not str(source_name or "").strip():
                continue
            tag = aliases.get(normalize_alias(source_name))
            if tag is None:
                ignored += 1
                continue
            if tag in found:
                raise HistoryCaptureError(f"{event_type}: active clan {tag} appears more than once")
            found[tag] = (row, origin_row + offset)

        if mode == "cumulative_win_delta":
            previous_clan_col = _field(spec, "previous_clan_column", "prior_clan_column")
            previous_total_col = _field(spec, "previous_total_column", "prior_total_column")
            current_total_col = _field(spec, "current_total_column", "total_column")
            if not previous_clan_col or not previous_total_col or not current_total_col:
                raise HistoryCaptureError(f"{event_type}: cumulative columns are incomplete")
            previous: dict[str, Decimal] = {}
            for row in matrix:
                tag = aliases.get(normalize_alias(_cell(row, previous_clan_col, origin_col)))
                total = _number(_cell(row, previous_total_col, origin_col))
                if tag and total is not None:
                    previous[tag] = total

        for tag, (_canonical_tag, name) in clans.items():
            source = found.get(tag)
            score: object = ""
            result = ""
            status = "missing"
            unit = _field(spec, "score_unit", "unit")
            source_row: object = ""
            if source is not None:
                row, source_row = source
                if mode == "weekly_score":
                    score_col = _field(spec, "score_column", "current_score_column")
                    if not score_col:
                        raise HistoryCaptureError(f"{event_type}: score_column is required")
                    numeric = _number(_cell(row, score_col, origin_col))
                    if numeric is not None and numeric > 0:
                        score, status = _display_number(numeric), "valid"
                else:
                    current = _number(_cell(row, current_total_col, origin_col))
                    prior = previous.get(tag)
                    if current is not None and prior is not None:
                        delta = current - prior
                        if delta < 0:
                            raise HistoryCaptureError(f"{event_type}: negative cumulative delta for {tag}")
                        score, unit = _display_number(delta), "wins"
                        result = "win" if delta > 0 else "loss"
                        status = _field(spec, "history_status", "evaluation_status", default="result_only") or "result_only"
            candidates.append(_candidate(
                week_key=week_key, event_type=event_type, tag=tag, name=name,
                score=score, score_unit=unit if score != "" else "", result=result,
                status=status, captured_at=now, source_range=f"{tab}!{source_range}",
                source_row=source_row, trigger=trigger,
            ))

    existing: dict[str, dict[str, object]] = {}
    candidate_keys = [str(candidate["record_key"]) for candidate in candidates]
    if len(candidate_keys) != len(set(candidate_keys)):
        raise HistoryCaptureError(
            "enabled capture specs produce duplicate week/event/clan record keys"
        )
    for row in history_values[1:]:
        values = {header: row[index] if index < len(row) else "" for index, header in enumerate(headers)}
        key = str(values.get("record_key", "")).strip()
        if key:
            existing[key] = values
    append: list[dict[str, object]] = []
    identical = 0
    comparable = tuple(header for header in HISTORY_HEADERS if header != "captured_at_utc")
    for candidate in candidates:
        old = existing.get(str(candidate["record_key"]))
        if old is None:
            append.append(candidate)
        elif all(str(old.get(key, "")) == str(candidate.get(key, "")) for key in comparable):
            identical += 1
        else:
            raise HistoryCaptureError(f"history-conflict for record_key {candidate['record_key']}")

    if append:
        worksheet = await aget_worksheet(sheet_id, tabs["cluster_event_history_tab"])
        rows = [[candidate.get(header, "") for header in headers] for candidate in append]
        await acall_with_backoff(worksheet.append_rows, rows, value_input_option="RAW")

    summary = CaptureSummary(
        week_key=week_key, active_clans=len(clans), enabled_specs=len(specs),
        candidate_rows=len(candidates), appended_rows=len(append), identical_rows=identical,
        missing_rows=sum(row["evaluation_status"] == "missing" for row in candidates),
        ignored_source_clans=ignored,
        result_only_rows=sum(row["evaluation_status"] == "result_only" for row in candidates),
    )
    log.info("league history capture completed", extra=summary.__dict__)
    return summary
