from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_ROOTS = (ROOT / "modules", ROOT / "cogs", ROOT / "shared")

SYNC_SHEETS_CORE_MODULE = "shared.sheets.core"
SYNC_CONFIG_MODULE = "shared.config"
LEAGUES_CONFIG_MODULE = "modules.community.leagues.config"
ONBOARDING_SHEETS_MODULE = "shared.sheets.onboarding"
ONBOARDING_SESSIONS_MODULE = "shared.sheets.onboarding_sessions"
RECRUITMENT_SHEETS_MODULE = "shared.sheets.recruitment"

FORBIDDEN_SYNC_HELPERS = {
    f"{SYNC_SHEETS_CORE_MODULE}.fetch_records",
    f"{SYNC_SHEETS_CORE_MODULE}.fetch_values",
    f"{SYNC_SHEETS_CORE_MODULE}.get_worksheet",
    f"{SYNC_SHEETS_CORE_MODULE}.call_with_backoff",
    f"{SYNC_SHEETS_CORE_MODULE}.read_table",
    f"{SYNC_SHEETS_CORE_MODULE}.sheets_read",
    f"{SYNC_SHEETS_CORE_MODULE}._retry_with_backoff",
    f"{SYNC_CONFIG_MODULE}.merge_onboarding_config_early",
    f"{SYNC_CONFIG_MODULE}._load_onboarding_config_values",
    f"{SYNC_CONFIG_MODULE}._load_milestones_config_values",
    f"{LEAGUES_CONFIG_MODULE}.load_league_bundles",
    f"{ONBOARDING_SHEETS_MODULE}._read_onboarding_config",
    f"{ONBOARDING_SHEETS_MODULE}.get_ticket_finalization_state",
    f"{ONBOARDING_SHEETS_MODULE}.get_finalization_headers",
    f"{ONBOARDING_SHEETS_MODULE}.get_promo_source_clan_tag_header",
    f"{ONBOARDING_SESSIONS_MODULE}.get_by_thread_id",
    f"{ONBOARDING_SESSIONS_MODULE}.load_all",
    f"{ONBOARDING_SESSIONS_MODULE}.update_existing",
    f"{ONBOARDING_SESSIONS_MODULE}.upsert_session",
    f"{ONBOARDING_SESSIONS_MODULE}.mark_completed",
    f"{ONBOARDING_SESSIONS_MODULE}.missing_columns",
    f"{RECRUITMENT_SHEETS_MODULE}.get_config_value",
    f"{RECRUITMENT_SHEETS_MODULE}.get_clan_header_row",
    f"{RECRUITMENT_SHEETS_MODULE}.fetch_clans",
    f"{RECRUITMENT_SHEETS_MODULE}.find_clan_row",
    f"{RECRUITMENT_SHEETS_MODULE}.get_clan_by_tag",
    "_read_onboarding_config",
}

ALLOWED_ASYNC_BRIDGES = {
    "asyncio.to_thread",
    "shared.sheets.async_core.a_to_thread_with_backoff",
    "shared.sheets.async_core.acall_with_backoff",
    "a_to_thread_with_backoff",
}

APPROVED_BRIDGE_FILES = {
    Path("shared/sheets/async_core.py"),
    Path("shared/sheets/async_facade.py"),
    Path("shared/sheets/async_adapter.py"),
}

MODULE_ALIAS_TARGETS = {
    SYNC_SHEETS_CORE_MODULE,
    SYNC_CONFIG_MODULE,
    LEAGUES_CONFIG_MODULE,
    ONBOARDING_SHEETS_MODULE,
    ONBOARDING_SESSIONS_MODULE,
    RECRUITMENT_SHEETS_MODULE,
    "shared.sheets.async_core",
    "asyncio",
}

DIRECT_IMPORT_MODULES = {
    SYNC_SHEETS_CORE_MODULE,
    SYNC_CONFIG_MODULE,
    LEAGUES_CONFIG_MODULE,
    ONBOARDING_SHEETS_MODULE,
    ONBOARDING_SESSIONS_MODULE,
    RECRUITMENT_SHEETS_MODULE,
    "shared.sheets.async_core",
}


