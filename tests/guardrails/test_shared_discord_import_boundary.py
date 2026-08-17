"""S-02 dependency guardrail for Discord imports under shared/.

Three pre-existing shared helpers still own Discord-specific behavior.  They are
explicitly frozen as legacy debt: new shared Discord imports fail CI, and removing
one of the old imports requires removing its allowlist entry too.
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SHARED_ROOT = ROOT / "shared"
LEGACY_DISCORD_IMPORT_ALLOWLIST = {
    "shared/logfmt.py",
    "shared/redaction.py",
    "shared/theme.py",
}


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


def test_shared_runtime_has_no_new_discord_imports() -> None:
    observed_legacy: set[str] = set()
    failures: list[str] = []
    for path in sorted(SHARED_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(ROOT).as_posix()
        lines = _discord_import_lines(path)
        if not lines:
            continue
        if rel in LEGACY_DISCORD_IMPORT_ALLOWLIST:
            observed_legacy.add(rel)
            continue
        failures.extend(f"{rel}:{line}" for line in lines)

    assert not failures, (
        "New Discord-specific imports are forbidden in shared/; move the dependency "
        "to modules/cogs or pass an abstraction into shared code:\n"
        + "\n".join(failures)
    )
    assert observed_legacy == LEGACY_DISCORD_IMPORT_ALLOWLIST, (
        "S-02 legacy allowlist is stale. Remove entries as shared Discord dependencies "
        "are migrated: "
        f"expected={sorted(LEGACY_DISCORD_IMPORT_ALLOWLIST)} "
        f"observed={sorted(observed_legacy)}"
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
