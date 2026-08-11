"""Repair the final Live Arena recap from Sheet truth after tournament completion."""

from __future__ import annotations

import logging

from modules.community.live_arena.service import _text

log = logging.getLogger("c1c.community.live_arena.knockout_recap_repair")
_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True

    from modules.community.live_arena import knockout_runtime

    original = knockout_runtime._reconcile_knockout

    async def reconcile_with_completed_recap(manager, service):
        warnings = list(await original(manager, service))
        try:
            _, (_, tournament), _, _ = await service.context()
            status = getattr(tournament, "status", None)
            if status is None and isinstance(tournament, dict):
                status = tournament.get("status")
            if _text(status) != "completed":
                return warnings
            summary = await service.complete_tournament("system")
            await knockout_runtime._sync_final_recap(manager, service, summary)
        except Exception:
            log.exception("Live Arena completed final recap repair failed")
            warnings.append("final tournament recap")
        return list(dict.fromkeys(warnings))

    knockout_runtime._reconcile_knockout = reconcile_with_completed_recap
