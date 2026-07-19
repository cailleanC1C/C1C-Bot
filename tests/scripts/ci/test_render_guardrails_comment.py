from scripts.ci.render_guardrails_comment import render_guardrails_comment


def test_render_guardrails_comment_includes_all_statuses():
    data = {
        "results": [
            {
                "code": "C-02",
                "description": "Use logger instead of print()",
                "status": "pass",
                "violations": [],
            },
            {
                "code": "C-03",
                "description": "Parent-relative imports are forbidden",
                "status": "fail",
                "violations": [1, 2],
            },
            {
                "code": "D-04",
                "description": "README includes setup instructions",
                "status": "fail",
                "violations": [1],
            },
            {
                "code": "G-03",
                "description": "PR body has [meta] block",
                "status": "skip",
                "reason": "PR body unavailable",
                "violations": [],
            },
        ]
    }

    body = render_guardrails_comment(data)

    assert "Guardrails Summary" in body
    assert "✅ C-02 — Use logger instead of print()" in body
    assert "❌ C-03 — Parent-relative imports are forbidden (2 violations)" in body
    assert "❌ D-04 — README includes setup instructions (1 violation)" in body
    assert "⚪ G-03 — PR body has [meta] block (skipped: PR body unavailable)" in body


def test_render_guardrails_comment_handles_empty_results():
    body = render_guardrails_comment({})

    assert "No guardrail checks found" in body
    assert "Config parity" not in body
    assert "Secret scan" not in body


def test_render_guardrails_comment_handles_legacy_checks_mapping():
    data = {
        "checks": {
            "C-01": {"status": "pass", "violations": 0, "description": "Async I/O"},
            "C-02": {"status": "fail", "violations": 3, "reason": "Found prints"},
            "D-03": {"status": "skip", "violations": [], "reason": "No PR body"},
        }
    }

    body = render_guardrails_comment(data)

    assert "✅ C-01 — Async I/O" in body
    assert "❌ C-02 — C-02 (3 violations)" in body
    assert "⚪ D-03 — D-03 (skipped: No PR body)" in body
