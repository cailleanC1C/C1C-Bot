"""Async facade for Google Sheets helpers.

This module mirrors the synchronous APIs exposed by ``shared.sheets.recruitment``
and ``shared.sheets.core``. Each wrapper executes the synchronous helper using
the bounded executor managed by :mod:`shared.sheets.async_adapter` so that async
callers never block the event loop.
"""

from __future__ import annotations

from typing import Any, Callable, ParamSpec, TypeVar

from shared.sheets import async_core as _core_async
from shared.sheets import recruitment as _recruitment_sync
from . import async_adapter as _adapter

P = ParamSpec("P")
T = TypeVar("T")


async def _to_thread(func: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    """Run ``func`` in the shared Sheets executor."""

    return await _adapter.arun(func, *args, **kwargs)


# === Recruitment-facing async wrappers ===
async def fetch_clans(*args: Any, **kwargs: Any) -> Any:
    return await _to_thread(_recruitment_sync.fetch_clans, *args, **kwargs)


async def fetch_clans_async(*args: Any, **kwargs: Any) -> Any:
    """Compatibility alias for :func:`fetch_clans`."""

    return await fetch_clans(*args, **kwargs)


async def fetch_clan_records(*args: Any, **kwargs: Any) -> Any:
    return await _to_thread(_recruitment_sync.get_clan_records, *args, **kwargs)


async def fetch_templates(*args: Any, **kwargs: Any) -> Any:
    return await _to_thread(_recruitment_sync.fetch_templates, *args, **kwargs)


async def fetch_clan_rows(*args: Any, **kwargs: Any) -> Any:
    return await _to_thread(_recruitment_sync.fetch_clan_rows, *args, **kwargs)


async def fetch_welcome_templates(*args: Any, **kwargs: Any) -> Any:
    return await _to_thread(_recruitment_sync.fetch_welcome_templates, *args, **kwargs)


async def get_cached_welcome_templates(*args: Any, **kwargs: Any) -> Any:
    return await _to_thread(_recruitment_sync.get_cached_welcome_templates, *args, **kwargs)


async def fetch_clan_tags_index(*args: Any, **kwargs: Any) -> Any:
    return await _to_thread(_recruitment_sync.fetch_clan_tags_index, *args, **kwargs)


async def get_clan_by_tag(*args: Any, **kwargs: Any) -> Any:
    return await _recruitment_sync.aget_clan_by_tag(*args, **kwargs)


# === Core helpers that touch network/files ===
async def open_by_key(*args: Any, **kwargs: Any) -> Any:
    return await _core_async.aopen_by_key(*args, **kwargs)


async def get_worksheet(*args: Any, **kwargs: Any) -> Any:
    return await _core_async.aget_worksheet(*args, **kwargs)


async def fetch_records(*args: Any, **kwargs: Any) -> Any:
    return await _core_async.afetch_records(*args, **kwargs)


async def fetch_values(*args: Any, **kwargs: Any) -> Any:
    return await _core_async.afetch_values(*args, **kwargs)


async def sheets_read(*args: Any, **kwargs: Any) -> Any:
    return await _core_async.asheets_read(*args, **kwargs)


async def call_with_backoff(*args: Any, **kwargs: Any) -> Any:
    return await _core_async.acall_with_backoff(*args, **kwargs)


__all__ = [
    "fetch_clans",
    "fetch_clans_async",
    "fetch_clan_records",
    "fetch_templates",
    "fetch_clan_rows",
    "fetch_welcome_templates",
    "get_cached_welcome_templates",
    "fetch_clan_tags_index",
    "get_clan_by_tag",
    "open_by_key",
    "get_worksheet",
    "fetch_records",
    "fetch_values",
    "sheets_read",
    "call_with_backoff",
]
