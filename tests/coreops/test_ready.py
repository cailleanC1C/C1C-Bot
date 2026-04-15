import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from modules.coreops import ready


def test_on_ready_registers_fusion_persistent_view(monkeypatch):
    async def _run() -> None:
        bot = SimpleNamespace(logger=None)
        monkeypatch.setattr(ready.panels, "register_views", Mock())
        monkeypatch.setattr(ready, "register_persistent_fusion_views", Mock())
        monkeypatch.setattr(ready.watcher_welcome, "setup", AsyncMock())
        monkeypatch.setattr(ready.watcher_promo, "setup", AsyncMock())

        await ready.on_ready(bot)

        ready.register_persistent_fusion_views.assert_called_once_with(bot)

    asyncio.run(_run())
