"""Load the Victory Ledger routing keys from the config source that owns them.

The registration repository intentionally carries only the core registration contract.
Victory Ledger presentation also needs ROUND_OVERVIEW_CHANNEL_ID and MESSAGES_TAB,
which live in CONFIG but are not part of LiveArenaRepository.config.  Do not assume
those presentation keys were injected by whichever service happened to initialize the
repository first.
"""

from __future__ import annotations

import logging

from shared.sheets.async_core import afetch_values

from modules.community.live_arena.service import (
    CONFIG_HEADERS,
    CONFIG_TAB,
    LiveArenaConfigError,
    _rows,
    _text,
)

log = logging.getLogger("c1c.community.live_arena.victory_ledger_routing_config")
_installed = False
_ROUTING_KEYS = ("ROUND_OVERVIEW_CHANNEL_ID", "MESSAGES_TAB")


async def _load_workspace_routing(sheet_id: str) -> dict[str, str]:
    """Read only the two CONFIG values owned by the Victory Ledger workspace."""
    matrix = await afetch_values(str(sheet_id), CONFIG_TAB) or []
    rows = _rows(matrix, CONFIG_HEADERS, CONFIG_TAB)
    result: dict[str, str] = {}
    for key in _ROUTING_KEYS:
        matches = [row for row in rows if _text(row["Key"]) == key]
        if len(matches) != 1:
            raise LiveArenaConfigError(f"CONFIG: key {key} must occur exactly once")
        value = _text(matches[0]["Value"])
        if not value:
            raise LiveArenaConfigError(f"CONFIG: missing required key {key}")
        result[key] = value

    try:
        int(result["ROUND_OVERVIEW_CHANNEL_ID"])
    except ValueError as exc:
        raise LiveArenaConfigError(
            "CONFIG: ROUND_OVERVIEW_CHANNEL_ID must be numeric"
        ) from exc
    return result


async def _ensure_workspace_routing(repository, sheet_id: str) -> None:
    config = getattr(repository, "config", None)
    if not config:
        await repository.initialize()
        config = getattr(repository, "config", None) or {}

    missing = [key for key in _ROUTING_KEYS if not _text(config.get(key))]
    if not missing:
        return

    routing = await _load_workspace_routing(sheet_id)
    merged = dict(config)
    merged.update(routing)
    repository.config = merged
    log.info(
        "Live Arena Victory Ledger routing config loaded • sheet_tail=%s • keys=%s",
        str(sheet_id)[-6:],
        ",".join(_ROUTING_KEYS),
    )


async def _ensure_workspace_with_routing(
    original,
    workspace,
    bot,
    sheet_id: str,
    repository=None,
):
    sid = str(sheet_id)
    # Preserve the workspace's own fast path without spending another CONFIG read.
    if workspace._WORKSPACE_CACHE.get(sid) is not None:
        return await original(bot, sid, repository)

    repository = repository or workspace.LiveArenaRepository(sid)
    await _ensure_workspace_routing(repository, sid)
    return await original(bot, sid, repository)


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import victory_ledger_workspace as workspace

    original = workspace.ensure_workspace

    async def ensure_workspace(bot, sheet_id: str, repository=None):
        return await _ensure_workspace_with_routing(
            original,
            workspace,
            bot,
            sheet_id,
            repository,
        )

    workspace.ensure_workspace = ensure_workspace
