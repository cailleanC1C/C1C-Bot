#!/usr/bin/env python3
"""Validate the repository-backed GitHub Wiki source."""

from pathlib import Path
import re
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[2]
WIKI = ROOT / "docs" / "wiki"
REQUIRED = ("Home.md", "_Sidebar.md")
LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")


def canonical_page_name(name: str) -> str:
    """Collapse punctuation lookalikes for duplicate detection."""
    normalized = unicodedata.normalize("NFKC", name).casefold()
    return "".join(ch for ch in normalized if ch.isalnum())


def main() -> int:
    errors: list[str] = []
    for required in REQUIRED:
        if not (WIKI / required).is_file():
            errors.append(f"missing required page: docs/wiki/{required}")

    pages = sorted(WIKI.glob("*.md")) if WIKI.is_dir() else []
    filenames = {page.name for page in pages}
    canonical: dict[str, Path] = {}
    for page in pages:
        key = canonical_page_name(page.stem)
        if previous := canonical.get(key):
            errors.append(
                "duplicate lookalike page names: "
                f"{previous.relative_to(ROOT)} and {page.relative_to(ROOT)}"
            )
        else:
            canonical[key] = page

    for page in pages:
        for target in LINK_RE.findall(page.read_text(encoding="utf-8")):
            expected = f"{target.strip().replace(' ', '-')}.md"
            if expected not in filenames:
                errors.append(
                    f"unresolved wiki link in {page.relative_to(ROOT)}: "
                    f"[[{target}]] -> docs/wiki/{expected}"
                )

    if errors:
        sys.stderr.write("Wiki validation failed:\n")
        for error in errors:
            sys.stderr.write(f"- {error}\n")
        return 1
    sys.stdout.write(f"Wiki validation passed ({len(pages)} pages, all links resolved).\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
