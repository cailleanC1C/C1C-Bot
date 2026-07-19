"""Guardrail against synchronous Google Sheets helpers in async runtime code."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME_ROOTS = ["cogs", "modules", "shared", "packages/c1c-coreops/src"]
FORBIDDEN_HELPERS = {
    "fetch_records",
    "fetch_values",
    "sheets_read",
    "read_table",
    "call_with_backoff",
    "_retry_with_backoff",
    "get_config_value",
    "get_cached_welcome_templates",
    "_config_lookup",
    "_load_config",
    "get_clans_tab_name",
    "get_role_map_tab_name",
    "get_reports_tab_name",
    "get_reservations_tab_name",
    "get_by_thread_id",
    "load_all",
    "update_existing",
    "upsert_session",
    "mark_completed",
    "missing_columns",
    "get_ticket_finalization_state",
    "get_finalization_headers",
    "get_promo_source_clan_tag_header",
    "reload_config",
}
SCOPED_FORBIDDEN_HELPERS = {
    "get_by_thread_id", "load_all", "update_existing", "upsert_session",
    "mark_completed", "missing_columns", "get_ticket_finalization_state",
    "get_finalization_headers", "get_promo_source_clan_tag_header",
    "reload_config",
}
FORBIDDEN_MODULES = {
    "shared.config",
    "shared.sheets.core",
    "shared.sheets.recruitment",
    "shared.sheets.onboarding",
    "shared.sheets.onboarding_sessions",
}
ASYNC_FACADE_MODULES = {
    "shared.sheets.async_facade",
}
SAFE_ASYNC_CALL_ALLOWLIST = {
    # Compatibility fallback runs sync helpers off-loop via asyncio.to_thread.
    ("modules/recruitment/availability.py", "_aresolve_availability_headers", "get_clans_tab_name"),
    ("modules/recruitment/availability.py", "_aresolve_availability_headers", "get_config_value"),
}


@dataclass(frozen=True)
class ImportAliases:
    async_facade_modules: frozenset[str]
    forbidden_modules: frozenset[str]
    forbidden_functions: dict[str, str]


def _runtime_files() -> list[Path]:
    files: list[Path] = []
    for root in RUNTIME_ROOTS:
        base = ROOT / root
        if base.exists():
            files.extend(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    return sorted(files)


def _join_module(base: str | None, name: str) -> str:
    return f"{base}.{name}" if base else name


def _import_aliases(tree: ast.Module) -> ImportAliases:
    async_facade_modules: set[str] = set()
    forbidden_modules: set[str] = set()
    forbidden_functions: dict[str, str] = {}

    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                local_name = alias.asname or alias.name.split(".", 1)[0]
                if alias.name in ASYNC_FACADE_MODULES:
                    async_facade_modules.add(local_name)
                elif alias.name in FORBIDDEN_MODULES:
                    forbidden_modules.add(local_name)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                if alias.name == "*":
                    continue
                local_name = alias.asname or alias.name
                imported_module = _join_module(module, alias.name)
                if imported_module in ASYNC_FACADE_MODULES:
                    async_facade_modules.add(local_name)
                elif imported_module in FORBIDDEN_MODULES:
                    forbidden_modules.add(local_name)
                elif module in FORBIDDEN_MODULES and alias.name in FORBIDDEN_HELPERS:
                    forbidden_functions[local_name] = alias.name
    return ImportAliases(
        async_facade_modules=frozenset(async_facade_modules),
        forbidden_modules=frozenset(forbidden_modules),
        forbidden_functions=forbidden_functions,
    )


def _forbidden_call_name(call: ast.Call, aliases: ImportAliases) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return aliases.forbidden_functions.get(func.id)
    if isinstance(func, ast.Attribute) and func.attr in FORBIDDEN_HELPERS:
        if isinstance(func.value, ast.Name):
            module_name = func.value.id
            if module_name in aliases.async_facade_modules:
                return None
            if module_name in aliases.forbidden_modules:
                return func.attr
        if (
            func.attr == "reload_config"
            and isinstance(func.value, ast.Attribute)
            and func.value.attr == "config"
            and isinstance(func.value.value, ast.Name)
            and func.value.value.id in aliases.forbidden_modules
        ):
            return func.attr
        if func.attr in SCOPED_FORBIDDEN_HELPERS:
            return None
        return func.attr
    return None


def test_async_runtime_does_not_call_sync_sheets_helpers() -> None:
    failures: list[str] = []
    for path in _runtime_files():
        rel = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(), filename=rel)
        aliases = _import_aliases(tree)
        for fn in [n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef)]:
            for call in [n for n in ast.walk(fn) if isinstance(n, ast.Call)]:
                name = _forbidden_call_name(call, aliases)
                if name is None:
                    continue
                if (rel, fn.name, name) in SAFE_ASYNC_CALL_ALLOWLIST:
                    continue
                failures.append(
                    f"{rel}:{call.lineno} in async {fn.name}() calls sync "
                    f"{name}(); use the async Sheets helper instead"
                )
    assert not failures, (
        "Synchronous Google Sheets helpers are forbidden in async "
        "Discord/runtime paths:\n" + "\n".join(failures)
    )


def test_guardrail_detects_forbidden_import_alias_inside_async_function() -> None:
    tree = ast.parse(
        "from shared.sheets.core import fetch_records as sheet_fetch_records\n"
        "async def command():\n"
        "    return sheet_fetch_records('sheet', 'tab')\n"
    )
    aliases = _import_aliases(tree)
    command = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef))
    call = next(n for n in ast.walk(command) if isinstance(n, ast.Call))

    assert _forbidden_call_name(call, aliases) == "fetch_records"


def test_guardrail_detects_sync_config_reload_inside_async_function() -> None:
    calls = _forbidden_calls(
        "from shared import config as cfg\n"
        "async def command():\n"
        "    cfg.reload_config()\n"
    )

    assert calls == ["reload_config"]


def test_guardrail_detects_directly_imported_sync_config_reload() -> None:
    calls = _forbidden_calls(
        "from shared.config import reload_config\n"
        "async def command():\n"
        "    reload_config()\n"
    )

    assert calls == ["reload_config"]


def test_guardrail_detects_fully_qualified_sync_config_reload() -> None:
    calls = _forbidden_calls(
        "import shared.config\n"
        "async def command():\n"
        "    shared.config.reload_config()\n"
    )

    assert calls == ["reload_config"]


def test_guardrail_detects_sync_config_reload_in_local_async_wrapper() -> None:
    calls = _forbidden_calls(
        "from shared import config as cfg\n"
        "async def reload_wrapper():\n"
        "    cfg.reload_config()\n"
    )

    assert calls == ["reload_config"]


def _forbidden_calls(source: str) -> list[str]:
    tree = ast.parse(source)
    aliases = _import_aliases(tree)
    command = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef))
    return [
        name
        for call in ast.walk(command)
        if isinstance(call, ast.Call)
        and (name := _forbidden_call_name(call, aliases)) is not None
    ]


def test_guardrail_detects_onboarding_sessions_module_alias_calls() -> None:
    calls = _forbidden_calls(
        "import shared.sheets.onboarding_sessions as sessions\n"
        "async def listener():\n"
        "    sessions.get_by_thread_id(1)\n"
        "    sessions.load_all()\n"
        "    sessions.update_existing(1, {})\n"
        "    sessions.upsert_session(thread_id=1, thread_name='W1-user', user_id=2)\n"
        "    sessions.mark_completed(1)\n"
        "    sessions.missing_columns({'thread_id'})\n"
    )

    assert calls == [
        "get_by_thread_id",
        "load_all",
        "update_existing",
        "upsert_session",
        "mark_completed",
        "missing_columns",
    ]


def test_guardrail_detects_direct_imported_onboarding_session_helpers() -> None:
    calls = _forbidden_calls(
        "from shared.sheets.onboarding_sessions import "
        "get_by_thread_id as find_session, update_existing\n"
        "async def listener():\n"
        "    find_session(1)\n"
        "    update_existing(1, {})\n"
    )

    assert calls == ["get_by_thread_id", "update_existing"]


def test_guardrail_detects_onboarding_finalization_alias_and_direct_calls() -> None:
    calls = _forbidden_calls(
        "from shared.sheets import onboarding as onboarding_sheets\n"
        "from shared.sheets.onboarding import get_finalization_headers as headers\n"
        "async def listener():\n"
        "    onboarding_sheets.get_ticket_finalization_state('welcome', {})\n"
        "    onboarding_sheets.get_promo_source_clan_tag_header()\n"
        "    headers('welcome')\n"
    )

    assert calls == [
        "get_ticket_finalization_state",
        "get_promo_source_clan_tag_header",
        "get_finalization_headers",
    ]


def test_async_facade_alias_is_the_only_allowed_sheets_alias() -> None:
    async_tree = ast.parse(
        "from shared.sheets import async_facade as sheets\n"
        "async def command():\n"
        "    return await sheets.fetch_records('sheet', 'tab')\n"
    )
    core_tree = ast.parse(
        "import shared.sheets.core as sheets\n"
        "async def command():\n"
        "    return sheets.fetch_records('sheet', 'tab')\n"
    )

    async_aliases = _import_aliases(async_tree)
    core_aliases = _import_aliases(core_tree)
    async_call = next(n for n in ast.walk(async_tree) if isinstance(n, ast.Call))
    core_call = next(n for n in ast.walk(core_tree) if isinstance(n, ast.Call))

    assert _forbidden_call_name(async_call, async_aliases) is None
    assert _forbidden_call_name(core_call, core_aliases) == "fetch_records"


def test_guardrail_detects_sync_cached_welcome_template_loader() -> None:
    tree = ast.parse(
        "from shared.sheets import recruitment as sheets\n"
        "async def command():\n"
        "    return sheets.get_cached_welcome_templates()\n"
    )
    aliases = _import_aliases(tree)
    command = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef))
    call = next(n for n in ast.walk(command) if isinstance(n, ast.Call))

    assert _forbidden_call_name(call, aliases) == "get_cached_welcome_templates"


def test_whoweare_regression_uses_async_role_map_tab_lookup() -> None:
    path = ROOT / "cogs/app_admin.py"
    tree = ast.parse(path.read_text(), filename="cogs/app_admin.py")
    whoweare = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "whoweare"
    )
    called = {
        call.func.attr
        for call in ast.walk(whoweare)
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
    }
    assert "get_role_map_tab_name" not in called
    assert "get_role_map_tab_name_async" in called
