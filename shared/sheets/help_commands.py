"""Config-driven, cached source for the shared Discord help registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from shared.sheets.async_core import acall_with_backoff, aget_worksheet
from shared.sheets import recruitment

BUCKET_NAME = "helpcommands"
CACHE_TTL_SEC = 24 * 60 * 60
REQUIRED_HEADERS = (
    "enabled",
    "bot_key",
    "command_key",
    "command",
    "usage",
    "category",
    "access_level",
    "summary",
    "details",
    "sort_order",
)


@dataclass(frozen=True)
class HelpCommandRow:
    bot_key: str
    command_key: str
    command: str
    usage: str
    category: str
    access_level: str
    summary: str
    details: str
    sort_order: float | None
    source_order: int


def _truthy(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y", "on"}


def _header_map(headers: Sequence[object]) -> tuple[dict[str, int], list[str]]:
    mapped = {
        str(header or "").strip().lower(): index
        for index, header in enumerate(headers)
        if str(header or "").strip()
    }
    return mapped, [header for header in REQUIRED_HEADERS if header not in mapped]


def parse_rows(values: Sequence[Sequence[object]]) -> tuple[HelpCommandRow, ...]:
    """Parse a values matrix using header names rather than column positions."""

    if not values:
        raise RuntimeError("Shared help sheet is empty.")
    headers = values[0]
    columns, missing = _header_map(headers)
    if missing:
        raise RuntimeError(
            "Shared help sheet is missing headers: " + ", ".join(missing)
        )

    parsed: list[HelpCommandRow] = []
    for source_order, raw_row in enumerate(values[1:]):

        def value(name: str) -> str:
            index = columns[name]
            return str(raw_row[index] if index < len(raw_row) else "").strip()

        if not _truthy(value("enabled")):
            continue
        raw_sort = value("sort_order")
        try:
            sort_order = float(raw_sort)
        except (TypeError, ValueError):
            sort_order = None
        parsed.append(
            HelpCommandRow(
                bot_key=value("bot_key"),
                command_key=value("command_key"),
                command=value("command"),
                usage=value("usage"),
                category=value("category") or "Other",
                access_level=value("access_level").lower() or "user",
                summary=value("summary"),
                details=value("details"),
                sort_order=sort_order,
                source_order=source_order,
            )
        )
    parsed.sort(
        key=lambda row: (
            row.sort_order is None,
            row.sort_order if row.sort_order is not None else 0,
            row.source_order,
        )
    )
    return tuple(parsed)


async def _load() -> tuple[HelpCommandRow, ...]:
    sheet_id = recruitment.get_recruitment_sheet_id().strip()
    if not sheet_id:
        raise RuntimeError("RECRUITMENT_SHEET_ID is required for shared help.")
    tab_name = str(
        await recruitment.get_config_value_async("HELP_COMMANDS_TAB", None) or ""
    ).strip()
    if not tab_name:
        raise RuntimeError("Config key HELP_COMMANDS_TAB is required for shared help.")
    worksheet = await aget_worksheet(sheet_id, tab_name)
    values = await acall_with_backoff(worksheet.get_all_values)
    return parse_rows(values or [])


def register_cache_buckets() -> None:
    from shared.sheets.cache_service import cache

    if cache.get_bucket(BUCKET_NAME) is None:
        # Help should fail soft immediately; a Discord request must never wait for
        # the cache service's usual delayed retry.
        cache.register(BUCKET_NAME, CACHE_TTL_SEC, _load, retry_delay_sec=0)


async def get_rows() -> tuple[HelpCommandRow, ...] | None:
    """Return fresh or stale rows; synchronously warm only a completely cold cache."""

    from shared.sheets.cache_service import cache

    register_cache_buckets()
    bucket = cache.get_bucket(BUCKET_NAME)
    assert bucket is not None
    if bucket.value is None:
        await cache.refresh_now(BUCKET_NAME, actor="help", trigger="runtime")
        value = bucket.value
    else:
        value = await cache.get(BUCKET_NAME)
    return value if isinstance(value, tuple) else None


def normalize_lookup(value: object) -> str:
    text = " ".join(str(value or "").strip().lower().split())
    return text.lstrip("!").strip()


def find_row(rows: Sequence[HelpCommandRow], query: object) -> HelpCommandRow | None:
    needle = normalize_lookup(query)
    for row in rows:
        candidates = (
            row.command_key.replace("_", " "),
            row.command,
            row.usage.split(" ", 1)[0],
        )
        if any(normalize_lookup(candidate) == needle for candidate in candidates):
            return row
    return None


def visible_rows(
    rows: Sequence[HelpCommandRow],
    *,
    staff: bool,
    recruiter: bool = False,
    admin: bool,
) -> tuple[HelpCommandRow, ...]:
    allowed = {"user", "public"}
    if staff:
        allowed.add("staff")
    if recruiter:
        allowed.add("recruiter")
    if admin:
        allowed.update({"staff", "recruiter", "admin", "hidden"})
    return tuple(row for row in rows if row.access_level in allowed)


def group_rows(
    rows: Sequence[HelpCommandRow],
) -> tuple[tuple[str, str, tuple[HelpCommandRow, ...]], ...]:
    """Group rows by access level, then category, then numeric display order."""

    access_rank = {
        "public": 0,
        "user": 0,
        "recruiter": 1,
        "staff": 2,
        "admin": 3,
        "hidden": 4,
    }

    def row_key(row: HelpCommandRow) -> tuple[int, str, str, bool, float, int]:
        return (
            access_rank.get(row.access_level, 99),
            row.access_level,
            row.category.casefold(),
            row.sort_order is None,
            row.sort_order if row.sort_order is not None else 0,
            row.source_order,
        )

    grouped: list[tuple[str, str, tuple[HelpCommandRow, ...]]] = []
    current_key: tuple[str, str] | None = None
    current_rows: list[HelpCommandRow] = []
    for row in sorted(rows, key=row_key):
        key = (row.access_level, row.category)
        if current_key is not None and key != current_key:
            grouped.append((*current_key, tuple(current_rows)))
            current_rows = []
        current_key = key
        current_rows.append(row)
    if current_key is not None:
        grouped.append((*current_key, tuple(current_rows)))
    return tuple(grouped)
