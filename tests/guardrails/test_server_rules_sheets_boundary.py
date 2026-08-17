"""Regression guardrails for the Server Rules/FAQ Sheets boundary."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "modules" / "ops" / "server_rules.py"


def _tree() -> ast.Module:
    return ast.parse(SOURCE.read_text(encoding="utf-8"), filename=str(SOURCE))


def _function(name: str) -> ast.AsyncFunctionDef:
    for node in _tree().body:
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name:
            return node
    raise AssertionError(f"missing async function {name}")


def _call_names(node: ast.AST) -> list[str]:
    names: list[str] = []
    for item in ast.walk(node):
        if not isinstance(item, ast.Call):
            continue
        func = item.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            names.append(f"{func.value.id}.{func.attr}")
    return names


def test_server_rules_imports_async_core_not_low_level_adapter() -> None:
    imports: list[tuple[str, str, str | None]] = []
    for node in _tree().body:
        if isinstance(node, ast.ImportFrom) and node.module == "shared.sheets":
            imports.extend((node.module, alias.name, alias.asname) for alias in node.names)

    assert ("shared.sheets", "async_core", "sheets_core") in imports
    assert not any(name == "async_adapter" for _module, name, _alias in imports)
    assert not any(name == "core" for _module, name, _alias in imports)


def test_server_rules_read_is_broker_attributed() -> None:
    node = _function("load_rows")
    calls = [item for item in ast.walk(node) if isinstance(item, ast.Call)]
    fetch = next(
        item
        for item in calls
        if isinstance(item.func, ast.Attribute)
        and isinstance(item.func.value, ast.Name)
        and item.func.value.id == "sheets_core"
        and item.func.attr == "afetch_values"
    )
    keywords = {keyword.arg: keyword.value for keyword in fetch.keywords if keyword.arg}
    assert isinstance(keywords.get("component"), ast.Constant)
    assert keywords["component"].value == "server_rules"
    assert isinstance(keywords.get("reason"), ast.Constant)
    assert keywords["reason"].value == "load_rows"


def test_server_rules_writes_use_async_core_mutation_helper() -> None:
    single = _function("_write_message_id")
    batch = _function("_write_ids_batch")

    assert "sheets_core.acall_with_backoff" in _call_names(single)
    assert "sheets_core.acall_with_backoff" in _call_names(batch)
    assert "async_adapter.aworksheet_values_update" not in _call_names(single)

    write_calls = [item for item in ast.walk(single) if isinstance(item, ast.Call)]
    mutation = next(
        item
        for item in write_calls
        if isinstance(item.func, ast.Attribute)
        and isinstance(item.func.value, ast.Name)
        and item.func.value.id == "sheets_core"
        and item.func.attr == "acall_with_backoff"
    )
    assert mutation.args
    assert isinstance(mutation.args[0], ast.Attribute)
    assert isinstance(mutation.args[0].value, ast.Name)
    assert mutation.args[0].value.id == "ws"
    assert mutation.args[0].attr == "update"
