"""Small read-side extension needed by the Swiss service.

The base LiveArenaRepository owns participant/audit persistence but historically did not
expose the global AVAILABILITY_SLOTS table. Swiss pairing needs that table only to
annotate an already-selected opponent pairing with shared scheduling windows.
"""

from __future__ import annotations

from shared.sheets.async_core import afetch_values

from modules.community.live_arena.repository import LiveArenaRepository
from modules.community.live_arena.service import (
    AVAILABILITY_SLOT_HEADERS,
    _rows,
    load_config,
)

_installed = False


def install() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    if callable(getattr(LiveArenaRepository, "availability_slots", None)):
        return

    async def availability_slots(self: LiveArenaRepository):
        config = self.config or await load_config(self.sheet_id)
        tab = config["AVAILABILITY_SLOTS_TAB"]
        return _rows(
            await afetch_values(self.sheet_id, tab) or [],
            AVAILABILITY_SLOT_HEADERS,
            tab,
        )

    LiveArenaRepository.availability_slots = availability_slots


install()
