#!/usr/bin/env python3
"""Validate the repository-backed GitHub Wiki source."""

from pathlib import Path
import re
import sys
import unicodedata

ROOT = Path(__file__).resolve().parents[2]
WIKI = ROOT / "docs" / "wiki"
REQUIRED = ("home.md", "_sidebar.md")
LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|[^\]]+)?\]\]")

# Repository sources obey G-06 lower_snake_case naming. Publishing deliberately
# maps them to the human-facing filenames expected by GitHub Wiki.
SOURCE_TO_WIKI = {
    "home.md": "Home.md",
    "command_reference.md": "Command-Reference.md",
    "feature_index.md": "Feature-Index.md",
    "sheets_config_reference.md": "Sheets-&-Config-Reference.md",
    "operations_runbook.md": "Operations-Runbook.md",
    "coreops_runtime.md": "CoreOps-&-Runtime.md",
    "onboarding_ticket_flows.md": "Onboarding-&-Ticket-Flows.md",
    "recruitment_clan_tools.md": "Recruitment-&-Clan-Tools.md",
    "placement_reservations.md": "Placement-&-Reservations.md",
    "housekeeping_maintenance.md": "Housekeeping-&-Maintenance.md",
    "community_features_events.md": "Community-Features-&-Events.md",
    "discord_roles_permissions.md": "Discord-Roles-&-Permissions.md",
    "troubleshooting.md": "Troubleshooting.md",
    "_sidebar.md": "_Sidebar.md",
}


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
    unmapped = filenames - SOURCE_TO_WIKI.keys()
    missing_sources = SOURCE_TO_WIKI.keys() - filenames
    for name in sorted(unmapped):
        errors.append(f"wiki source has no publish mapping: docs/wiki/{name}")
    for name in sorted(missing_sources):
        errors.append(f"publish mapping has no wiki source: docs/wiki/{name}")

    published_filenames = set(SOURCE_TO_WIKI.values())
    canonical: dict[str, Path] = {}
    for page in pages:
        published_name = SOURCE_TO_WIKI.get(page.name, page.name)
        key = canonical_page_name(Path(published_name).stem)
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
            if expected not in published_filenames:
                errors.append(
                    f"unresolved wiki link in {page.relative_to(ROOT)}: "
                    f"[[{target}]] -> published wiki page {expected}"
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
