"""Architectural guardrail for the Google Sheets low-level access boundary.

This guardrail intentionally targets imports first: runtime modules must not gain
new direct gspread or async-adapter access. Existing worksheet-handle read sites
are migrated separately before method-level enforcement is tightened.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOTS = ("cogs", "modules", "shared", "packages/c1c-coreops/src")

GSPREAD_IMPORT_ALLOWLIST = {
    "shared/sheets/core.py",
}
ASYNC_ADAPTER_IMPORT_ALLOWLIST = {
    "shared/sheets/core.py",
    "shared/sheets/async_core.py",
    "shared/sheets/async_facade.py",
}


def _runtime_files() -> list[Path]:
    files: list[Path] = []
    for root in RUNTIME_ROOTS:
        base = ROOT / root
        if not base.exists():
            continue
        files.extend(path for path in base.rglob("*.py") if "__pycache__" not in path.parts)
    return sorted(files)


def _imports_gspread(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "gspread" or alias.name.startswith("gspread.") for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "gspread" or module.startswith("gspread."):
                return True
    return False


def _imports_async_adapter(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name == "shared.sheets.async_adapter" for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "shared.sheets.async_adapter":
                return True
            if module == "shared.sheets" and any(alias.name == "async_adapter" for alias in node.names):
                return True
    return False


def test_runtime_does_not_import_gspread_outside_core_boundary() -> None:
    failures: list[str] = []
    for path in _runtime_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in GSPREAD_IMPORT_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(), filename=rel)
        if _imports_gspread(tree):
            failures.append(rel)

    assert not failures, (
        "Direct gspread imports are forbidden outside shared/sheets/core.py; "
        "route reads through shared.sheets.async_core/the broker instead:\n"
        + "\n".join(failures)
    )


def test_runtime_does_not_import_async_adapter_outside_low_level_boundary() -> None:
    failures: list[str] = []
    for path in _runtime_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel in ASYNC_ADAPTER_IMPORT_ALLOWLIST:
            continue
        tree = ast.parse(path.read_text(), filename=rel)
        if _imports_async_adapter(tree):
            failures.append(rel)

    assert not failures, (
        "Direct shared.sheets.async_adapter imports are forbidden outside the "
        "approved low-level boundary; brokered reads must go through async_core:\n"
        + "\n".join(failures)
    )


def test_guardrail_detects_forbidden_import_shapes() -> None:
    assert _imports_gspread(ast.parse("import gspread\n"))
    assert _imports_gspread(ast.parse("from gspread.exceptions import APIError\n"))
    assert _imports_async_adapter(ast.parse("import shared.sheets.async_adapter as adapter\n"))
    assert _imports_async_adapter(ast.parse("from shared.sheets import async_adapter\n"))
