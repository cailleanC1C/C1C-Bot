from __future__ import annotations

from pathlib import Path
import json
import sys

sys.path.append(str(Path(__file__).resolve().parents[2] / "scripts" / "ci"))

import guardrails_suite


def _configure_roots(tmp_path: Path, monkeypatch: object) -> None:
    monkeypatch.setattr(guardrails_suite, "ROOT", tmp_path)
    monkeypatch.setattr(guardrails_suite, "AUDIT_ROOT", tmp_path / "AUDIT")
    monkeypatch.setattr(guardrails_suite, "DOCS_ROOT", tmp_path / "docs")


def test_c03_detects_parent_import(tmp_path: Path, monkeypatch: object) -> None:
    _configure_roots(tmp_path, monkeypatch)
    module_dir = tmp_path / "modules"
    module_dir.mkdir()
    target = module_dir / "sample.py"
    target.write_text("from ..utils import helper\n", encoding="utf-8")

    suite = guardrails_suite.run_checks(None, pr_body="", parity_status="success", pr_number=0)
    c03_result = next(result for result in suite.check_results if result.code == "C-03")

    assert c03_result.status == "fail"
    assert any("C-03" in violation.rule_id for violation in c03_result.violations)


def test_d02_requires_footer(tmp_path: Path, monkeypatch: object) -> None:
    _configure_roots(tmp_path, monkeypatch)
    docs_dir = tmp_path / "docs"
    docs_dir.mkdir()
    doc = docs_dir / "Guide.md"
    doc.write_text("# Guide\nContent only\n", encoding="utf-8")

    category = guardrails_suite.CategoryResult("Docs (D)")
    guardrails_suite.check_d02(category)

    assert category.status == "fail"
    assert category.violations[0].rule_id == "D-02"


def test_g09_enforces_tests_and_docs_blocks() -> None:
    body = """Summary

Details here.
"""
    category = guardrails_suite.CategoryResult("Governance (G)")
    guardrails_suite.check_g09(category, body)

    assert category.status == "fail"
    assert category.violations[0].rule_id == "G-09"


def test_g03_requires_meta_block() -> None:
    body = """Summary only

Tests: Added
Docs: Added
"""
    category = guardrails_suite.CategoryResult("Governance (G)")
    guardrails_suite.check_g03(category, body)

    assert category.status == "fail"
    assert category.violations[0].rule_id == "G-03"


def test_f04_uses_feature_registry_and_accessor(tmp_path: Path, monkeypatch: object) -> None:
    _configure_roots(tmp_path, monkeypatch)

    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    (modules_dir / "feature_usage.py").write_text(
        "from modules.common import feature_flags\n\n"
        "if feature_flags.is_enabled('member_panel'):\n"
        "    pass\n",
        encoding="utf-8",
    )

    category = guardrails_suite.CategoryResult("Features (F)")
    monkeypatch.setattr(
        guardrails_suite,
        "_load_feature_toggle_names",
        lambda: {"member_panel", "recruiter_panel"},
    )

    guardrails_suite.check_feature_toggles(category)

    assert category.status == "warn"
    assert category.violations[0].rule_id == "F-04"
    assert category.violations[0].files == ["recruiter_panel"]


def test_summary_reports_guardrail_health(tmp_path: Path, monkeypatch: object) -> None:
    _configure_roots(tmp_path, monkeypatch)

    docs_ops = tmp_path / "docs" / "ops"
    docs_ops.mkdir(parents=True)
    (docs_ops / ".env.example").write_text(
        "DISCORD_TOKEN=placeholder\nRECRUITMENT_SHEET_ID=sheet\n",
        encoding="utf-8",
    )

    config_md = docs_ops / "Config.md"
    config_md.write_text(
        "# Config\n\n## Environment keys\n\n| `DISCORD_TOKEN` | desc |\n| `RECRUITMENT_SHEET_ID` | desc |\n",
        encoding="utf-8",
    )

    summary_path = tmp_path / "summary.md"
    check_results = [
        guardrails_suite.CheckResult(
            code="C-02",
            description="Use logger instead of print()",
            status="pass",
        ),
        guardrails_suite.CheckResult(
            code="C-03",
            description="Parent-relative imports are forbidden",
            status="fail",
            violations=[guardrails_suite.Violation("C-03", "error", "Parent-relative imports are forbidden", [])],
        ),
        guardrails_suite.CheckResult(
            code="D-03",
            description="ENV parity check",
            status="skip",
            reason="ENV parity status unavailable",
        ),
    ]
    suite = guardrails_suite.SuiteResult(
        check_results=check_results,
        categories=guardrails_suite._build_categories(check_results),
        violations=[violation for result in check_results for violation in result.violations],
    )

    guardrails_suite._append_summary_markdown(suite, summary_path)

    summary_text = summary_path.read_text(encoding="utf-8")
    assert "## Automated guardrail checks" in summary_text
    assert "- ✅ C-02 — Use logger instead of print()" in summary_text
    assert "- ❌ C-03 — Parent-relative imports are forbidden (1 violation)" in summary_text
    assert "- ⚪ D-03 — ENV parity check (skipped: ENV parity status unavailable)" in summary_text
    assert "Config parity" not in summary_text
    assert "Secret scan" not in summary_text


