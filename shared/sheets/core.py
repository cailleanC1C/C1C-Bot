"""Thin wrappers over gspread to centralise authentication and caching."""

from __future__ import annotations

import asyncio
import json
import os
from functools import lru_cache
from typing import Any, Awaitable, Callable, Dict, Tuple, TypeVar

try:
    import gspread
    from google.oauth2.service_account import Credentials
except Exception as exc:  # pragma: no cover - optional dependency at import time
    gspread = None  # type: ignore
    Credentials = None  # type: ignore
    _IMPORT_ERROR = exc
else:
    _IMPORT_ERROR = None

import shared.sheets.async_adapter as async_adapter

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

_WorksheetT = TypeVar("_WorksheetT")
_WorkbookCache: Dict[str, Any] = {}
_WorksheetCache: Dict[Tuple[str, str], Any] = {}

_CONFIG_CACHE: Any | None = None
_CONFIG_UNAVAILABLE = False

_DEFAULT_ATTEMPTS = int(os.getenv("GSHEETS_RETRY_ATTEMPTS", "5"))
_DEFAULT_BACKOFF_BASE = float(os.getenv("GSHEETS_RETRY_BASE", "0.5"))
_DEFAULT_BACKOFF_FACTOR = float(os.getenv("GSHEETS_RETRY_FACTOR", "2.0"))


def _service_account_info() -> dict[str, Any]:
    raw = (
        os.getenv("GSPREAD_CREDENTIALS")
        or os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")
        or ""
    )
    if not raw:
        raise RuntimeError("GSPREAD_CREDENTIALS/GOOGLE_SERVICE_ACCOUNT_JSON not set")
    return json.loads(raw)


@lru_cache(maxsize=1)
def get_service_account_client():
    if gspread is None or Credentials is None:
        raise RuntimeError("gspread is not installed") from _IMPORT_ERROR
    info = _service_account_info()
    creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    return gspread.authorize(creds)


def _retry_with_backoff(
    func: Callable[..., _WorksheetT],
    *args: Any,
    attempts: int | None = None,
    base_delay: float | None = None,
    factor: float | None = None,
    **kwargs: Any,
) -> _WorksheetT:
    """Retry ``func`` using exponential backoff on failure."""

    tries = attempts or _DEFAULT_ATTEMPTS
    delay = base_delay if base_delay is not None else _DEFAULT_BACKOFF_BASE
    multiplier = factor if factor is not None else _DEFAULT_BACKOFF_FACTOR

    if tries <= 0:
        raise ValueError("attempts must be positive")

    last_exc: Exception | None = None
    for attempt in range(tries):
        try:
            return func(*args, **kwargs)
        except Exception as exc:  # pragma: no cover - network/Sheets failures
            last_exc = exc
            if attempt >= tries - 1:
                raise
            _sleep_with_new_loop(max(0.0, delay))
            delay *= multiplier if multiplier > 1 else 1
    if last_exc is not None:  # pragma: no cover - defensive
        raise last_exc
    raise RuntimeError("_retry_with_backoff exhausted without executing")


