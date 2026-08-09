from __future__ import annotations

"""Async wrappers for Google Sheets access built on :mod:`shared.sheets.core`."""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterator, ParamSpec, TypeVar

import shared.sheets.core as _core

P = ParamSpec("P")
T = TypeVar("T")


@dataclass
class SheetReadScope:
    """Per-operation read cache used only while ``sheet_read_scope`` is active."""

    values: dict[tuple[object, ...], Any] = field(default_factory=dict)
    inflight: dict[tuple[object, ...], asyncio.Task[Any]] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0


_read_scope: ContextVar[SheetReadScope | None] = ContextVar(
    "sheets_read_scope", default=None
)


@contextmanager
def sheet_read_scope() -> Iterator[SheetReadScope]:
    """De-duplicate identical Sheet reads for one logical async operation.

    This is deliberately short-lived. It does not make data stale across user
    actions or background jobs; callers opt in around a single coherent workflow.
    Nested scopes reuse the outer scope so helper layers do not spend the same read
    budget repeatedly.
    """

    current = _read_scope.get()
    if current is not None:
        yield current
        return

    state = SheetReadScope()
    token = _read_scope.set(state)
    try:
        yield state
    finally:
        _read_scope.reset(token)


async def _scoped_read(
    key: tuple[object, ...], loader: Callable[[], Awaitable[T]]
) -> T:
    state = _read_scope.get()
    if state is None:
        return await loader()

    if key in state.values:
        state.hits += 1
        return state.values[key]

    existing = state.inflight.get(key)
    if existing is not None:
        state.hits += 1
        return await existing

    state.misses += 1
    task = asyncio.create_task(loader())
    state.inflight[key] = task
    try:
        result = await task
    finally:
        state.inflight.pop(key, None)
    state.values[key] = result
    return result


async def aopen_by_key(
    sheet_id: str | None = None, *, timeout: float | None = None
) -> Any:
    """Open a spreadsheet by key without blocking the event loop."""

    return await _core.aopen_by_key(sheet_id, timeout=timeout)


async def aget_worksheet(
    sheet_id: str, name: str, *, timeout: float | None = None
) -> Any:
    """Fetch a worksheet handle using the shared cache without blocking."""

    return await _core.aget_worksheet(sheet_id, name, timeout=timeout)


async def afetch_records(
    sheet_id: str, worksheet: str, *, timeout: float | None = None
) -> list[dict[str, Any]]:
    """Return worksheet records asynchronously with retry semantics."""

    return await _scoped_read(
        ("records", str(sheet_id), str(worksheet)),
        lambda: _core.afetch_records(sheet_id, worksheet, timeout=timeout),
    )


async def afetch_values(
    sheet_id: str, worksheet: str, *, timeout: float | None = None
) -> list[list[Any]]:
    """Return worksheet values asynchronously with retry semantics."""

    return await _scoped_read(
        ("values", str(sheet_id), str(worksheet)),
        lambda: _core.afetch_values(sheet_id, worksheet, timeout=timeout),
    )


async def asheets_read(
    sheet_id: str,
    a1_range: str,
    *,
    timeout: float | None = None,
) -> Any:
    """Read an arbitrary ``a1_range`` without blocking the event loop."""

    return await _scoped_read(
        ("range", str(sheet_id), str(a1_range)),
        lambda: _core.asheets_read(sheet_id, a1_range, timeout=timeout),
    )


async def acall_with_backoff(
    func: Callable[P, T],
    *args: P.args,
    attempts: int | None = None,
    base_delay: float | None = None,
    factor: float | None = None,
    timeout: float | None = None,
    **kwargs: P.kwargs,
) -> T:
    """Execute ``func`` in the Sheets executor with async backoff."""

    return await _core.acall_with_backoff(
        func,
        *args,
        attempts=attempts,
        base_delay=base_delay,
        factor=factor,
        timeout=timeout,
        **kwargs,
    )


async def a_to_thread_with_backoff(
    func: Callable[P, T],
    *args: P.args,
    attempts: int | None = None,
    base_delay: float | None = None,
    factor: float | None = None,
    timeout: float | None = None,
    **kwargs: P.kwargs,
) -> T:
    """Run a synchronous Sheets callable off-loop with async retry/backoff."""

    async def _invoke() -> T:
        if timeout is None:
            return await _core.async_adapter.arun(func, *args, **kwargs)
        return await _core.async_adapter.arun(func, *args, timeout=timeout, **kwargs)

    return await _core._retry_with_backoff_async(
        _invoke,
        attempts=attempts,
        base_delay=base_delay,
        factor=factor,
    )


__all__ = [
    "SheetReadScope",
    "sheet_read_scope",
    "aopen_by_key",
    "aget_worksheet",
    "afetch_records",
    "afetch_values",
    "asheets_read",
    "acall_with_backoff",
    "a_to_thread_with_backoff",
]
