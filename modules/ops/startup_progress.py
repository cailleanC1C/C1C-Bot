"""Rendering helpers for the editable Discord startup progress message."""

from __future__ import annotations

import os
from collections.abc import Mapping


STARTUP_PHASES = (
    "Core initialization",
    "Schedulers",
    "Watchdog & keepalive",
    "Startup refresh",
)


def deployment_identity(*, version: object, env: object) -> str:
    """Return a compact deployment identity using Render's commit metadata when present."""

    commit = (
        os.getenv("RENDER_GIT_COMMIT")
        or os.getenv("GIT_COMMIT_SHA")
        or os.getenv("COMMIT_SHA")
        or "unknown"
    )
    short_commit = commit[:7] if commit != "unknown" else commit
    return f"commit={short_commit} • version={version or 'dev'} • env={env or 'unknown'}"


def render_startup_progress(
    *,
    identity: str,
    states: Mapping[str, str],
    title: str = "🚀 Woadkeeper starting…",
) -> str:
    """Render the single message that is edited while startup advances."""

    lines = [title, identity, ""]
    for phase in STARTUP_PHASES:
        marker = states.get(phase, "⏳")
        lines.append(f"{marker} {phase}")
    return "\n".join(lines)


def render_startup_ready(*, identity: str, duration_s: float) -> str:
    """Render the final compact deployment header after detailed startup logs land."""

    return "\n".join(
        [
            "✅ Woadkeeper ready",
            identity,
            f"startup={max(0.0, duration_s):.1f}s",
        ]
    )


def render_startup_failed(*, identity: str, phase: str) -> str:
    """Render a durable failure breadcrumb when a fatal startup phase fails."""

    return "\n".join(
        [
            "❌ Woadkeeper startup failed",
            identity,
            f"phase={phase}",
        ]
    )
