"""Temporary Google Sheets read audit logging.

Enable with ``SHEETS_READ_AUDIT_LOGGING=true``. This module intentionally logs
metadata only: no sheet IDs, cell values, row contents, or secrets.
"""

from __future__ import annotations

import inspect
import logging
import os
import time
from contextlib import contextmanager
from typing import Any, Iterator

log = logging.getLogger("shared.sheets.audit")

_TRUE_VALUES = {"1", "true", "yes", "on", "y"}


def enabled() -> bool:
    return str(os.getenv("SHEETS_READ_AUDIT_LOGGING", "")).strip().lower() in _TRUE_VALUES


def _caller(skip_modules: set[str] | None = None) -> str:
    skip = skip_modules or {__name__, "shared.sheets.core", "shared.sheets.cache_service"}
    for frame in inspect.stack()[2:12]:
        module = inspect.getmodule(frame.frame)
        module_name = module.__name__ if module else "?"
        if module_name not in skip:
            return f"{module_name}.{frame.function}:{frame.lineno}"
    return "unknown"


def _result_summary(value: Any) -> str:
    if value is None:
        return "none"
    if isinstance(value, dict):
        return f"dict:{len(value)}"
    if isinstance(value, (list, tuple, set)):
        return f"{type(value).__name__}:{len(value)}"
    return type(value).__name__


@contextmanager
def log_read(
    *,
    component: str,
    operation: str,
    sheet_source: str = "unknown",
    tab_config_key: str | None = None,
    cache_bucket: str | None = None,
    trigger: str = "unknown",
    cache_status: str | None = None,
    caller: str | None = None,
) -> Iterator[dict[str, Any]]:
    """Log a logical Sheets read when audit logging is explicitly enabled."""

    if not enabled():
        yield {}
        return

    start = time.perf_counter()
    fields: dict[str, Any] = {"result": "unknown"}
    try:
        yield fields
        result = fields.get("result", "ok")
        error_type = "-"
    except Exception as exc:
        result = "fail"
        error_type = exc.__class__.__name__
        raise
    finally:
        duration_ms = int((time.perf_counter() - start) * 1000)
        log.info(
            "[sheets_read_audit] component=%s operation=%s sheet_source=%s "
            "tab_config_key=%s cache_bucket=%s trigger=%s caller=%s cache=%s "
            "duration_ms=%s result=%s error_type=%s",
            component,
            operation,
            sheet_source,
            tab_config_key or "-",
            cache_bucket or "-",
            trigger,
            caller or _caller(),
            cache_status or "-",
            duration_ms,
            result if isinstance(result, str) else _result_summary(result),
            error_type,
        )
