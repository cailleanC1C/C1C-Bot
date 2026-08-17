from __future__ import annotations

"""Async Google Sheets facade backed by the process-wide read broker.

Read-side calls in this module are the migration boundary for async runtime code:
physical gspread/adapter reads are single-attempt loaders and the broker owns
pacing, coalescing, quota retry, and optional result caching. Sync helpers and
write helpers remain in :mod:`shared.sheets.core` for compatibility while their
callers are migrated separately.
"""

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterator, ParamSpec, TypeVar

import shared.sheets.core as _core
from shared.sheets.read_broker import (
    CachePolicy,
    FRESH_REQUIRED,
    ReadPriority,
    SheetReadKey,
    broker,
)

P = ParamSpec("P")
T = TypeVar("T")


@dataclass
class SheetReadScope:
    """Per-operation logical cache retained for compatibility.

    The process-wide broker is now the physical-read authority. This short-lived
    scope still avoids repeated logical broker calls inside one coherent workflow.
    """

    values: dict[tuple[object, ...], Any] = field(default_factory=dict)
    inflight: dict[tuple[object, ...], asyncio.Task[Any]] = field(default_factory=dict)
    hits: int = 0
    misses: int = 0


_read_scope: ContextVar[SheetReadScope | None] = ContextVar(
    "sheets_read_scope", default=None
)


@contextmanager
def sheet_read_scope() -> Iterator[SheetReadScope]:
    """De-duplicate identical logical Sheet reads for one async operation."""

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
        return await asyncio.shield(existing)

    state.misses += 1
    task = asyncio.create_task(loader())
    state.inflight[key] = task
    try:
        result = await asyncio.shield(task)
    finally:
        current = state.inflight.get(key)
        if current is task:
            state.inflight.pop(key, None)
    state.values[key] = result
    return result


async def _adapter_call(
    func: Callable[..., Awaitable[T]],
    *args: Any,
    timeout: float | None,
    **kwargs: Any,
) -> T:
    """Invoke one async adapter operation exactly once.

    Retry is deliberately absent here. A loader passed to ``SheetsReadBroker``
    represents one physical Google API attempt; the broker is the only read-side
    quota retry owner.
    """

    if timeout is None:
        return await func(*args, **kwargs)
    return await func(*args, timeout=timeout, **kwargs)


async def aopen_by_key(
    sheet_id: str | None = None,
    *,
    timeout: float | None = None,
    priority: ReadPriority = ReadPriority.NORMAL,
    component: str = "async_core",
    reason: str = "runtime",
) -> Any:
    """Open/cache a workbook with brokered metadata reads on cache miss."""

    resolved = _core._resolve_sheet_id(sheet_id)
    cached = _core._WorkbookCache.get(resolved)
    if cached is not None:
        return cached

    # Credential parsing/client construction is local work, not a Sheets read.
    client = await _core.async_adapter.arun(_core.get_service_account_client)
    key = SheetReadKey(sheet_id=resolved, operation="workbook_open")

    async def _load() -> Any:
        return await _adapter_call(
            _core.async_adapter.aopen_spreadsheet,
            client,
            resolved,
            timeout=timeout,
        )

    workbook = await broker.read(
        key,
        _load,
        policy=FRESH_REQUIRED,
        priority=priority,
        component=component,
        reason=reason,
    )
    _core._WorkbookCache[resolved] = workbook
    return workbook


async def aget_worksheet(
    sheet_id: str,
    name: str,
    *,
    timeout: float | None = None,
    priority: ReadPriority = ReadPriority.NORMAL,
    component: str = "async_core",
    reason: str = "runtime",
) -> Any:
    """Return a worksheet handle with brokered metadata lookup on cache miss."""

    resolved = _core._resolve_sheet_id(sheet_id)
    tab = str(name or "").strip()
    if not tab:
        raise ValueError("worksheet name is required")

    cache_key = (resolved, tab)
    cached = _core._WorksheetCache.get(cache_key)
    if cached is not None:
        return cached

    workbook = await aopen_by_key(
        resolved,
        timeout=timeout,
        priority=priority,
        component=component,
        reason=reason,
    )
    key = SheetReadKey(
        sheet_id=resolved,
        operation="worksheet_lookup",
        worksheet=tab,
    )

    async def _load() -> Any:
        return await _adapter_call(
            _core.async_adapter.aworksheet_by_title,
            workbook,
            tab,
            timeout=timeout,
        )

    worksheet = await broker.read(
        key,
        _load,
        policy=FRESH_REQUIRED,
        priority=priority,
        component=component,
        reason=reason,
    )
    _core._WorksheetCache[cache_key] = worksheet
    return worksheet