def test_summary_json_includes_all_check_results(tmp_path: Path, monkeypatch: object) -> None:
    _configure_roots(tmp_path, monkeypatch)

    check_results = [
        guardrails_suite.CheckResult(
            code="C-02",
            description="Use logger instead of print()",
            status="pass",
        ),
        guardrails_suite.CheckResult(
            code="C-03",
            description="Parent-relative imports are forbidden",
            status="fail",
            violations=[guardrails_suite.Violation("C-03", "error", "Parent-relative imports are forbidden", [])],
        ),
        guardrails_suite.CheckResult(
            code="D-03",
            description="ENV parity check",
            status="skip",
            reason="ENV parity status unavailable",
        ),
    ]
    suite = guardrails_suite.SuiteResult(
        check_results=check_results,
        categories=guardrails_suite._build_categories(check_results),
        violations=[violation for result in check_results for violation in result.violations],
    )

    json_path = tmp_path / "guardrails-results.json"
    guardrails_suite._write_summary_json(
        suite, json_path, parity_status="ok", config_parity_status="success", secret_scan_status="success"
    )

    payload = json.loads(json_path.read_text(encoding="utf-8"))

    assert payload.get("results")
    codes = [entry["code"] for entry in payload["results"]]
    assert codes == sorted([result.code for result in check_results])
    assert set(payload.get("checks", {}).keys()) == set(codes)
    for code, entry in payload.get("checks", {}).items():
        matching = next(result for result in check_results if result.code == code)
        assert entry.get("status") == matching.status
        assert entry.get("violations") == len(matching.violations)
        assert entry.get("reason") == matching.reason
    assert payload.get("config_parity_status") == "success"
    assert payload.get("secret_scan_status") == "success"


def test_run_checks_covers_all_codes(tmp_path: Path, monkeypatch: object) -> None:
    _configure_roots(tmp_path, monkeypatch)

    docs_root = tmp_path / "docs"
    ops_root = docs_root / "ops"
    ops_root.mkdir(parents=True)
    (docs_root / "README.md").write_text(
        "# Docs\n\n- [ops/Config.md](ops/Config.md)\n\nDoc last updated: 2026-01-01 (v0.9.8.3)\n",
        encoding="utf-8",
    )
    (ops_root / "Config.md").write_text(
        "# Config\n\n## Environment keys\n\n| `DISCORD_TOKEN` | desc |\n\nDoc last updated: 2026-01-01 (v0.9.8.3)\n",
        encoding="utf-8",
    )
    (ops_root / ".env.example").write_text("DISCORD_TOKEN=placeholder\n", encoding="utf-8")

    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    (modules_dir / "bad_import.py").write_text("from ..legacy import helper\n", encoding="utf-8")

    monkeypatch.setattr(guardrails_suite, "_load_feature_toggle_names", lambda: set())

    pr_body = (
        "[meta]\nlabels: guardrails\nmilestone: Harmonize v1.0\n[/meta]\n\n"
        "Tests:\nNot required (reason: CI-only)\nDocs:\nNot required (reason: CI-only)\n"
    )

    suite = guardrails_suite.run_checks(None, pr_body=pr_body, parity_status="success", pr_number=1)

    codes = {result.code for result in suite.check_results}
    assert codes == {check.code for check in guardrails_suite.CHECKS}

    c03_result = next(result for result in suite.check_results if result.code == "C-03")
    assert c03_result.status == "fail"
    d03_result = next(result for result in suite.check_results if result.code == "D-03")
    assert d03_result.status == "pass"


def test_run_all_checks_returns_results(tmp_path: Path, monkeypatch: object) -> None:
    _configure_roots(tmp_path, monkeypatch)

    docs_root = tmp_path / "docs"
    ops_root = docs_root / "ops"
    ops_root.mkdir(parents=True)
    (docs_root / "README.md").write_text(
        "# Docs\n\n- [ops/Config.md](ops/Config.md)\n\nDoc last updated: 2026-01-01 (v0.9.8.3)\n",
        encoding="utf-8",
    )
    (ops_root / "Config.md").write_text(
        "# Config\n\n## Environment keys\n\n| `DISCORD_TOKEN` | desc |\n\nDoc last updated: 2026-01-01 (v0.9.8.3)\n",
        encoding="utf-8",
    )
    (ops_root / ".env.example").write_text("DISCORD_TOKEN=placeholder\n", encoding="utf-8")

    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    (modules_dir / "bad_import.py").write_text("from ..legacy import helper\n", encoding="utf-8")

    monkeypatch.setattr(guardrails_suite, "_load_feature_toggle_names", lambda: set())

    pr_body = (
        "[meta]\nlabels: guardrails\nmilestone: Harmonize v1.0\n[/meta]\n\n"
        "Tests:\nNot required (reason: CI-only)\nDocs:\nNot required (reason: CI-only)\n"
    )

    results, violations = guardrails_suite.run_all_checks(
        base_ref=None, pr_number=1, pr_body=pr_body, parity_status="success"
    )

    codes = {result.code for result in results}
    assert codes == {check.code for check in guardrails_suite.CHECKS}
    assert any(v.rule_id == "C-03" for v in violations)
