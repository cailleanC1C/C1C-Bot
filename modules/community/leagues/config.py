from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator, Mapping

from shared.sheets import core as sheets_core
from shared.sheets.async_core import afetch_records


class LeaguesConfigError(RuntimeError):
    """Raised when the Leagues Config tab is missing required specs."""


@dataclass(frozen=True)
class LeagueSpec:
    key: str
    slug: str
    kind: str
    index: int | None
    sheet_name: str
    cell_range: str


@dataclass(frozen=True)
class LeagueBundle:
    slug: str
    display_name: str
    header: LeagueSpec | None
    boards: list[LeagueSpec]


_LEAGUE_MAP: dict[str, str] = {
    "legendary": "Legendary League",
    "rising": "Rising Stars League",
    "storm": "Stormforged League",
}


def _normalize(text: str | None) -> str:
    return (text or "").strip()


def _iter_league_rows(sheet_id: str, config_tab: str) -> Iterator[Mapping[str, object]]:
    """Yield league Config rows for sync scripts/tools/tests only.

    Live Discord runtime must use :func:`aload_league_bundles` so blocking
    Google Sheets reads never execute inside the bot event loop.
    """

    records = sheets_core.fetch_records(sheet_id, config_tab)
    yield from records or []


async def _aiter_league_rows(sheet_id: str, config_tab: str) -> list[Mapping[str, object]]:
    """Return league Config rows using the async Sheets boundary for bot runtime."""

    records = await afetch_records(sheet_id, config_tab)
    return list(records or [])


def _parse_bool(value: str | object | None) -> bool:
    text = str(value or "").strip().lower()
    if not text:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return True


def _extract_field(row: Mapping[str, object], *candidates: str) -> str:
    for name in candidates:
        key = name.strip().lower()
        for column, value in row.items():
            if (column or "").strip().lower() == key:
                return _normalize(str(value))
    return ""


def _build_tab_lookup(rows: Iterable[Mapping[str, object]]) -> dict[str, str]:
    tabs: dict[str, str] = {}
    for row in rows:
        spec_key = _extract_field(row, "spec_key", "key", "name")
        normalized_key = spec_key.strip().upper()
        if not normalized_key.endswith("_TAB"):
            continue

        sheet_name = _extract_field(row, "sheet_name", "sheet", "tab", "value", "val")
        if not sheet_name:
            continue

        tabs[normalized_key] = sheet_name
    return tabs


def _tab_lookup_key(spec_key: str) -> str:
    upper_key = spec_key.upper()
    for slug in _LEAGUE_MAP:
        prefix = f"LEAGUE_{slug.upper()}_"
        if upper_key.startswith(prefix):
            return f"{prefix}TAB"
    return ""


def _iter_league_specs(rows: Iterable[Mapping[str, object]]) -> Iterator[LeagueSpec]:
    tab_lookup = _build_tab_lookup(rows)

    for row in rows:
        spec_key = _extract_field(row, "spec_key", "key", "name")
        sheet_name = _extract_field(row, "sheet_name", "sheet", "tab")
        cell_range = _extract_field(row, "range", "cell_range", "value", "val")
        enabled = _parse_bool(_extract_field(row, "enabled", "active"))

        if not enabled:
            continue
        if not spec_key or not sheet_name or not cell_range:
            tab_key = _tab_lookup_key(spec_key)
            sheet_name = sheet_name or tab_lookup.get(tab_key, "")
            if not spec_key or not sheet_name or not cell_range:
                continue

        normalized_key = spec_key.strip()
        if not normalized_key.upper().startswith("LEAGUE_"):
            continue
        if normalized_key.upper().endswith("_TAB"):
            continue

        slug = ""
        upper_key = normalized_key.upper()
        for candidate in _LEAGUE_MAP:
            prefix = f"LEAGUE_{candidate.upper()}_"
            if upper_key.startswith(prefix):
                slug = candidate
                suffix = upper_key[len(prefix) :]
                break
        else:
            suffix = ""

        if not slug:
            continue

        if suffix == "HEADER":
            kind = "header"
            index: int | None = None
        else:
            kind = "board"
            try:
                index = int(suffix)
            except (TypeError, ValueError):
                continue

        yield LeagueSpec(
            key=normalized_key,
            slug=slug,
            kind=kind,
            index=index,
            sheet_name=sheet_name,
            cell_range=cell_range,
        )


def _bundle_for_slug(specs: list[LeagueSpec], slug: str) -> LeagueBundle | None:
    display_name = _LEAGUE_MAP[slug]
    header: LeagueSpec | None = None
    boards: list[LeagueSpec] = []

    for spec in specs:
        if spec.slug != slug:
            continue
        if spec.kind == "header":
            header = spec
        elif spec.kind == "board":
            boards.append(spec)

    if header is None and not boards:
        return None

    boards.sort(key=lambda item: item.index or 0)

    return LeagueBundle(
        slug=slug,
        display_name=display_name,
        header=header,
        boards=boards,
    )


def load_league_bundles_from_rows(rows: Iterable[Mapping[str, object]]) -> list[LeagueBundle]:
    specs = list(_iter_league_specs(rows))

    bundles: list[LeagueBundle] = []
    missing_headers: list[str] = []
    empty_boards: list[str] = []

    for slug in ("legendary", "rising", "storm"):
        bundle = _bundle_for_slug(specs, slug)
        if bundle is None:
            missing_headers.append(slug)
            continue

        if bundle.header is None:
            missing_headers.append(slug)
        elif not bundle.boards:
            empty_boards.append(slug)

        bundles.append(bundle)

    if missing_headers:
        raise LeaguesConfigError(
            f"config missing headers for: {', '.join(missing_headers)}"
        )
    if empty_boards:
        raise LeaguesConfigError(
            f"config has no boards for: {', '.join(empty_boards)}"
        )

    return bundles


def load_league_bundles(sheet_id: str, *, config_tab: str = "Config") -> list[LeagueBundle]:
    """Load league bundles synchronously for scripts/tools/tests only.

    Do not call this from Discord commands, listeners, schedulers, view
    callbacks, or background jobs. Use :func:`aload_league_bundles` instead.
    The sync Sheets retry guard intentionally raises when this path is used
    from an active event loop.
    """

    rows = list(_iter_league_rows(sheet_id, config_tab))
    return load_league_bundles_from_rows(rows)


async def aload_league_bundles(sheet_id: str, *, config_tab: str = "Config") -> list[LeagueBundle]:
    """Load league bundles through async Sheets access for live bot runtime."""

    rows = await _aiter_league_rows(sheet_id, config_tab)
    return load_league_bundles_from_rows(rows)
