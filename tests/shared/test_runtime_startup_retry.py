import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.common import runtime


class _RetryableLoginError(asyncio.TimeoutError):
    pass


class _FakeBot:
    def __init__(self, outcomes: list[object], *, label: str) -> None:
        self._outcomes = list(outcomes)
        self._closed = False
        self.start_calls = 0
        self.close_calls = 0
        self.label = label
        self.user = None
        self.http = SimpleNamespace(_HTTPClient__session=None)

    def is_closed(self) -> bool:
        return self._closed

    async def start(self, _token: str) -> None:
        self.start_calls += 1
        if not self._outcomes:
            return
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return None

    async def close(self) -> None:
        self.close_calls += 1
        self._closed = True


def _patch_runtime_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime.Runtime, "start_webserver", AsyncMock())
    monkeypatch.setattr(runtime, "rehydrate_tiers", lambda _bot: None)
    monkeypatch.setattr(runtime, "audit_tiers", lambda _bot, _logger: None)
    monkeypatch.setattr(
        runtime.shared_config, "merge_onboarding_config_early", lambda: 0
    )
    monkeypatch.setattr(
        "shared.sheets.cache_scheduler.ensure_cache_registration", lambda: None
    )
    monkeypatch.setattr(
        "shared.sheets.cache_scheduler.preload_on_startup", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "shared.sheets.cache_scheduler.register_refresh_job",
        lambda rt, bucket, interval, cadence_label: (
            SimpleNamespace(bucket=bucket, cadence_label=cadence_label),
            rt.scheduler.every(seconds=60, name=f"cache_{bucket}"),
        ),
    )
    monkeypatch.setattr(
        "shared.sheets.cache_scheduler.emit_schedule_log", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        "modules.ops.server_map.schedule_server_map_job", lambda _runtime: None
    )
    monkeypatch.setattr(
        "modules.community.leagues.schedule_leagues_jobs", lambda _runtime: None
    )
    monkeypatch.setattr(
        "modules.community.fusion.scheduler.schedule_fusion_jobs", lambda _runtime: None
    )


def test_runtime_startup_retry_rebuilds_bot_per_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def runner() -> None:
        _patch_runtime_startup(monkeypatch)
        sleep_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(runtime, "_sleep_startup_retry_backoff", sleep_mock)

        setup_calls: list[str] = []
        setup_mock = AsyncMock(side_effect=lambda: setup_calls.append("ok"))
        monkeypatch.setattr(runtime.Runtime, "_run_startup_setup", setup_mock)

        bot_attempt_1 = _FakeBot([_RetryableLoginError()], label="attempt-1")
        bot_attempt_2 = _FakeBot([_RetryableLoginError()], label="attempt-2")
        bot_attempt_3 = _FakeBot([None], label="attempt-3")
        built = [bot_attempt_2, bot_attempt_3]
        rebuilt: list[_FakeBot] = []

        rt = runtime.Runtime(
            bot=bot_attempt_1,
            bot_factory=lambda: built.pop(0),
            bot_rebuild_hook=lambda new_bot: rebuilt.append(new_bot),  # type: ignore[arg-type]
        )

        await rt.start("token")

        assert bot_attempt_1.start_calls == 1
        assert bot_attempt_2.start_calls == 1
        assert bot_attempt_3.start_calls == 1
        assert bot_attempt_1.close_calls == 1
        assert bot_attempt_2.close_calls == 1
        assert bot_attempt_3.close_calls == 0
        assert rebuilt == [bot_attempt_2, bot_attempt_3]
        assert rt.bot is bot_attempt_3
        assert sleep_mock.await_count == 2
        assert setup_mock.await_count == 3

    asyncio.run(runner())


def test_runtime_startup_retry_requires_factory_for_rebuild(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def runner() -> None:
        _patch_runtime_startup(monkeypatch)
        monkeypatch.setattr(
            runtime, "_sleep_startup_retry_backoff", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(runtime.Runtime, "_run_startup_setup", AsyncMock(return_value=None))
        bot = _FakeBot([_RetryableLoginError()], label="attempt-1")
        rt = runtime.Runtime(bot=bot)

        with pytest.raises(RuntimeError, match="no bot_factory is configured"):
            await rt.start("token")
        assert bot.start_calls == 1
        assert bot.close_calls == 1

    asyncio.run(runner())


def test_runtime_startup_post_login_failure_does_not_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def runner() -> None:
        _patch_runtime_startup(monkeypatch)
        monkeypatch.setattr(
            runtime, "_sleep_startup_retry_backoff", AsyncMock(return_value=None)
        )
        setup_mock = AsyncMock(return_value=None)
        monkeypatch.setattr(runtime.Runtime, "_run_startup_setup", setup_mock)

        class _PostLoginFailure(RuntimeError):
            pass

        bot = _FakeBot([None], label="attempt-1")

        async def _start(_token: str) -> None:
            bot.start_calls += 1
            bot.user = object()
            raise _PostLoginFailure("after login")

        bot.start = _start  # type: ignore[assignment]
        rt = runtime.Runtime(bot=bot, bot_factory=lambda: _FakeBot([], label="never"))

        with pytest.raises(_PostLoginFailure):
            await rt.start("token")
        assert bot.start_calls == 1
        assert setup_mock.await_count == 1
        assert bot.close_calls == 0

    asyncio.run(runner())
