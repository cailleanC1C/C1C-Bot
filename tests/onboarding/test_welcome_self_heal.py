from __future__ import annotations

from types import SimpleNamespace

import pytest

from modules.onboarding import welcome_self_heal as self_heal


@pytest.mark.asyncio
async def test_initialize_legacy_watcher_closes_first_ready_race():
    class Watcher:
        def __init__(self):
            self.channel_id = None
            self._onb_registered = False
            self.calls = 0

        async def on_ready(self):
            self.calls += 1
            self.channel_id = 123
            self._onb_registered = True

    watcher = Watcher()
    bot = SimpleNamespace(get_cog=lambda name: watcher if name == "WelcomeWatcher" else None)

    assert await self_heal._initialize_legacy_watcher(bot) is True
    assert watcher.calls == 1
    assert watcher.channel_id == 123


@pytest.mark.asyncio
async def test_initialize_legacy_watcher_does_not_repeat_ready_when_initialized():
    class Watcher:
        channel_id = 123
        _onb_registered = True
        calls = 0

        async def on_ready(self):
            self.calls += 1

    watcher = Watcher()
    bot = SimpleNamespace(get_cog=lambda name: watcher if name == "WelcomeWatcher" else None)

    assert await self_heal._initialize_legacy_watcher(bot) is True
    assert watcher.calls == 0


@pytest.mark.asyncio
async def test_ensure_onboarding_started_creates_missing_panel(monkeypatch):
    thread = SimpleNamespace(id=42, name="W1014-Echo14")
    welcome = SimpleNamespace(id=88)
    outcome = SimpleNamespace(
        result="panel_created", reason=None, panel_message_id=99
    )
    captured = {}

    monkeypatch.setattr(self_heal, "_feature_enabled", lambda: True)
    monkeypatch.setattr(self_heal, "_is_target_thread", lambda value: value is thread)

    async def locate(value):
        assert value is thread
        return welcome

    async def resolve(value, *, bot_user_id=None):
        assert value is thread
        return 777

    async def post(bot, value, **kwargs):
        captured.update(kwargs)
        assert value is thread
        return outcome

    monkeypatch.setattr(self_heal, "locate_welcome_message", locate)
    monkeypatch.setattr(self_heal, "resolve_subject_user_id", resolve)
    monkeypatch.setattr(self_heal, "post_open_questions_panel", post)

    bot = SimpleNamespace(user=SimpleNamespace(id=1))
    result = await self_heal.ensure_onboarding_started(
        bot, thread, source="startup_reconcile"
    )

    assert result == "started"
    assert captured["trigger_message"] is welcome
    assert captured["subject_user_id"] == 777
    assert captured["flow"] == "welcome"


@pytest.mark.asyncio
async def test_ensure_onboarding_started_treats_existing_panel_as_success(monkeypatch):
    thread = SimpleNamespace(id=42, name="W1012-MoonFever")
    welcome = SimpleNamespace(id=88)

    monkeypatch.setattr(self_heal, "_feature_enabled", lambda: True)
    monkeypatch.setattr(self_heal, "_is_target_thread", lambda value: True)

    async def resolve(*args, **kwargs):
        return 777

    async def post(*args, **kwargs):
        return SimpleNamespace(
            result="skipped", reason="panel_exists", panel_message_id=99
        )

    monkeypatch.setattr(self_heal, "resolve_subject_user_id", resolve)
    monkeypatch.setattr(self_heal, "post_open_questions_panel", post)

    result = await self_heal.ensure_onboarding_started(
        SimpleNamespace(user=SimpleNamespace(id=1)),
        thread,
        source="reaction",
        trigger_message=welcome,
    )

    assert result == "already_present"


def test_supported_wake_emojis_include_current_and_legacy_ticket_reactions():
    assert "👍" in self_heal._SUPPORTED_WAKE_EMOJIS
    assert "🎫" in self_heal._SUPPORTED_WAKE_EMOJIS
    assert "🎟️" in self_heal._SUPPORTED_WAKE_EMOJIS
