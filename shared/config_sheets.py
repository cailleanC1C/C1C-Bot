"""Environment-backed Sheet source accessors.

Keep direct environment reads behind the shared config boundary so Sheet helpers
never reach into ``os.environ`` themselves. This module is intentionally small;
the broader import-time config bootstrap is migrated in the next broker phase.
"""

from __future__ import annotations

import os


def get_milestones_config_tab() -> str:
    """Return the explicitly configured Milestones Config source tab."""

    return (os.getenv("MILESTONES_CONFIG_TAB") or "").strip()


__all__ = ["get_milestones_config_tab"]
