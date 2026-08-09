"""Short-lived read de-duplication for one Live Arena logical operation.

This is intentionally not a cross-operation cache.  It only reuses successful
Sheet reads while a caller holds ``read_scope()`` so correctness-sensitive
mutations still begin from fresh workbook state on their next operation.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Iterator

from shared.sheets.async_core import afetch_values as _raw_afetch_values


@dataclass
class ReadScopeState:
    values: dict[tuple[str, str], list[list[object]]] = field(default_factory=dict)
    inflight: dict[tuple[str, str], asyncio.Task] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0


_scope: ContextVar[ReadScopeState | None] = ContextVar(
    "live_arena_read_scope", default=None
)


@contextmanager
def read_scope() -> Iterator[ReadScopeState]:
    """Reuse identical Live Arena tab reads for one logical operation."""

    current = _scope.get()
    if current is not None:
        yield current
        return

    state = ReadScopeState()
    token = _scope.set(state)
    try:
        yield state
    finally:
        _scope.reset(token)


async def afetch_values(
    sheet_id: str, worksheet: str, *, timeout: float | None = None
) -> list[list[object]]:
    """Fetch values, de-duplicating identical reads inside ``read_scope``.

    Failed reads are never cached. Concurrent callers for the same tab share the
    same in-flight request, which prevents a gather from accidentally spending
    the read budget twice on identical data.
    """

    state = _scope.get()
    if state is None:
        return await _raw_afetch_values(sheet_id, worksheet, timeout=timeout)

    key = (str(sheet_id), str(worksheet))
    if key in state.values:
        state.hits += 1
        return state.values[key]

    existing = state.inflight.get(key)
    if existing is not None:
        state.hits += 1
        return await existing

    state.misses += 1
    task = asyncio.create_task(
        _raw_afetch_values(sheet_id, worksheet, timeout=timeout)
    )
    state.inflight[key] = task
    try:
        result = await task
    finally:
        state.inflight.pop(key, None)
    state.values[key] = result
    return result
