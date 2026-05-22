from types import SimpleNamespace
from unittest.mock import AsyncMock

import asyncio

from modules.onboarding.watcher_promo import PromoTicketWatcher


def test_promo_watcher_on_ready_does_not_send_standalone_runtime_message(monkeypatch) -> None:
    bot = SimpleNamespace(get_channel=lambda *_: None, guilds=[])
    watcher = PromoTicketWatcher(bot)
    monkeypatch.setattr("modules.onboarding.watcher_promo.get_promo_channel_id", lambda: "123")
    monkeypatch.setattr("modules.onboarding.watcher_promo.feature_flags.is_enabled", lambda *_: True)
    runtime_send = AsyncMock()
    monkeypatch.setattr("modules.onboarding.watcher_promo.rt.send_log_message", runtime_send)

    asyncio.run(watcher.on_ready())

    runtime_send.assert_not_awaited()
