from __future__ import annotations

from pathlib import Path
import json
import sys

sys.path.append(str(Path(__file__).resolve().parents[2] / "scripts" / "ci"))

import guardrails_suite


def test_repository_root_and_runtime_scan_scope() -> None:
    repository_root = Path(__file__).resolve().parents[2]

    assert guardrails_suite.ROOT == repository_root
    runtime_paths = {
        path.relative_to(repository_root).as_posix()
        for path in guardrails_suite._iter_runtime_python_files()
    }
    assert "scripts/ci/guardrails_suite.py" not in runtime_paths
    assert "scripts/ci/check_docs.py" not in runtime_paths
    assert "scripts/ci/utils/env.py" not in runtime_paths
    assert any(path.startswith(("modules/", "shared/", "cogs/")) for path in runtime_paths)


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


def test_pr_body_metadata_is_not_a_guardrail() -> None:
    codes = {check.code for check in guardrails_suite.CHECKS}

    assert "G-09" not in codes


def test_d09_ignores_pr_body_metadata() -> None:
    tests_category = guardrails_suite.CategoryResult("Docs (D)")

    guardrails_suite.check_d09(tests_category, ["modules/example.py"])

    assert tests_category.status == "fail"
    assert tests_category.violations[0].message == "Runtime changes require tests"


def test_ci_only_change_needs_no_pr_body_metadata() -> None:
    tests_category = guardrails_suite.CategoryResult("Docs (D)")

    guardrails_suite.check_d09(tests_category, [".github/workflows/test.yml"])

    assert tests_category.status == "pass"


def test_d10_is_not_an_automated_guardrail() -> None:
    codes = {check.code for check in guardrails_suite.CHECKS}

    assert "D-10" not in codes
    assert "D-10" not in guardrails_suite.PR_DIFF_AWARE_CHECKS


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


def test_feature_checks_degrade_quietly_when_registry_is_unavailable(
    monkeypatch: object, caplog: object
) -> None:
    monkeypatch.setattr(guardrails_suite, "_load_feature_toggle_names", lambda: set())
    context = guardrails_suite.GuardrailContext({}, [], "", None, 0)
    runners = {
        check.code: check.runner for check in guardrails_suite.CHECKS if check.code in {"F-01", "F-04"}
    }

    results = [runners[code](context) for code in ("F-01", "F-04")]

    assert [result.status for result in results] == ["pass", "pass"]
    assert all(result.reason is None for result in results)
    assert "feature toggle registry" not in caplog.text.lower()


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


def test_c11_allows_shared_ports_and_rejects_config_runtime(
    tmp_path: Path, monkeypatch: object
) -> None:
    _configure_roots(tmp_path, monkeypatch)
    package = tmp_path / "modules" / "example"
    package.mkdir(parents=True)
    (package / "allowed.py").write_text(
        "from shared.ports import get_port\nPORT = get_port()\n",
        encoding="utf-8",
    )
    (package / "forbidden.py").write_text(
        "from config import runtime as runtime_config\n"
        "PORT = runtime_config.get_port()\n",
        encoding="utf-8",
    )
    (package / "forbidden_qualified.py").write_text(
        "import config.runtime\nPORT = config.runtime.get_port()\n",
        encoding="utf-8",
    )
    (package / "forbidden_direct.py").write_text(
        "from config.runtime import get_port\nPORT = get_port()\n",
        encoding="utf-8",
    )
    (package / "forbidden_shared.py").write_text(
        "import shared.config\nPORT = shared.config.get_port()\n",
        encoding="utf-8",
    )
    (package / "forbidden_shared_direct.py").write_text(
        "from shared.config import get_port\nPORT = get_port()\n",
        encoding="utf-8",
    )

    category = guardrails_suite.CategoryResult("c11")
    guardrails_suite.check_c11(category)

    assert len(category.violations) == 1
    assert category.violations[0].files == [
        "modules/example/forbidden.py:2",
        "modules/example/forbidden_direct.py:1",
        "modules/example/forbidden_qualified.py:2",
        "modules/example/forbidden_shared.py:2",
        "modules/example/forbidden_shared_direct.py:1",
    ]


