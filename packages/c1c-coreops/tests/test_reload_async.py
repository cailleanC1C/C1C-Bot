from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
import sys


ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "packages" / "c1c-coreops" / "src"
for path in (ROOT, SRC):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from c1c_coreops import cog as coreops_cog  # noqa: E402
from c1c_coreops.cog import CoreOpsCog  # noqa: E402


def test_reload_impl_awaits_async_config_reload(monkeypatch) -> None:
    calls = []

    async def areload_config() -> None:
        calls.append("async")

    async def send(message: str) -> None:
        calls.append(message)

    monkeypatch.setattr(
        coreops_cog,
        "_CONFIG_MODULE",
        SimpleNamespace(areload_config=areload_config),
    )
    cog = CoreOpsCog.__new__(CoreOpsCog)
    ctx = SimpleNamespace(
        author=SimpleNamespace(id=1, display_name="Admin"),
        send=send,
    )

    asyncio.run(cog._reload_impl(ctx, reboot=False))

    assert calls[0] == "async"
    assert str(calls[1]).startswith("config reloaded")
