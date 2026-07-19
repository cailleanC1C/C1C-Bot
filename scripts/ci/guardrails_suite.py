"""Repository guardrails enforcement suite.

This script implements automatable guardrails from ``docs/guardrails/RepositoryGuardrails.md``
and produces both a human-readable audit report and a machine-readable summary that
GitHub Actions can surface in PR comments.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import json
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

from scripts.ci.utils.env import get_env, get_env_path


ROOT = Path(__file__).resolve().parents[1]
AUDIT_ROOT = ROOT / "AUDIT"
DOCS_ROOT = ROOT / "docs"

ALLOWED_EMBED_COLORS = {0xF200E5, 0x1B8009, 0x3498DB}


log = logging.getLogger(__name__)


@dataclass
class Violation:
    rule_id: str
    severity: str  # "error" or "warning"
    message: str
    files: List[str] = field(default_factory=list)


@dataclass
class CategoryResult:
    identifier: str
    violations: List[Violation] = field(default_factory=list)

    @property
    def status(self) -> str:
        if any(v.severity == "error" for v in self.violations):
            return "fail"
        if self.violations:
            return "warn"
        return "pass"

    def add(self, violation: Violation) -> None:
        self.violations.append(violation)


@dataclass
class CheckResult:
    code: str
    description: str
    status: str  # one of "pass", "fail", "skip"
    violations: List[Violation] = field(default_factory=list)
    reason: Optional[str] = None


@dataclass
class GuardrailCheck:
    code: str
    description: str
    runner: Callable[["GuardrailContext"], CheckResult]


@dataclass
class GuardrailContext:
    diff_status: Dict[str, str]
    changed_files: List[str]
    pr_body: str
    parity_status: Optional[str]
    pr_number: int
    cache: Dict[str, Any] = field(default_factory=dict)


@dataclass
class SuiteResult:
    check_results: List[CheckResult]
    categories: Dict[str, CategoryResult]
    violations: List[Violation]


def _iter_python_files() -> Iterable[Path]:
    for path in ROOT.rglob("*.py"):
        rel = path.relative_to(ROOT)
        if "AUDIT" in rel.parts:
            continue
        yield path


def _iter_runtime_python_files() -> Iterable[Path]:
    for path in _iter_python_files():
        rel = path.relative_to(ROOT).as_posix()
        if rel.startswith("tests/"):
            continue
        if rel.startswith("scripts/"):
            continue
        yield path


def _iter_markdown_files() -> Iterable[Path]:
    for path in DOCS_ROOT.rglob("*.md"):
        if "AUDIT" in path.parts:
            continue
        yield path


def _collect_category_violations(
    context: GuardrailContext, key: str, checker: Callable[..., None], *args: object
) -> List[Violation]:
    if key in context.cache:
        cached = context.cache[key]
        return list(cached) if isinstance(cached, list) else []
    category = CategoryResult(key)
    checker(category, *args)
    context.cache[key] = category.violations
    return category.violations


def _status_from_violations(violations: List[Violation]) -> str:
    return "fail" if violations else "pass"


def _git_diff_names(base_ref: Optional[str]) -> List[str]:
    if base_ref:
        ref = base_ref
    else:
        ref = "HEAD^"
    cmd = ["git", "diff", "--name-only", f"{ref}..HEAD"]
    try:
        output = subprocess.check_output(cmd, cwd=ROOT, text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return []
    return [line.strip() for line in output.splitlines() if line.strip()]


def _git_diff_status(base_ref: Optional[str]) -> Dict[str, str]:
    if base_ref:
        ref = base_ref
    else:
        ref = "HEAD^"
    cmd = ["git", "diff", "--name-status", f"{ref}..HEAD"]
    try:
        output = subprocess.check_output(cmd, cwd=ROOT, text=True, stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return {}
    mapping: Dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        status, path = line.split("\t", 1)
        mapping[path] = status
    return mapping


def _load_pr_body(event_path: Optional[str]) -> str:
    if not event_path:
        return ""
    try:
        data = json.loads(Path(event_path).read_text(encoding="utf-8"))
    except Exception:
        return ""
    return data.get("pull_request", {}).get("body", "") or ""


def _match_paths(paths: Iterable[Path], pattern: re.Pattern[str]) -> List[str]:
    hits: List[str] = []
    for path in paths:
        rel = path.relative_to(ROOT).as_posix()
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for idx, line in enumerate(lines, 1):
            if pattern.search(line):
                hits.append(f"{rel}:{idx}")
    return hits


def _env_keys_from_example(path: Path) -> List[str]:
    keys: List[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return keys
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key = stripped.split("=", 1)[0].strip()
            if key:
                keys.append(key)
    return keys


def _env_keys_from_docs(path: Path) -> List[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    marker = "## Environment keys"
    start = text.find(marker)
    if start == -1:
        return []
    section = text[start + len(marker) :]
    next_heading = section.find("\n## ")
    if next_heading != -1:
        section = section[:next_heading]
    table_keys = set(
        re.findall(r"^\|\s*`([A-Z][A-Z0-9_]+)`\s*\|", section, flags=re.MULTILINE)
    )
    inline_keys = {
        match
        for match in re.findall(r"`([A-Z][A-Z0-9_]+)`", section)
        if "_" in match
    }
    keys = table_keys.union(inline_keys)
    return sorted(keys)


def _check_discord_token_leak(repo_root: Path) -> List[str]:
    pattern = re.compile(r"[A-Za-z\d_-]{23,28}\.[A-Za-z\d_-]{6}\.[A-Za-z\d_-]{27}")
    offenders: List[str] = []
    for path in repo_root.rglob("*"):
        if path.is_dir():
            continue
        if any(part.lower() == "audit" for part in path.parts):
            continue
        if any(part == ".git" for part in path.parts):
            continue
        if path.suffix.lower() in {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".mp4",
            ".pdf",
            ".ico",
            ".svg",
            ".csv",
            ".tsv",
            ".parquet",
            ".feather",
        }:
            continue
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if pattern.search(content):
            offenders.append(str(path.relative_to(ROOT)))
    return offenders


def _compute_guardrail_health() -> Tuple[Optional[str], Optional[str], Optional[str]]:
    env_example = DOCS_ROOT / "ops" / ".env.example"
    config_md = DOCS_ROOT / "ops" / "Config.md"

    example_keys = _env_keys_from_example(env_example)
    doc_keys = _env_keys_from_docs(config_md)

    config_status: Optional[str]
    parity_status: Optional[str]
    if example_keys and doc_keys:
        example_set = set(example_keys)
        doc_set = set(doc_keys)
        missing_in_docs = sorted(example_set - doc_set)
        missing_in_example = sorted(doc_set - example_set)
        if missing_in_docs or missing_in_example:
            config_status = "failure"
            parity_status = "failure"
        else:
            config_status = "success"
            parity_status = "success"
    else:
        config_status = None
        parity_status = None

    offenders = _check_discord_token_leak(ROOT)
    secret_status: Optional[str]
    if offenders:
        secret_status = "failure"
    else:
        secret_status = "success"

    return config_status, secret_status, parity_status


def check_s01_s05(category: CategoryResult) -> None:
    stray_domains = []
    candidates = {"recruitment", "onboarding", "placement", "community", "ops"}
    for candidate in candidates:
        path = ROOT / candidate
        if path.exists() and path.is_dir():
            stray_domains.append(path.relative_to(ROOT).as_posix())
    if stray_domains:
        category.add(Violation("S-01/S-05", "error", "Feature domains must live under modules/", stray_domains))


def check_s02(category: CategoryResult) -> None:
    pattern = re.compile(r"\bdiscord(\.ext\.commands)?")
    shared_files = [p for p in _iter_python_files() if p.relative_to(ROOT).as_posix().startswith("shared/")]
    hits = _match_paths(shared_files, pattern)
    if hits:
        category.add(Violation("S-02", "error", "Discord-specific imports not allowed in shared/", hits))


def check_s03(category: CategoryResult) -> None:
    pattern = re.compile(r"@(commands\.|bot\.|tree\.|app_commands\.)command")
    hits = _match_paths(_iter_python_files(), pattern)
    filtered = [h for h in hits if not h.startswith("cogs/") and not h.startswith("tests/")]
    if filtered:
        category.add(Violation("S-03", "error", "Command decorators must live under cogs/", filtered))


def check_s07_s09(category: CategoryResult, diff_status: Dict[str, str]) -> None:
    bad_audits: List[str] = []
    audit_re = re.compile(r"^AUDIT/\d{8}_.+")
    for path, status in diff_status.items():
        if not path.startswith("AUDIT/"):
            continue
        if status.upper() == "A" and not audit_re.match(path):
            bad_audits.append(path)
    if bad_audits:
        category.add(Violation("S-07", "error", "Audit artifacts must live under AUDIT/<YYYYMMDD>_*/", bad_audits))

    import_pattern = re.compile(r"from\s+AUDIT|import\s+AUDIT")
    runtime_files = list(_iter_runtime_python_files())
    hits = _match_paths(runtime_files, import_pattern)
    if hits:
        category.add(Violation("S-09", "error", "Runtime code must not import AUDIT modules", hits))


def check_s08(category: CategoryResult) -> None:
    bases = [ROOT / "modules", ROOT / "shared", ROOT / "coreops"]
    missing: List[str] = []
    for base in bases:
        if not base.exists():
            continue
        for directory in base.rglob("*"):
            if not directory.is_dir():
                continue
            if any(part == "__pycache__" for part in directory.parts):
                continue
            if not any(child.suffix == ".py" for child in directory.iterdir() if child.is_file()):
                continue
            init_file = directory / "__init__.py"
            if not init_file.exists():
                missing.append(directory.relative_to(ROOT).as_posix())
    if missing:
        category.add(Violation("S-08", "error", "Packages must include __init__.py", missing))


def check_c02(category: CategoryResult) -> None:
    pattern = re.compile(r"\bprint\(")
    runtime_files = [p for p in _iter_python_files() if not p.relative_to(ROOT).as_posix().startswith(("scripts/", "tests/"))]
    hits = _match_paths(runtime_files, pattern)
    if hits:
        category.add(Violation("C-02", "error", "Use logger instead of print()", hits))


def check_c03(category: CategoryResult) -> None:
    pattern = re.compile(r"from \.+")
    runtime_files = list(_iter_runtime_python_files())
    hits = _match_paths(runtime_files, pattern)
    if hits:
        category.add(Violation("C-03", "error", "Parent-relative imports are forbidden", hits))


def check_c09(category: CategoryResult) -> None:
    patterns = [re.compile(r"shared\.utils\.coreops_"), re.compile(r"recruitment/"), re.compile(r"onboarding/")]
    runtime_files = list(_iter_runtime_python_files())
    hits: List[str] = []
    for path in runtime_files:
        rel = path.relative_to(ROOT).as_posix()
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for idx, line in enumerate(lines, 1):
            if any(pat.search(line) for pat in patterns):
                hits.append(f"{rel}:{idx}")
    if hits:
        category.add(Violation("C-09", "error", "Legacy paths must not be used", hits))


def check_c10(category: CategoryResult) -> None:
    pattern = re.compile(r"os\.getenv|os\.environ\[")
    allowed_prefixes = ("shared/config", "scripts/", "tests/")
    runtime_files = [
        p
        for p in _iter_runtime_python_files()
        if not p.relative_to(ROOT).as_posix().startswith(allowed_prefixes)
    ]
    hits = _match_paths(runtime_files, pattern)
    if hits:
        category.add(Violation("C-10", "warning", "Use shared config accessor instead of ad-hoc env reads", hits))


def check_c11(category: CategoryResult) -> None:
    pattern = re.compile(r"get_port")
    hits = _match_paths(_iter_runtime_python_files(), pattern)
    hits = [h for h in hits if "check_forbidden_imports" not in h]
    if hits:
        category.add(Violation("C-11", "error", "Forbidden get_port import detected", hits))


def _extract_embed_color_violations() -> List[str]:
    offenders: List[str] = []
    for path in _iter_python_files():
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        class EmbedVisitor(ast.NodeVisitor):
            def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
                if isinstance(node.func, ast.Attribute) and node.func.attr == "Embed":
                    for kw in node.keywords:
                        if kw.arg in {"color", "colour"}:
                            if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, int):
                                if kw.value.value not in ALLOWED_EMBED_COLORS:
                                    offenders.append(f"{rel}:{kw.value.lineno}")
                            elif isinstance(kw.value, ast.Name):
                                name = kw.value.id
                                if not re.match(r"[A-Z0-9_]*COLOR", name):
                                    offenders.append(f"{rel}:{kw.value.lineno}")
                self.generic_visit(node)
        EmbedVisitor().visit(tree)
    return offenders


def check_c17(category: CategoryResult) -> None:
    offenders = _extract_embed_color_violations()
    if offenders:
        category.add(Violation("C-17", "error", "Embed colours must use approved palette", offenders))


def _is_feature_accessor(node: ast.Attribute) -> bool:
    if isinstance(node.value, ast.Name) and node.value.id in {"feature_flags", "features"}:
        return True
    if isinstance(node.value, ast.Attribute) and node.value.attr in {"feature_flags", "features"}:
        return True
    return False


def _extract_toggle_name(node: ast.Call) -> Optional[str]:
    for arg in node.args:
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            return arg.value.strip().lower()
    for kw in node.keywords:
        if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value.strip().lower()
    return None


def _extract_toggle_usage() -> Dict[str, List[str]]:
    usage: Dict[str, List[str]] = {}

    for path in _iter_runtime_python_files():
        rel = path.relative_to(ROOT).as_posix()
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue

        class ToggleVisitor(ast.NodeVisitor):
            def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
                if isinstance(node.func, ast.Attribute) and node.func.attr == "is_enabled":
                    if _is_feature_accessor(node.func):
                        name = _extract_toggle_name(node)
                        if name:
                            usage.setdefault(name, []).append(f"{rel}:{node.lineno}")
                self.generic_visit(node)

        ToggleVisitor().visit(tree)

    return usage


def _load_feature_toggle_names() -> set[str]:
    try:
        import modules.common.feature_flags as features
    except Exception as exc:  # pragma: no cover - import errors vary by env
        log.warning("⚠️ Unable to import feature_flags for F-04: %s", exc)
        return set()

    try:
        asyncio.run(features.refresh())
    except Exception as exc:  # pragma: no cover - runtime fetch failures are environment-dependent
        log.warning("⚠️ Feature toggle refresh failed: %s", exc)

    try:
        values = features.values()
    except Exception as exc:  # pragma: no cover - defensive guard
        log.warning("⚠️ Unable to read feature toggle values: %s", exc)
        return set()

    toggles: set[str] = set()
    for key in values:
        normalized = str(key or "").strip().lower()
        if normalized:
            toggles.add(normalized)
    return toggles


def _collect_feature_toggle_violations(
    context: Optional[GuardrailContext] = None,
) -> tuple[List[Violation], Optional[str]]:
    cache_key = "feature_toggle_violations"
    if context and cache_key in context.cache:
        return (
            list(context.cache.get(cache_key, [])),
            context.cache.get("feature_toggle_skip_reason"),
        )

    documented = _load_feature_toggle_names()
    if not documented:
        log.warning("⚠️ Feature toggle registry empty; skipping F-01/F-04 guardrails.")
        if context is not None:
            context.cache[cache_key] = []
            context.cache["feature_toggle_skip_reason"] = "feature toggle registry unavailable"
        return [], "feature toggle registry unavailable"

    usage = _extract_toggle_usage()
    violations: List[Violation] = []

    undocumented = [name for name in usage if name not in documented]
    if undocumented:
        files: List[str] = []
        for toggle in undocumented:
            files.extend(usage.get(toggle, []))
        violations.append(Violation("F-01", "error", "Toggle used but not documented", files))

    unused_documented = [name for name in documented if name not in usage]
    if unused_documented:
        violations.append(
            Violation(
                "F-04",
                "warning",
                "Documented toggles not referenced in code",
                sorted(unused_documented),
            )
        )

    if context is not None:
        context.cache[cache_key] = violations
        context.cache["feature_toggle_skip_reason"] = None
    return violations, None


def check_feature_toggles(category: CategoryResult, context: Optional[GuardrailContext] = None) -> None:
    violations, _ = _collect_feature_toggle_violations(context)
    for violation in violations:
        category.add(violation)


def check_d01(category: CategoryResult) -> None:
    header_re = re.compile(r"^# +(.+)$")
    violations: List[str] = []
    for path in _iter_markdown_files():
        rel = path.relative_to(ROOT).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            match = header_re.match(line)
            if match:
                if "phase" in match.group(1).lower():
                    violations.append(rel)
                break
    if violations:
        category.add(Violation("D-01", "error", "Doc titles must not include 'Phase'", violations))


def check_d02(category: CategoryResult) -> None:
    footer_re = re.compile(r"^Doc last updated: \d{4}-\d{2}-\d{2} \(v0\.9\.8\.\d+\)$")
    violations: List[str] = []
    for path in _iter_markdown_files():
        rel = path.relative_to(ROOT).as_posix()
        lines = path.read_text(encoding="utf-8").splitlines()
        for line in reversed(lines):
            if line.strip():
                if not footer_re.match(line.strip()):
                    violations.append(rel)
                break
    if violations:
        category.add(Violation("D-02", "error", "Docs must end with standard footer", violations))


def check_d04_d08(category: CategoryResult) -> None:
    readme = DOCS_ROOT / "README.md"
    if not readme.exists():
        category.add(Violation("D-04", "error", "docs/README.md missing", []))
        return
    text = readme.read_text(encoding="utf-8")
    link_re = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
    linked: set[str] = set()
    for match in link_re.finditer(text):
        target = match.group(1)
        if target.startswith(("http://", "https://", "mailto:", "tel:", "#")):
            continue
        resolved = (readme.parent / target).resolve()
        try:
            rel = resolved.relative_to(DOCS_ROOT).as_posix()
        except ValueError:
            continue
        linked.add(rel)

    all_docs = {path.relative_to(DOCS_ROOT).as_posix() for path in _iter_markdown_files()}
    all_docs.discard("README.md")
    missing = sorted(all_docs - linked)
    if missing:
        category.add(Violation("D-04/D-08", "error", "Docs must be linked from docs/README.md", missing))


def check_d05(category: CategoryResult) -> None:
    adr_dir = DOCS_ROOT / "adr"
    bad: List[str] = []
    if adr_dir.exists():
        for path in adr_dir.glob("*.md"):
            if not re.match(r"ADR-\d{4}\.md", path.name):
                bad.append(path.relative_to(ROOT).as_posix())
    if bad:
        category.add(Violation("D-05", "error", "ADR files must follow ADR-XXXX.md naming", bad))


def check_d06(category: CategoryResult, diff_status: Dict[str, str]) -> None:
    new_audits = [path for path, status in diff_status.items() if path.startswith("AUDIT/") and status.upper() == "A"]
    if not new_audits:
        return
    changelog = ROOT / "CHANGELOG.md"
    if not changelog.exists():
        category.add(Violation("D-06", "error", "CHANGELOG.md missing for new audit entries", new_audits))
        return
    content = changelog.read_text(encoding="utf-8")
    missing: List[str] = []
    for audit_path in new_audits:
        if audit_path not in content:
            missing.append(audit_path)
    if missing:
        category.add(Violation("D-06", "error", "CHANGELOG must reference new AUDIT entries", missing))


def _parse_block(body: str, label: str) -> Optional[str]:
    pattern = re.compile(rf"{label}:\s*(.+)", re.IGNORECASE)
    match = pattern.search(body)
    if match:
        return match.group(1).strip()
    return None


def check_d09(category: CategoryResult, changed_files: List[str], pr_body: str) -> None:
    runtime_touched = any(f.startswith(("modules/", "shared/", "coreops/")) for f in changed_files)
    tests_touched = any(f.startswith("tests/") for f in changed_files)
    if not runtime_touched:
        return
    tests_block = _parse_block(pr_body, "Tests") or ""
    if tests_touched or tests_block:
        return
    category.add(Violation("D-09", "error", "Runtime changes require tests or Tests: declaration", changed_files))


def check_d10(category: CategoryResult, changed_files: List[str], pr_body: str) -> None:
    user_flow_change = any(f.startswith("cogs/") or f.startswith("modules/") for f in changed_files)
    docs_changed = any(f.startswith("docs/") for f in changed_files)
    if not user_flow_change:
        return
    docs_block = _parse_block(pr_body, "Docs") or ""
    if docs_changed or docs_block:
        return
    category.add(Violation("D-10", "error", "User-facing changes require docs or Docs: declaration", changed_files))


def _parity_violation(parity_status: Optional[str]) -> tuple[List[Violation], Optional[str]]:
    if parity_status is None:
        return [], "ENV parity status unavailable"

    normalized = parity_status.lower()
    if normalized == "failure":
        return [Violation("D-03", "error", "ENV parity check failed", [])], None

    if normalized == "success":
        return [], None

    return [], f"ENV parity status reported as '{parity_status}'"


def check_g03(category: CategoryResult, pr_body: str) -> None:
    meta_block = re.search(r"\[meta\](.*?)\[/meta\]", pr_body, re.DOTALL | re.IGNORECASE)
    if not meta_block:
        category.add(Violation("G-03", "error", "PR body missing [meta] block with labels and milestone", []))
        return
    block = meta_block.group(1)
    if "labels:" not in block or "milestone:" not in block:
        category.add(Violation("G-03", "error", "[meta] block must include labels and milestone", []))


def check_g06(category: CategoryResult, diff_status: Dict[str, str]) -> None:
    added_docs = [path for path, status in diff_status.items() if status.upper() == "A" and path.startswith("docs/")]
    bad: List[str] = []
    for doc in added_docs:
        name = Path(doc).name
        if " " in name or "Phase" in name:
            bad.append(doc)
        elif not re.match(r"[a-z0-9_]+\.md", name):
            bad.append(doc)
    if bad:
        category.add(Violation("G-06", "error", "New docs must be lower_snake_case and avoid Phase", bad))


def check_g09(category: CategoryResult, pr_body: str) -> None:
    tests_block = _parse_block(pr_body, "Tests")
    docs_block = _parse_block(pr_body, "Docs")
    if not tests_block or not docs_block:
        category.add(Violation("G-09", "error", "PR body must declare Tests: and Docs: sections", []))


def _build_guardrail_checks() -> List[GuardrailCheck]:
    return [
        GuardrailCheck(
            "S-01",
            "Modules-first: feature domains live under modules/",
            lambda ctx: _build_check_from_violations(
                "S-01",
                "Modules-first: feature domains live under modules/",
                [v for v in _collect_category_violations(ctx, "s01_s05", check_s01_s05) if "S-01" in v.rule_id],
            ),
        ),
        GuardrailCheck(
            "S-05",
            "Single home per domain",
            lambda ctx: _build_check_from_violations(
                "S-05",
                "Single home per domain",
                [v for v in _collect_category_violations(ctx, "s01_s05", check_s01_s05) if "S-05" in v.rule_id or "S-01/S-05" in v.rule_id],
            ),
        ),
        GuardrailCheck(
            "S-02",
            "Discord-specific imports not allowed in shared/",
            lambda ctx: _build_check_from_violations(
                "S-02",
                "Discord-specific imports not allowed in shared/",
                _collect_category_violations(ctx, "s02", check_s02),
            ),
        ),
        GuardrailCheck(
            "S-03",
            "Command decorators must live under cogs/",
            lambda ctx: _build_check_from_violations(
                "S-03",
                "Command decorators must live under cogs/",
                _collect_category_violations(ctx, "s03", check_s03),
            ),
        ),
        GuardrailCheck(
            "S-07",
            "Audits must live under AUDIT/<YYYYMMDD>_*/",
            lambda ctx: _build_check_from_violations(
                "S-07",
                "Audits must live under AUDIT/<YYYYMMDD>_*/",
                [
                    v
                    for v in _collect_category_violations(ctx, "s07_s09", check_s07_s09, ctx.diff_status)
                    if v.rule_id == "S-07"
                ],
            ),
        ),
        GuardrailCheck(
            "S-08",
            "Packages must include __init__.py",
            lambda ctx: _build_check_from_violations(
                "S-08",
                "Packages must include __init__.py",
                _collect_category_violations(ctx, "s08", check_s08),
            ),
        ),
        GuardrailCheck(
            "S-09",
            "Runtime code must not import AUDIT modules",
            lambda ctx: _build_check_from_violations(
                "S-09",
                "Runtime code must not import AUDIT modules",
                [
                    v
                    for v in _collect_category_violations(ctx, "s07_s09", check_s07_s09, ctx.diff_status)
                    if v.rule_id == "S-09"
                ],
            ),
        ),
        GuardrailCheck(
            "C-02",
            "Use logger instead of print()",
            lambda ctx: _build_check_from_violations(
                "C-02",
                "Use logger instead of print()",
                _collect_category_violations(ctx, "c02", check_c02),
            ),
        ),
        GuardrailCheck(
            "C-03",
            "Parent-relative imports are forbidden",
            lambda ctx: _build_check_from_violations(
                "C-03",
                "Parent-relative imports are forbidden",
                _collect_category_violations(ctx, "c03", check_c03),
            ),
        ),
        GuardrailCheck(
            "C-09",
            "No imports from removed legacy paths",
            lambda ctx: _build_check_from_violations(
                "C-09",
                "No imports from removed legacy paths",
                _collect_category_violations(ctx, "c09", check_c09),
            ),
        ),
        GuardrailCheck(
            "C-10",
            "Use shared config accessor instead of ad-hoc env reads",
            lambda ctx: _build_check_from_violations(
                "C-10",
                "Use shared config accessor instead of ad-hoc env reads",
                _collect_category_violations(ctx, "c10", check_c10),
            ),
        ),
        GuardrailCheck(
            "C-11",
            "Import get_port from shared.ports only",
            lambda ctx: _build_check_from_violations(
                "C-11",
                "Import get_port from shared.ports only",
                _collect_category_violations(ctx, "c11", check_c11),
            ),
        ),
        GuardrailCheck(
            "C-17",
            "Embed colours must use approved palette",
            lambda ctx: _build_check_from_violations(
                "C-17",
                "Embed colours must use approved palette",
                _collect_category_violations(ctx, "c17", check_c17),
            ),
        ),
        GuardrailCheck(
            "F-01",
            "Toggle used but not documented",
            _build_feature_toggle_check("F-01", "Toggle used but not documented"),
        ),
        GuardrailCheck(
            "F-04",
            "Documented toggles not referenced in code",
            _build_feature_toggle_check(
                "F-04", "Documented toggles not referenced in code"
            ),
        ),
        GuardrailCheck(
            "D-01",
            "Doc titles must not include 'Phase'",
            lambda ctx: _build_check_from_violations(
                "D-01",
                "Doc titles must not include 'Phase'",
                _collect_category_violations(ctx, "d01", check_d01),
            ),
        ),
        GuardrailCheck(
            "D-02",
            "Docs require standard footer",
            lambda ctx: _build_check_from_violations(
                "D-02",
                "Docs require standard footer",
                _collect_category_violations(ctx, "d02", check_d02),
            ),
        ),
        GuardrailCheck(
            "D-03",
            "ENV parity check",
            _build_parity_check,
        ),
        GuardrailCheck(
            "D-04",
            "Docs must be linked from docs/README.md",
            lambda ctx: _build_check_from_violations(
                "D-04",
                "Docs must be linked from docs/README.md",
                [
                    v
                    for v in _collect_category_violations(ctx, "d04_d08", check_d04_d08)
                    if "D-04" in v.rule_id
                ],
            ),
        ),
        GuardrailCheck(
            "D-05",
            "ADR files must follow ADR-XXXX.md naming",
            lambda ctx: _build_check_from_violations(
                "D-05",
                "ADR files must follow ADR-XXXX.md naming",
                _collect_category_violations(ctx, "d05", check_d05),
            ),
        ),
        GuardrailCheck(
            "D-06",
            "CHANGELOG must reference new AUDIT entries",
            lambda ctx: _build_check_from_violations(
                "D-06",
                "CHANGELOG must reference new AUDIT entries",
                _collect_category_violations(ctx, "d06", check_d06, ctx.diff_status),
            ),
        ),
        GuardrailCheck(
            "D-08",
            "Docs must be linked from docs/README.md",
            lambda ctx: _build_check_from_violations(
                "D-08",
                "Docs must be linked from docs/README.md",
                [
                    v
                    for v in _collect_category_violations(ctx, "d04_d08", check_d04_d08)
                    if "D-08" in v.rule_id
                ],
            ),
        ),
        GuardrailCheck(
            "D-09",
            "Runtime changes require tests or Tests: declaration",
            _build_pr_only_check(
                "D-09",
                "Runtime changes require tests or Tests: declaration",
                lambda ctx: _collect_category_violations(
                    ctx, "d09", check_d09, ctx.changed_files, ctx.pr_body
                ),
            ),
        ),
        GuardrailCheck(
            "D-10",
            "User-facing changes require docs or Docs: declaration",
            _build_pr_only_check(
                "D-10",
                "User-facing changes require docs or Docs: declaration",
                lambda ctx: _collect_category_violations(
                    ctx, "d10", check_d10, ctx.changed_files, ctx.pr_body
                ),
            ),
        ),
        GuardrailCheck(
            "G-03",
            "PR body must include [meta] block with labels and milestone",
            _build_pr_only_check(
                "G-03",
                "PR body must include [meta] block with labels and milestone",
                lambda ctx: _collect_category_violations(ctx, "g03", check_g03, ctx.pr_body),
            ),
        ),
        GuardrailCheck(
            "G-06",
            "New docs must be lower_snake_case and avoid Phase",
            lambda ctx: _build_check_from_violations(
                "G-06",
                "New docs must be lower_snake_case and avoid Phase",
                _collect_category_violations(ctx, "g06", check_g06, ctx.diff_status),
            ),
        ),
        GuardrailCheck(
            "G-09",
            "PR body must declare Tests: and Docs: sections",
            _build_pr_only_check(
                "G-09",
                "PR body must declare Tests: and Docs: sections",
                lambda ctx: _collect_category_violations(ctx, "g09", check_g09, ctx.pr_body),
            ),
        ),
    ]


def _build_check_from_violations(code: str, description: str, violations: List[Violation]) -> CheckResult:
    return CheckResult(code=code, description=description, status=_status_from_violations(violations), violations=violations)


def _build_feature_toggle_check(code: str, description: str) -> Callable[[GuardrailContext], CheckResult]:
    def _runner(context: GuardrailContext) -> CheckResult:
        violations, skip_reason = _collect_feature_toggle_violations(context)
        relevant = [v for v in violations if v.rule_id == code]
        status = _status_from_violations(relevant)
        if skip_reason and not relevant:
            return CheckResult(code=code, description=description, status="skip", violations=[], reason=skip_reason)
        return CheckResult(
            code=code,
            description=description,
            status=status,
            violations=relevant,
            reason=skip_reason if status == "skip" else None,
        )

    return _runner


def _build_pr_only_check(
    code: str, description: str, runner: Callable[[GuardrailContext], List[Violation]]
) -> Callable[[GuardrailContext], CheckResult]:
    def _wrapped(context: GuardrailContext) -> CheckResult:
        if context.pr_number <= 0:
            return CheckResult(code=code, description=description, status="skip", reason="PR context unavailable")
        violations = runner(context)
        return _build_check_from_violations(code, description, violations)

    return _wrapped


def _build_parity_check(context: GuardrailContext) -> CheckResult:
    violations, reason = _parity_violation(context.parity_status)
    status = "skip" if reason and not violations else _status_from_violations(violations)
    return CheckResult(
        code="D-03",
        description="ENV parity check",
        status=status,
        violations=violations,
        reason=reason if status == "skip" else None,
    )


def _build_categories(check_results: List[CheckResult]) -> Dict[str, CategoryResult]:
    categories = {
        "Structure (S)": CategoryResult("Structure (S)"),
        "Code (C)": CategoryResult("Code (C)"),
        "Features (F)": CategoryResult("Features (F)"),
        "Docs (D)": CategoryResult("Docs (D)"),
        "Governance (G)": CategoryResult("Governance (G)"),
    }

    prefix_map = {
        "S": "Structure (S)",
        "C": "Code (C)",
        "F": "Features (F)",
        "D": "Docs (D)",
        "G": "Governance (G)",
    }

    seen: Dict[str, set[tuple[str, str, Tuple[str, ...]]]] = {
        key: set() for key in categories
    }

    for result in check_results:
        category_key = prefix_map.get(result.code.split("-", 1)[0])
        if not category_key:
            continue
        category = categories[category_key]
        for violation in result.violations:
            fingerprint = (
                violation.rule_id,
                violation.message,
                tuple(violation.files),
            )
            if fingerprint in seen[category_key]:
                continue
            seen[category_key].add(fingerprint)
            category.add(violation)

    return categories


CHECKS: List[GuardrailCheck] = _build_guardrail_checks()


def _ensure_audit_report_path() -> Path:
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    audit_dir = AUDIT_ROOT / f"{timestamp}_GUARDRAILS"
    audit_dir.mkdir(parents=True, exist_ok=True)
    return audit_dir / "report.md"


def _write_markdown_report(suite: SuiteResult, report_path: Path, parity_status: Optional[str]) -> None:
    lines: List[str] = ["# Repository Guardrails Report", ""]
    if parity_status:
        lines.append(f"ENV parity status: {parity_status}")
        lines.append("")
    for category in suite.categories.values():
        icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}[category.status]
        lines.append(f"## {category.identifier} {icon}")
        if not category.violations:
            lines.append("No issues detected.")
        else:
            for violation in category.violations:
                lines.append(f"- **{violation.rule_id}** ({violation.severity}): {violation.message}")
                for file in violation.files:
                    lines.append(f"  - {file}")
        lines.append("")
    report_path.write_text("\n".join(lines), encoding="utf-8")


def _write_summary_json(
    suite: SuiteResult,
    path: Path,
    parity_status: Optional[str],
    config_parity_status: Optional[str],
    secret_scan_status: Optional[str],
) -> None:
    overall_status = "pass"
    if any(cat.status == "fail" for cat in suite.categories.values()):
        overall_status = "fail"
    elif any(cat.status == "warn" for cat in suite.categories.values()):
        overall_status = "warn"

    def _serialize_violation(violation: Violation) -> Dict[str, object]:
        return {
            "rule": violation.rule_id,
            "severity": violation.severity,
            "message": violation.message,
            "files": violation.files,
        }

    serialized_results = [
        {
            "code": check.code,
            "description": check.description,
            "status": check.status,
            "violations": [_serialize_violation(v) for v in check.violations],
            "reason": check.reason,
        }
        for check in suite.check_results
    ]

    legacy_checks = {
        entry["code"]: {
            "status": entry["status"],
            "violations": len(entry["violations"]),
            "reason": entry.get("reason"),
            "description": entry.get("description"),
        }
        for entry in serialized_results
    }

    payload = {
        "overall_status": overall_status,
        "parity_status": parity_status,
        "config_parity_status": config_parity_status,
        "secret_scan_status": secret_scan_status,
        "results": serialized_results,
        "categories": [
            {
                "id": cat.identifier,
                "status": cat.status,
                "violations": [
                    {
                        "rule": v.rule_id,
                        "severity": v.severity,
                        "message": v.message,
                        "files": v.files,
                    }
                    for v in cat.violations
                ],
            }
            for cat in suite.categories.values()
        ],
        "checks": legacy_checks,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

def _append_summary_markdown(suite: SuiteResult, path: Path) -> None:
    def _group_file_references(file_entries: List[str]) -> List[str]:
        grouped: Dict[str, List[str]] = {}
        for entry in file_entries:
            if ":" in entry:
                path_part, line_part = entry.split(":", 1)
                grouped.setdefault(path_part, []).append(line_part)
            else:
                grouped.setdefault(entry, [])
        formatted: List[str] = []
        for path_part, line_parts in grouped.items():
            if line_parts:
                formatted.append(f"{path_part}:{', '.join(line_parts)}")
            else:
                formatted.append(path_part)
        return formatted

    lines: List[str] = ["# Guardrails Summary", ""]
    has_failures = any(result.status == "fail" for result in suite.check_results)
    lines.append("❌ Guardrail violations found" if has_failures else "✅ All guardrail checks passed")
    lines.append("")
    lines.append("## Automated guardrail checks")
    lines.append("")
    status_icons = {"pass": "✅", "fail": "❌", "skip": "⚪"}
    for result in sorted(suite.check_results, key=lambda r: r.code):
        icon = status_icons.get(result.status, "⚠️")
        suffix = ""
        if result.status == "fail":
            count = len(result.violations)
            plural = "s" if count != 1 else ""
            suffix = f" ({count} violation{plural})"
        elif result.status == "skip":
            reason = result.reason or "not run"
            suffix = f" (skipped: {reason})"
        lines.append(f"- {icon} {result.code} — {result.description}{suffix}")

    lines.append("")
    lines.append("## Summary by category")
    lines.append("")
    lines.append("| Category       | Status |")
    lines.append("|----------------|--------|")
    for cat in suite.categories.values():
        icon = {"pass": "✅", "warn": "⚠️", "fail": "❌"}[cat.status]
        count = len(cat.violations)
        lines.append(f"| {cat.identifier} | {icon} {count} |")
    lines.append("")

    all_violations = suite.violations
    if not all_violations:
        lines.append("No guardrail issues detected.")
    else:
        lines.append("## Violations")
        lines.append("")
        for violation in all_violations:
            lines.append(f"### {violation.rule_id} — {violation.message}")
            if violation.rule_id == "F-04":
                if violation.files:
                    lines.append(
                        "The following toggles are documented but currently reported as unused:"
                    )
                    lines.append("")
                    for toggle in violation.files:
                        lines.append(f"- {toggle}")
                else:
                    lines.append("No documented toggles are marked as unused.")
            else:
                grouped_files = _group_file_references(violation.files)
                for file_entry in grouped_files:
                    lines.append(f"- {file_entry}")
            lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _run_guardrail_checks(context: GuardrailContext) -> List[CheckResult]:
    results: List[CheckResult] = []
    for check in CHECKS:
        try:
            result = check.runner(context)
        except Exception as exc:  # pragma: no cover - defensive fallback
            result = CheckResult(
                code=check.code,
                description=check.description,
                status="fail",
                violations=[],
                reason=str(exc),
            )
        results.append(result)
    return results


def run_checks(
    base_ref: Optional[str], pr_body: str, parity_status: Optional[str], pr_number: int
) -> SuiteResult:
    diff_status = _git_diff_status(base_ref)
    changed_files = _git_diff_names(base_ref)
    context = GuardrailContext(
        diff_status=diff_status,
        changed_files=changed_files,
        pr_body=pr_body,
        parity_status=parity_status,
        pr_number=pr_number,
    )

    check_results = _run_guardrail_checks(context)
    categories = _build_categories(check_results)
    violations = [violation for result in check_results for violation in result.violations]

    return SuiteResult(check_results=check_results, categories=categories, violations=violations)


def run_all_checks(
    base_ref: Optional[str] = None,
    pr_number: int = 0,
    pr_body: Optional[str] = None,
    parity_status: Optional[str] = None,
) -> Tuple[List[CheckResult], List[Violation]]:
    event_path = get_env_path("GITHUB_EVENT_PATH")
    resolved_pr_body = pr_body if pr_body is not None else _load_pr_body(str(event_path) if event_path else None)
    _, _, computed_parity_status = _compute_guardrail_health()
    resolved_parity_status = parity_status or computed_parity_status or get_env("ENV_PARITY_STATUS")

    suite = run_checks(base_ref, resolved_pr_body, resolved_parity_status, pr_number)
    updated_violations = [violation for result in suite.check_results for violation in result.violations]
    return suite.check_results, updated_violations


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run repository guardrails suite")
    parser.add_argument(
        "--pr", type=int, required=False, default=0, help="Pull request number if available"
    )
    parser.add_argument("--status-file", type=Path, default=None, help="Path to write overall status")
    parser.add_argument("--summary", type=Path, default=None, help="Path to append human-readable summary")
    parser.add_argument("--base-ref", type=str, default=None, help="Base ref for diff (e.g., origin/main)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    event_path = get_env_path("GITHUB_EVENT_PATH")
    pr_body = _load_pr_body(str(event_path) if event_path else None)
    _, _, parity_status = _compute_guardrail_health()
    parity_status = parity_status or get_env("ENV_PARITY_STATUS")
    suite = run_checks(args.base_ref, pr_body, parity_status, args.pr)

    report_path = _ensure_audit_report_path()
    _write_markdown_report(suite, report_path, parity_status)
    if args.summary:
        _append_summary_markdown(suite, args.summary)

    overall_status = "fail" if any(res.status == "fail" for res in suite.check_results) else "pass"
    if args.status_file:
        args.status_file.write_text(overall_status, encoding="utf-8")
    return 0 if overall_status == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
