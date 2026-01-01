from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_ROOT = REPO_ROOT / "docs"
OPS_DIR = DOCS_ROOT / "ops"
MODULES_DIR = DOCS_ROOT / "modules"
README_PATH = DOCS_ROOT / "README.md"

ALLOWED_OPS_MD = {
    "CommandMatrix.md",
    "Config.md",
    "Housekeeping.md",
    "Logging.md",
    "OnboardingFlows.md",
    "PermCommandQuickstart.md",
    "Promo_Summary_Spec.md",
    "PromoTickets.md",
    "Welcome_Summary_Spec.md",
    "Watchers.md",
    "ShardTracker.md",
}

REQUIRED_MODULE_DOCS = {
    "CoreOps.md",
    "CoreOps-Development.md",
    "Onboarding.md",
    "Welcome.md",
    "Recruitment.md",
    "Placement.md",
    "PermissionsUI.md",
}


@pytest.mark.parametrize("ops_dir", [OPS_DIR])
def test_ops_docs_stay_on_allowlist(ops_dir: Path) -> None:
    assert ops_dir.is_dir(), f"Missing docs/ops/ directory at {ops_dir}"
    unexpected = []
    for path in ops_dir.rglob("*.md"):
        if path.name not in ALLOWED_OPS_MD:
            rel_path = Path("ops") / path.relative_to(ops_dir)
            unexpected.append(str(rel_path))
    assert not unexpected, (
        "Unexpected docs/ops Markdown files: " + ", ".join(sorted(unexpected))
    )


def test_required_module_docs_exist() -> None:
    assert MODULES_DIR.is_dir(), "docs/modules/ directory is missing"
    module_files = {path.name for path in MODULES_DIR.glob("*.md")}
    missing = sorted(REQUIRED_MODULE_DOCS - module_files)
    assert not missing, (
        "Missing required module docs: " + ", ".join(missing)
    )


def test_module_docs_linked_from_docs_readme() -> None:
    module_files = sorted(MODULES_DIR.glob("*.md"))
    assert module_files, "docs/modules/ contains no module docs to index"
    readme_text = README_PATH.read_text(encoding="utf-8")
    missing_links = []
    for path in module_files:
        rel_link = f"modules/{path.name}"
        if rel_link not in readme_text:
            missing_links.append(rel_link)
    assert not missing_links, (
        "The following module docs are not linked from docs/README.md: "
        + ", ".join(missing_links)
    )