async def _worksheet_by_index(
    sheet_id: str,
    index: int,
    *,
    timeout: float | None,
    priority: ReadPriority,
    component: str,
    reason: str,
) -> Any:
    workbook = await aopen_by_key(
        sheet_id,
        timeout=timeout,
        priority=priority,
        component=component,
        reason=reason,
    )
    identity = f"#{int(index)}"
    key = SheetReadKey(
        sheet_id=sheet_id,
        operation="worksheet_lookup_index",
        worksheet=identity,
    )

    async def _load() -> Any:
        return await _adapter_call(
            _core.async_adapter.aworksheet_by_index,
            workbook,
            int(index),
            timeout=timeout,
        )

    return await broker.read(
        key,
        _load,
        policy=FRESH_REQUIRED,
        priority=priority,
        component=component,
        reason=reason,
    )


async def afetch_records(
    sheet_id: str,
    worksheet: str,
    *,
    timeout: float | None = None,
    policy: CachePolicy = FRESH_REQUIRED,
    priority: ReadPriority = ReadPriority.NORMAL,
    component: str = "async_core",
    reason: str = "runtime",
) -> list[dict[str, Any]]:
    """Return worksheet records through the process-wide read broker."""

    resolved = _core._resolve_sheet_id(sheet_id)
    tab = str(worksheet or "").strip()
    key = SheetReadKey.records(resolved, tab)

    async def _read() -> list[dict[str, Any]]:
        ws = await aget_worksheet(
            resolved,
            tab,
            timeout=timeout,
            priority=priority,
            component=component,
            reason=reason,
        )

        async def _load() -> list[dict[str, Any]]:
            return await _adapter_call(
                _core.async_adapter.aworksheet_records_all,
                ws,
                timeout=timeout,
            )

        return await broker.read(
            key,
            _load,
            policy=policy,
            priority=priority,
            component=component,
            reason=reason,
        )

    return await _scoped_read(("broker", key, policy.name), _read)


async def afetch_values(
    sheet_id: str,
    worksheet: str,
    *,
    timeout: float | None = None,
    policy: CachePolicy = FRESH_REQUIRED,
    priority: ReadPriority = ReadPriority.NORMAL,
    component: str = "async_core",
    reason: str = "runtime",
) -> list[list[Any]]:
    """Return worksheet values through the process-wide read broker."""

    resolved = _core._resolve_sheet_id(sheet_id)
    tab = str(worksheet or "").strip()
    key = SheetReadKey.values(resolved, tab)

    async def _read() -> list[list[Any]]:
        ws = await aget_worksheet(
            resolved,
            tab,
            timeout=timeout,
            priority=priority,
            component=component,
            reason=reason,
        )

        async def _load() -> list[list[Any]]:
            return await _adapter_call(
                _core.async_adapter.aworksheet_values_all,
                ws,
                timeout=timeout,
            )

        return await broker.read(
            key,
            _load,
            policy=policy,
            priority=priority,
            component=component,
            reason=reason,
        )

    return await _scoped_read(("broker", key, policy.name), _read)


async def asheets_read(
    sheet_id: str,
    a1_range: str,
    *,
    timeout: float | None = None,
    policy: CachePolicy = FRESH_REQUIRED,
    priority: ReadPriority = ReadPriority.NORMAL,
    component: str = "async_core",
    reason: str = "runtime",
) -> Any:
    """Read an arbitrary A1 range through the process-wide read broker."""

    resolved = _core._resolve_sheet_id(sheet_id)
    raw_range = str(a1_range or "")
    worksheet_name = ""
    cell_range = raw_range

    if "!" in raw_range:
        worksheet_name, cell_range = raw_range.split("!", 1)
        worksheet_name = worksheet_name.strip()

    if worksheet_name:
        ws = await aget_worksheet(
            resolved,
            worksheet_name,
            timeout=timeout,
            priority=priority,
            component=component,
            reason=reason,
        )
        identity = worksheet_name
    else:
        ws = await _worksheet_by_index(
            resolved,
            0,
            timeout=timeout,
            priority=priority,
            component=component,
            reason=reason,
        )
        identity = "#0"

    key = (
        SheetReadKey.range(resolved, identity, cell_range)
        if cell_range
        else SheetReadKey.values(resolved, identity)
    )

    async def _read() -> Any:
        async def _load() -> Any:
            if not cell_range:
                return await _adapter_call(
                    _core.async_adapter.aworksheet_values_all,
                    ws,
                    timeout=timeout,
                )
            return await _adapter_call(
                _core.async_adapter.aworksheet_values_get,
                ws,
                cell_range,
                timeout=timeout,
            )

        return await broker.read(
            key,
            _load,
            policy=policy,
            priority=priority,
            component=component,
            reason=reason,
        )

    return await _scoped_read(("broker", key, policy.name), _read)


async def acall_with_backoff(
    func: Callable[P, T],
    *args: P.args,
    attempts: int | None = None,
    base_delay: float | None = None,
    factor: float | None = None,
    timeout: float | None = None,
    **kwargs: P.kwargs,
) -> T:
    """Execute a generic/write callable with legacy async backoff.

    Read-side callers should use ``afetch_records``, ``afetch_values`` or
    ``asheets_read``. This helper remains for writes and migration compatibility.
    """

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
    """Run a generic synchronous Sheets callable off-loop with legacy backoff."""

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
    "CachePolicy",
    "FRESH_REQUIRED",
    "ReadPriority",
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
