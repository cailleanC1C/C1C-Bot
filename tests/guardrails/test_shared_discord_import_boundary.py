"""S-02 dependency guardrail: shared/ must not import Discord runtime modules."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHARED_ROOT = ROOT / "shared"


def _discord_import_lines(path: Path) -> list[int]:
    rel = path.relative_to(ROOT).as_posix()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == "discord" or alias.name.startswith("discord.")
                for alias in node.names
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "discord" or module.startswith("discord."):
                lines.append(node.lineno)
    return sorted(lines)


def test_shared_runtime_does_not_import_discord() -> None:
    failures: list[str] = []
    for path in sorted(SHARED_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        for line in _discord_import_lines(path):
            failures.append(f"{path.relative_to(ROOT).as_posix()}:{line}")

    assert not failures, (
        "Discord-specific imports are forbidden in shared/; move the dependency "
        "to modules/cogs or pass an abstraction into shared code:\n"
        + "\n".join(failures)
    )


def test_s02_detects_imports_but_not_plain_text() -> None:
    tree = ast.parse(
        'COMPONENT = "discord"\n'
        'import discord as d\n'
        'from discord.ext import commands\n'
    )
    imports: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                alias.name == "discord" or alias.name.startswith("discord.")
                for alias in node.names
            ):
                imports.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "discord" or module.startswith("discord."):
                imports.append(node.lineno)

    assert imports == [2, 3]