class _RuntimeBoundaryVisitor(ast.NodeVisitor):
    def __init__(self, *, allow_forbidden_bridges: bool = False) -> None:
        self.aliases: dict[str, str] = {}
        self.violations: list[str] = []
        self._async_depth = 0
        self.allow_forbidden_bridges = allow_forbidden_bridges

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802 - ast API
        for alias in node.names:
            if alias.name in MODULE_ALIAS_TARGETS:
                self.aliases[alias.asname or alias.name.split(".", 1)[0]] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802 - ast API
        if node.module is None:
            return
        for alias in node.names:
            local = alias.asname or alias.name
            if node.module == "shared.sheets" and alias.name == "core":
                self.aliases[local] = SYNC_SHEETS_CORE_MODULE
            elif node.module == "shared.sheets" and alias.name == "onboarding":
                self.aliases[local] = ONBOARDING_SHEETS_MODULE
            elif node.module == "shared.sheets" and alias.name == "onboarding_sessions":
                self.aliases[local] = ONBOARDING_SESSIONS_MODULE
            elif node.module == "shared.sheets" and alias.name == "recruitment":
                self.aliases[local] = RECRUITMENT_SHEETS_MODULE
            elif node.module == "shared" and alias.name == "config":
                self.aliases[local] = SYNC_CONFIG_MODULE
            elif node.module in DIRECT_IMPORT_MODULES:
                self.aliases[local] = f"{node.module}.{alias.name}"
            elif f"{node.module}.{alias.name}" in MODULE_ALIAS_TARGETS:
                self.aliases[local] = f"{node.module}.{alias.name}"
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802 - ast API
        self._async_depth += 1
        self.generic_visit(node)
        self._async_depth -= 1

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802 - ast API
        if self._async_depth:
            bridge_name = self._resolve_name(node.func)
            bridged_name = self._resolve_name(node.args[0]) if node.args else ""
            locally_bridged_forbidden = (
                bridge_name in ALLOWED_ASYNC_BRIDGES
                and bridged_name in FORBIDDEN_SYNC_HELPERS
                and not self.allow_forbidden_bridges
            )
            if locally_bridged_forbidden:
                self.violations.append(f"{node.lineno} calls {bridge_name}")
            elif not self._is_allowed_bridge_call(node):
                resolved = self._resolve_name(node.func)
                if resolved in FORBIDDEN_SYNC_HELPERS:
                    self.violations.append(f"{node.lineno} calls {resolved}")
        self.generic_visit(node)

    def _is_allowed_bridge_call(self, node: ast.Call) -> bool:
        if not self.allow_forbidden_bridges:
            return False
        bridge_name = self._resolve_name(node.func)
        if bridge_name not in ALLOWED_ASYNC_BRIDGES:
            return False
        if not node.args:
            return False
        bridged_name = self._resolve_name(node.args[0])
        return bridged_name in FORBIDDEN_SYNC_HELPERS

    def _resolve_name(self, node: ast.AST) -> str:
        raw = self._call_name(node)
        if not raw:
            return ""
        if raw in FORBIDDEN_SYNC_HELPERS or raw in ALLOWED_ASYNC_BRIDGES:
            return raw
        if any(raw.startswith(f"{module}.") for module in MODULE_ALIAS_TARGETS):
            return raw
        if raw in self.aliases:
            return self.aliases[raw]
        first, dot, rest = raw.partition(".")
        if first in self.aliases:
            return f"{self.aliases[first]}{dot}{rest}" if dot else self.aliases[first]
        return raw

    def _call_name(self, node: ast.AST) -> str:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = self._call_name(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return ""


def _violations_for_source(source: str, *, approved_bridge_file: bool = False) -> list[str]:
    tree = ast.parse(source)
    visitor = _RuntimeBoundaryVisitor(allow_forbidden_bridges=approved_bridge_file)
    visitor.visit(tree)
    return visitor.violations


def _violations_for_path(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path.relative_to(ROOT)))
    visitor = _RuntimeBoundaryVisitor(
        allow_forbidden_bridges=path.relative_to(ROOT) in APPROVED_BRIDGE_FILES
    )
    visitor.visit(tree)
    return [f"{path.relative_to(ROOT)}:{violation}" for violation in visitor.violations]


def test_runtime_async_functions_do_not_directly_call_sync_sheet_config_helpers() -> None:
    """Live Discord runtime async code must stay on async-safe Sheets/config APIs."""

    violations: list[str] = []
    for root in RUNTIME_ROOTS:
        for path in root.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            violations.extend(_violations_for_path(path))

    assert violations == []


