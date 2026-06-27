from __future__ import annotations

"""Async wrappers for Google Sheets access built on :mod:`shared.sheets.core`."""

from typing import Any, Callable, ParamSpec, TypeVar

import shared.sheets.core as _core

P = ParamSpec("P")
T = TypeVar("T")


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

    return await _core.afetch_records(sheet_id, worksheet, timeout=timeout)


async def afetch_values(
    sheet_id: str, worksheet: str, *, timeout: float | None = None
) -> list[list[Any]]:
    """Return worksheet values asynchronously with retry semantics."""

    return await _core.afetch_values(sheet_id, worksheet, timeout=timeout)


async def asheets_read(
    sheet_id: str,
    a1_range: str,
    *,
    timeout: float | None = None,
) -> Any:
    """Read an arbitrary ``a1_range`` without blocking the event loop."""

    return await _core.asheets_read(sheet_id, a1_range, timeout=timeout)


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
    "aopen_by_key",
    "aget_worksheet",
    "afetch_records",
    "afetch_values",
    "asheets_read",
    "acall_with_backoff",
    "a_to_thread_with_backoff",
]