async def _retry_with_backoff_async(
    func: Callable[..., Awaitable[_WorksheetT]],
    *args: Any,
    attempts: int | None = None,
    base_delay: float | None = None,
    factor: float | None = None,
    **kwargs: Any,
) -> _WorksheetT:
    """Async variant of :func:`_retry_with_backoff` using ``asyncio.sleep``."""

    tries = attempts or _DEFAULT_ATTEMPTS
    delay = base_delay if base_delay is not None else _DEFAULT_BACKOFF_BASE
    multiplier = factor if factor is not None else _DEFAULT_BACKOFF_FACTOR

    if tries <= 0:
        raise ValueError("attempts must be positive")

    last_exc: Exception | None = None
    for attempt in range(tries):
        try:
            return await func(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - network/Sheets failures
            last_exc = exc
            if attempt >= tries - 1:
                raise
            await asyncio.sleep(max(0.0, delay))
            delay *= multiplier if multiplier > 1 else 1
    if last_exc is not None:  # pragma: no cover - defensive
        raise last_exc
    raise RuntimeError("_retry_with_backoff_async exhausted without executing")


def _sleep_with_new_loop(delay: float) -> None:
    """Sleep for ``delay`` seconds without relying on ``time.sleep``."""

    if delay <= 0:
        return

    try:
        asyncio.run(asyncio.sleep(delay))
    except RuntimeError as exc:  # pragma: no cover - unexpected event loop reuse
        raise RuntimeError(
            "_retry_with_backoff must not run inside an active event loop; use the async variant"
        ) from exc


def _resolve_sheet_id(sheet_id: str | None) -> str:
    """Resolve ``sheet_id``, requiring callers to provide an explicit value."""

    if sheet_id is None or not str(sheet_id).strip():
        raise RuntimeError("sheet id not provided")
    return str(sheet_id).strip()


def open_by_key(sheet_id: str | None = None):
    resolved = _resolve_sheet_id(sheet_id)
    if resolved in _WorkbookCache:
        return _WorkbookCache[resolved]
    client = get_service_account_client()
    workbook = _retry_with_backoff(
        async_adapter.open_spreadsheet, client, resolved
    )
    _WorkbookCache[resolved] = workbook
    return workbook


async def aopen_by_key(sheet_id: str | None = None, *, timeout: float | None = None):
    """Async variant of :func:`open_by_key` using the async adapter."""

    resolved = _resolve_sheet_id(sheet_id)
    if resolved in _WorkbookCache:
        return _WorkbookCache[resolved]

    # ``get_service_account_client`` performs OAuth credential initialisation which
    # may block while ``gspread`` loads service account data. Run it in the Sheets
    # executor so the event loop stays responsive on first use.
    run_kwargs: dict[str, Any] = {}
    if timeout is not None:
        run_kwargs["timeout"] = timeout

    client = await async_adapter.arun(get_service_account_client)

    workbook = await _retry_with_backoff_async(
        async_adapter.aopen_spreadsheet, client, resolved, **run_kwargs
    )
    _WorkbookCache[resolved] = workbook
    return workbook


def get_worksheet(sheet_id: str, name: str):
    """Return a cached ``gspread.Worksheet`` for ``sheet_id`` + ``name``."""

    key = (sheet_id, name)
    if key in _WorksheetCache:
        return _WorksheetCache[key]
    workbook = open_by_key(sheet_id)
    worksheet = _retry_with_backoff(
        async_adapter.worksheet_by_title, workbook, name
    )
    _WorksheetCache[key] = worksheet
    return worksheet


async def aget_worksheet(
    sheet_id: str,
    name: str,
    *,
    timeout: float | None = None,
) -> Any:
    """Async variant of :func:`get_worksheet` using the async adapter."""

    key = (sheet_id, name)
    if key in _WorksheetCache:
        return _WorksheetCache[key]

    workbook = await aopen_by_key(sheet_id, timeout=timeout)
    kwargs: dict[str, Any] = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    worksheet = await _retry_with_backoff_async(
        async_adapter.aworksheet_by_title, workbook, name, **kwargs
    )
    _WorksheetCache[key] = worksheet
    return worksheet


def fetch_records(sheet_id: str, worksheet: str):
    ws = get_worksheet(sheet_id, worksheet)
    return _retry_with_backoff(async_adapter.worksheet_records_all, ws)


async def afetch_records(
    sheet_id: str, worksheet: str, *, timeout: float | None = None
) -> list[dict[str, Any]]:
    """Async wrapper around :func:`fetch_records`."""

    ws = await aget_worksheet(sheet_id, worksheet, timeout=timeout)
    kwargs: dict[str, Any] = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return await _retry_with_backoff_async(
        async_adapter.aworksheet_records_all, ws, **kwargs
    )


def fetch_values(sheet_id: str, worksheet: str):
    ws = get_worksheet(sheet_id, worksheet)
    return _retry_with_backoff(async_adapter.worksheet_values_all, ws)


async def afetch_values(
    sheet_id: str, worksheet: str, *, timeout: float | None = None
) -> list[list[Any]]:
    """Async wrapper around :func:`fetch_values`."""

    ws = await aget_worksheet(sheet_id, worksheet, timeout=timeout)
    kwargs: dict[str, Any] = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return await _retry_with_backoff_async(
        async_adapter.aworksheet_values_all, ws, **kwargs
    )


def sheets_read(sheet_id: str, a1_range: str):
    """Read a specific ``a1_range`` from ``sheet_id`` with retry semantics."""

    workbook = open_by_key(sheet_id)
    worksheet = None
    cell_range = a1_range
    if "!" in a1_range:
        worksheet_name, cell_range = a1_range.split("!", 1)
        worksheet_name = worksheet_name.strip()
        if worksheet_name:
            worksheet = _retry_with_backoff(
                async_adapter.worksheet_by_title, workbook, worksheet_name
            )
        else:
            worksheet = getattr(workbook, "sheet1", None)
    else:
        worksheet = getattr(workbook, "sheet1", None)

    if worksheet is None:
        worksheet = _retry_with_backoff(
            async_adapter.worksheet_by_index, workbook, 0
        )

    if not cell_range:
        return _retry_with_backoff(async_adapter.worksheet_values_all, worksheet)
    return _retry_with_backoff(
        async_adapter.worksheet_values_get, worksheet, cell_range
    )


async def asheets_read(
    sheet_id: str,
    a1_range: str,
    *,
    timeout: float | None = None,
) -> Any:
    """Async variant of :func:`sheets_read`."""

    workbook = await aopen_by_key(sheet_id, timeout=timeout)
    worksheet = None
    cell_range = a1_range
    kwargs: dict[str, Any] = {}
    if timeout is not None:
        kwargs["timeout"] = timeout

    if "!" in a1_range:
        worksheet_name, cell_range = a1_range.split("!", 1)
        worksheet_name = worksheet_name.strip()
        if worksheet_name:
            worksheet = await _retry_with_backoff_async(
                async_adapter.aworksheet_by_title, workbook, worksheet_name, **kwargs
            )

    if worksheet is None:
        worksheet = await _retry_with_backoff_async(
            async_adapter.aworksheet_by_index, workbook, 0, **kwargs
        )

    if not cell_range:
        return await _retry_with_backoff_async(
            async_adapter.aworksheet_values_all, worksheet, **kwargs
        )
    return await _retry_with_backoff_async(
        async_adapter.aworksheet_values_get, worksheet, cell_range, **kwargs
    )


def call_with_backoff(func: Callable[..., _WorksheetT], *args: Any, **kwargs: Any) -> _WorksheetT:
    """Expose the retry helper for modules performing write operations."""

    return _retry_with_backoff(func, *args, **kwargs)


def _get_config():
    """Return the cached config facade if available without raising."""

    global _CONFIG_CACHE, _CONFIG_UNAVAILABLE

    if _CONFIG_CACHE is not None:
        return _CONFIG_CACHE
    if _CONFIG_UNAVAILABLE:
        return None

    try:
        from shared.config import cfg as imported_cfg
    except Exception:  # pragma: no cover - configuration optional in some contexts
        _CONFIG_UNAVAILABLE = True
        return None

    _CONFIG_CACHE = imported_cfg
    return imported_cfg


def _resolve_default_sheet_id(candidate: str | None) -> str:
    """Resolve ``candidate`` falling back to configured sheet identifiers."""

    text = str(candidate).strip() if candidate is not None else ""
    if text:
        return _resolve_sheet_id(text)

    config = _get_config()
    if config is not None:
        getter = getattr(config, "get", None)
        if callable(getter):
            for key in ("onboarding.sheet_id", "recruitment.sheet_id"):
                value = getter(key)
                if isinstance(value, str) and value.strip():
                    return _resolve_sheet_id(value)

    return _resolve_sheet_id(candidate)


def read_table(
    *,
    sheet_id: str | None = None,
    tab_name: str | None = None,
) -> list[dict[str, Any]]:
    """Return ``get_all_records`` for ``tab_name`` using configured sheet IDs."""

    resolved_sheet_id = _resolve_default_sheet_id(sheet_id)

    if tab_name is None:
        raise ValueError("read_table requires tab_name")

    resolved_tab = str(tab_name).strip()
    if not resolved_tab:
        raise ValueError("read_table requires tab_name")

    return fetch_records(resolved_sheet_id, resolved_tab)


async def acall_with_backoff(
    func: Callable[..., _WorksheetT],
    *args: Any,
    attempts: int | None = None,
    base_delay: float | None = None,
    factor: float | None = None,
    timeout: float | None = None,
    **kwargs: Any,
) -> _WorksheetT:
    """Async helper mirroring :func:`call_with_backoff` using the adapter."""

    async def _invoke() -> _WorksheetT:
        if timeout is None:
            return await async_adapter.arun(func, *args, **kwargs)
        return await async_adapter.arun(func, *args, timeout=timeout, **kwargs)

    return await _retry_with_backoff_async(
        _invoke,
        attempts=attempts,
        base_delay=base_delay,
        factor=factor,
    )
