"""Regression coverage for both CoreOpsCog reload command surfaces."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from c1c_coreops import cog as coreops_cog
from c1c_coreops.cog import CoreOpsCog


class _Context:
    def __init__(self) -> None:
        self.author = SimpleNamespace(id=123, display_name="Operator")
        self.messages: list[str] = []

    async def send(self, message: str) -> None:
        self.messages.append(message)


def test_coreops_reload_surfaces_use_async_config_reload(monkeypatch) -> None:
    calls: list[str] = []

    def sync_reload_forbidden() -> None:
        raise AssertionError("CoreOps commands must not call sync config reload")

    async def async_reload() -> dict[str, object]:
        calls.append("async_reload")
        return {}

    config_module = SimpleNamespace(
        reload_config=sync_reload_forbidden,
        areload_config=async_reload,
    )
    monkeypatch.setattr(coreops_cog, "_ensure_config_module", lambda: config_module)

    cog = object.__new__(CoreOpsCog)

    async def runner() -> None:
        root_ctx = _Context()
        ops_ctx = _Context()

        await CoreOpsCog.reload.callback(cog, root_ctx)
        await CoreOpsCog.ops_reload.callback(cog, ops_ctx)

        assert root_ctx.messages and "config reloaded" in root_ctx.messages[-1]
        assert ops_ctx.messages and "config reloaded" in ops_ctx.messages[-1]

    asyncio.run(runner())

    assert calls == ["async_reload", "async_reload"]
