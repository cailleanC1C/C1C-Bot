"""Discord admin startup summary renderer."""
from __future__ import annotations


def render_startup_summary(*, sections: dict[str, list[str]]) -> str:
    ordered = ["allow_list", "watchers", "scheduler", "watchdog", "refresh"]
    lines = ["✅ Woadkeeper startup complete", ""]
    first = True
    for key in ordered:
        block = sections.get(key) or ["❌ unknown section", "• missing data"]
        if not first:
            lines.append("")
        lines.extend(block)
        first = False
    return "\n".join(lines)