def test_runtime_boundary_catches_sync_core_import_styles() -> None:
    source = """
import shared.sheets.core
import shared.sheets.core as sc
from shared.sheets import core as sheets_core
from shared.sheets.core import fetch_records, fetch_values as fv, get_worksheet, call_with_backoff, read_table, _retry_with_backoff

async def bad():
    shared.sheets.core.fetch_records('s', 't')
    shared.sheets.core.fetch_values('s', 't')
    shared.sheets.core.get_worksheet('s', 't')
    shared.sheets.core.call_with_backoff(lambda: None)
    shared.sheets.core.read_table(sheet_id='s', tab_name='t')
    shared.sheets.core._retry_with_backoff(lambda: None)
    sheets_core.fetch_records('s', 't')
    sheets_core._retry_with_backoff(lambda: None)
    sc.fetch_records('s', 't')
    sc._retry_with_backoff(lambda: None)
    fetch_records('s', 't')
    fv('s', 't')
    get_worksheet('s', 't')
    call_with_backoff(lambda: None)
    read_table(sheet_id='s', tab_name='t')
    _retry_with_backoff(lambda: None)
"""

    violations = _violations_for_source(source)

    assert len(violations) == 16
    assert any("shared.sheets.core.fetch_records" in violation for violation in violations)
    assert any("shared.sheets.core.fetch_values" in violation for violation in violations)
    assert any("shared.sheets.core.get_worksheet" in violation for violation in violations)
    assert any("shared.sheets.core.call_with_backoff" in violation for violation in violations)
    assert any("shared.sheets.core.read_table" in violation for violation in violations)
    assert any("shared.sheets.core._retry_with_backoff" in violation for violation in violations)


def test_runtime_boundary_catches_sync_config_leagues_and_onboarding_helpers() -> None:
    source = """
import shared.sheets.onboarding
from shared.sheets import onboarding as onboarding_sheets
from shared import config as shared_config
from shared.config import merge_onboarding_config_early, _load_onboarding_config_values, _load_milestones_config_values
from modules.community.leagues.config import load_league_bundles
from shared.sheets.onboarding import _read_onboarding_config
from shared.sheets import recruitment

async def bad():
    _read_onboarding_config('sheet')
    shared.sheets.onboarding._read_onboarding_config('sheet')
    onboarding_sheets._read_onboarding_config('sheet')
    shared_config.merge_onboarding_config_early()
    merge_onboarding_config_early()
    _load_onboarding_config_values()
    _load_milestones_config_values()
    load_league_bundles('sheet')
    recruitment.fetch_clans(force=True)
    recruitment.find_clan_row('FIT', force=True)
    recruitment.get_config_value('clans_tab')
    recruitment.get_clan_header_row(force=True)
    recruitment.get_clan_by_tag('FIT')
"""

    violations = _violations_for_source(source)

    assert len(violations) == 13
    assert any("shared.sheets.onboarding._read_onboarding_config" in violation for violation in violations)
    assert any("shared.config.merge_onboarding_config_early" in violation for violation in violations)
    assert any("shared.config._load_onboarding_config_values" in violation for violation in violations)
    assert any("shared.config._load_milestones_config_values" in violation for violation in violations)
    assert any("modules.community.leagues.config.load_league_bundles" in violation for violation in violations)
    assert any("shared.sheets.recruitment.fetch_clans" in violation for violation in violations)
    assert any("shared.sheets.recruitment.find_clan_row" in violation for violation in violations)
    assert any("shared.sheets.recruitment.get_config_value" in violation for violation in violations)
    assert any("shared.sheets.recruitment.get_clan_header_row" in violation for violation in violations)
    assert any("shared.sheets.recruitment.get_clan_by_tag" in violation for violation in violations)


def test_runtime_boundary_rejects_local_asyncio_to_thread_bridges() -> None:
    source = """
import asyncio
from shared.sheets import core as sheets_core

async def bad():
    await asyncio.to_thread(sheets_core.fetch_records, 's', 't')
"""

    violations = _violations_for_source(source)

    assert violations == ["6 calls asyncio.to_thread"]


def test_runtime_boundary_allows_approved_centralized_bridge_modules() -> None:
    source = """
import asyncio
from shared.sheets import core as sheets_core
from shared.sheets import async_core

async def ok():
    await asyncio.to_thread(sheets_core.fetch_records, 's', 't')
    await async_core.a_to_thread_with_backoff(sheets_core.get_worksheet, 's', 't')
"""

    assert _violations_for_source(source, approved_bridge_file=True) == []


def test_runtime_boundary_allows_async_safe_runtime_wrappers() -> None:
    source = """
from shared.sheets.async_core import afetch_records, afetch_values, aget_worksheet, acall_with_backoff
from modules.community.leagues.config import aload_league_bundles
from shared.config import amerge_onboarding_config_early

async def ok():
    await afetch_records('s', 't')
    await afetch_values('s', 't')
    await aget_worksheet('s', 't')
    await acall_with_backoff(lambda: None)
    await aload_league_bundles('s')
    await amerge_onboarding_config_early()
"""

    assert _violations_for_source(source) == []