def test_c10_temporarily_exempts_existing_coreops_cog_env_debt(
    tmp_path: Path, monkeypatch: object
) -> None:
    _configure_roots(tmp_path, monkeypatch)
    coreops = tmp_path / "packages" / "c1c-coreops" / "src" / "c1c_coreops"
    coreops.mkdir(parents=True)
    (coreops / "cog.py").write_text("import os\nVALUE = os.getenv('VALUE')\n")

    category = guardrails_suite.CategoryResult("c10")
    guardrails_suite.check_c10(category)

    assert category.violations == []


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
    monkeypatch.setattr(guardrails_suite, "_git_diff_names", lambda base_ref: ["modules/bad_import.py"])
    monkeypatch.setattr(
        guardrails_suite, "_git_diff_status", lambda base_ref: {"modules/bad_import.py": "M"}
    )

    pr_body = "Summary only; no verification metadata."

    suite = guardrails_suite.run_checks(None, pr_body=pr_body, parity_status="success", pr_number=1)

    codes = {result.code for result in suite.check_results}
    assert codes == {check.code for check in guardrails_suite.CHECKS}
    assert "G-03" not in codes

    c03_result = next(result for result in suite.check_results if result.code == "C-03")
    assert c03_result.status == "fail"
    d03_result = next(result for result in suite.check_results if result.code == "D-03")
    assert d03_result.status == "pass"


def test_pr_scope_ignores_unchanged_historical_violations(
    tmp_path: Path, monkeypatch: object
) -> None:
    _configure_roots(tmp_path, monkeypatch)
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    (modules_dir / "historical.py").write_text("from ..legacy import helper\n", encoding="utf-8")
    (modules_dir / "changed.py").write_text("value = 1\n", encoding="utf-8")

    monkeypatch.setattr(guardrails_suite, "_git_diff_names", lambda base_ref: ["modules/changed.py"])
    monkeypatch.setattr(
        guardrails_suite, "_git_diff_status", lambda base_ref: {"modules/changed.py": "M"}
    )
    monkeypatch.setattr(guardrails_suite, "_load_feature_toggle_names", lambda: set())

    suite = guardrails_suite.run_checks(
        "origin/main",
        pr_body="Summary only; no verification metadata.",
        parity_status="success",
        pr_number=1017,
    )

    c03_result = next(result for result in suite.check_results if result.code == "C-03")
    assert c03_result.status == "pass"
    assert c03_result.violations == []


def test_pr_scope_keeps_violations_in_changed_files(tmp_path: Path, monkeypatch: object) -> None:
    _configure_roots(tmp_path, monkeypatch)
    modules_dir = tmp_path / "modules"
    modules_dir.mkdir()
    (modules_dir / "changed.py").write_text("from ..legacy import helper\n", encoding="utf-8")

    monkeypatch.setattr(guardrails_suite, "_git_diff_names", lambda base_ref: ["modules/changed.py"])
    monkeypatch.setattr(
        guardrails_suite, "_git_diff_status", lambda base_ref: {"modules/changed.py": "M"}
    )
    monkeypatch.setattr(guardrails_suite, "_load_feature_toggle_names", lambda: set())

    suite = guardrails_suite.run_checks(
        "origin/main",
        pr_body="Summary only; no verification metadata.",
        parity_status="success",
        pr_number=1017,
    )

    c03_result = next(result for result in suite.check_results if result.code == "C-03")
    assert c03_result.status == "fail"
    assert c03_result.violations[0].files == ["modules/changed.py:1"]


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
    monkeypatch.setattr(guardrails_suite, "_git_diff_names", lambda base_ref: ["modules/bad_import.py"])
    monkeypatch.setattr(
        guardrails_suite, "_git_diff_status", lambda base_ref: {"modules/bad_import.py": "M"}
    )

    pr_body = "Summary only; no verification metadata."

    results, violations = guardrails_suite.run_all_checks(
        base_ref=None, pr_number=1, pr_body=pr_body, parity_status="success"
    )

    codes = {result.code for result in results}
    assert codes == {check.code for check in guardrails_suite.CHECKS}
    assert any(v.rule_id == "C-03" for v in violations)
