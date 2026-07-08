import asyncio
import importlib.util
import logging
import os
import sys
import time
import types
from unittest.mock import AsyncMock

from pathlib import Path

import pytest

os.environ.setdefault("DISCORD_TOKEN", "test-token")
os.environ.setdefault("GSPREAD_CREDENTIALS", "{}")
os.environ.setdefault("RECRUITMENT_SHEET_ID", "sheet-id")
os.environ.setdefault("ONBOARDING_SHEET_ID", "onb-sheet-id-abcdef")

if "modules.common.runtime" not in sys.modules:
    runtime_stub = types.ModuleType("modules.common.runtime")

    def _monotonic_ms() -> int:
        return int(time.time() * 1000)

    runtime_stub.monotonic_ms = _monotonic_ms  # type: ignore[attr-defined]
    sys.modules["modules.common.runtime"] = runtime_stub

    modules_common = types.ModuleType("modules.common")
    modules_common.runtime = runtime_stub  # type: ignore[attr-defined]
    sys.modules.setdefault("modules.common", modules_common)

if "modules.common.logs" not in sys.modules:
    logs_module = types.ModuleType("modules.common.logs")

    class _LogStub:
        def human(self, *_args, **_kwargs) -> None:
            return None

    logs_module.log = _LogStub()  # type: ignore[attr-defined]
    sys.modules["modules.common.logs"] = logs_module
    modules_common = sys.modules.setdefault("modules.common", types.ModuleType("modules.common"))
    modules_common.logs = logs_module  # type: ignore[attr-defined]

modules_pkg = sys.modules.setdefault("modules", types.ModuleType("modules"))
if not hasattr(modules_pkg, "__path__"):
    modules_pkg.__path__ = []  # type: ignore[attr-defined]
onboarding_pkg = sys.modules.setdefault(
    "modules.onboarding", types.ModuleType("modules.onboarding")
)
if not hasattr(onboarding_pkg, "__path__"):
    onboarding_pkg.__path__ = []  # type: ignore[attr-defined]
modules_pkg.onboarding = onboarding_pkg  # type: ignore[attr-defined]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCHEMA_PATH = _REPO_ROOT / "modules" / "onboarding" / "schema.py"
_spec = importlib.util.spec_from_file_location("modules.onboarding.schema", _SCHEMA_PATH)
if _spec is None or _spec.loader is None:  # pragma: no cover - defensive guard
    raise RuntimeError("failed to load onboarding schema module for tests")
onboarding_schema = importlib.util.module_from_spec(_spec)
sys.modules["modules.onboarding.schema"] = onboarding_schema
_spec.loader.exec_module(onboarding_schema)
onboarding_pkg.schema = onboarding_schema  # type: ignore[attr-defined]

from shared import config as shared_config
import shared.sheets.cache_service as cache_service
from shared.sheets import cache_scheduler, onboarding_questions
from shared.sheets.cache_service import cache, register_onboarding_questions_bucket


@pytest.fixture(autouse=True)
def _register_bucket() -> None:
    register_onboarding_questions_bucket()
    bucket = cache.get_bucket("onboarding_questions")
    if bucket is not None:
        bucket.value = None
        bucket.last_refresh = None
        bucket.last_result = None
        bucket.last_error = None
        bucket.last_item_count = None


def test_startup_preload_success(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    async def runner() -> None:
        sample_rows = ({"flow": "welcome", "order": "1", "qid": "ign", "label": "IGN", "type": "short"},)
        monkeypatch.setattr(
            "shared.sheets.onboarding_questions.fetch_question_rows_async",
            AsyncMock(return_value=sample_rows),
        )
        monkeypatch.setitem(shared_config._CONFIG, "ONBOARDING_TAB", "Questions")
        monkeypatch.setitem(
            shared_config._CONFIG,
            "ONBOARDING_SHEET_ID",
            "onb-sheet-id-abcdef",
        )

        caplog.set_level(logging.INFO, logger="shared.sheets.cache_service")

        await cache.refresh_now("onboarding_questions", actor="startup")

        bucket = cache.get_bucket("onboarding_questions")
        assert bucket is not None
        assert bucket.last_result in {"ok", "retry_ok"}
        assert bucket.last_item_count == 1

        messages = [record.getMessage() for record in caplog.records if "bucket=onboarding_questions" in record.getMessage()]
        assert messages, "expected refresh log entry"
        line = messages[-1]
        assert "trigger=manual" in line
        assert "actor=startup" in line
        assert "result=ok" in line
        assert "count=1" in line
        assert "error=-" in line

        snapshot = cache_service.get_bucket_snapshot("onboarding_questions")
        assert snapshot.get("metadata", {}).get("sheet") == "…abcdef"
        assert snapshot.get("metadata", {}).get("tab") == "Questions"

    asyncio.run(runner())


def test_startup_preload_missing_config(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    async def runner() -> None:
        async def _no_wait(_seconds: float) -> None:  # pragma: no cover - deterministic sleep
            return None

        monkeypatch.setattr(cache_service.asyncio, "sleep", _no_wait)
        monkeypatch.setattr(
            "shared.sheets.onboarding_questions.fetch_question_rows_async",
            AsyncMock(side_effect=KeyError("missing config key: ONBOARDING_TAB")),
        )
        monkeypatch.setitem(
            shared_config._CONFIG,
            "ONBOARDING_SHEET_ID",
            "onb-sheet-id-abcdef",
        )

        caplog.set_level(logging.INFO, logger="shared.sheets.cache_service")

        await cache.refresh_now("onboarding_questions", actor="startup")

        bucket = cache.get_bucket("onboarding_questions")
        assert bucket is not None
        assert bucket.last_result == "fail"
        assert str(bucket.last_error or "").startswith("missing config key: ONBOARDING_TAB")

        messages = [record.getMessage() for record in caplog.records if "bucket=onboarding_questions" in record.getMessage()]
        assert messages, "expected refresh log entry"
        line = messages[-1]
        assert "trigger=manual" in line
        assert "actor=startup" in line
        assert "result=fail" in line
        assert "error=missing config key: ONBOARDING_TAB" in line

    asyncio.run(runner())


def test_preload_on_startup_refreshes_onboarding(monkeypatch: pytest.MonkeyPatch) -> None:
    async def runner() -> None:
        calls: list[tuple[str, str | None, str]] = []

        async def fake_refresh(
            name: str,
            *,
            actor: str | None = None,
            trigger: str = "manual",
        ) -> None:
            calls.append((name, actor, trigger))

        monkeypatch.setattr(cache_scheduler, "ensure_cache_registration", lambda: None)
        monkeypatch.setattr(cache_scheduler.cache, "refresh_now", fake_refresh)
        monkeypatch.setattr(cache_scheduler.asyncio, "sleep", AsyncMock(return_value=None))

        await cache_scheduler.preload_on_startup()

        expected = [
            (bucket, "startup", "manual") for bucket, _delay in cache_scheduler.STARTUP_BUCKETS
        ]
        assert calls == expected

    asyncio.run(runner())


def test_schema_loader_refreshes_when_cache_cold(monkeypatch: pytest.MonkeyPatch) -> None:
    async def runner() -> None:
        onboarding_schema._clear_welcome_questions_cache()

        monkeypatch.setattr(onboarding_questions, "register_cache_buckets", lambda: None)
        monkeypatch.setattr(onboarding_questions, "cached_rows", lambda: None)

        refresh_mock = AsyncMock()
        monkeypatch.setattr(onboarding_schema.cache_telemetry, "refresh_now", refresh_mock)
        monkeypatch.setattr(onboarding_schema, "load_welcome_questions", lambda: ["qid"])

        result = await onboarding_schema.get_cached_welcome_questions()

        assert result == ["qid"]
        refresh_mock.assert_awaited_once_with(
            name="onboarding_questions", actor="schema"
        )

    try:
        asyncio.run(runner())
    finally:
        onboarding_schema._clear_welcome_questions_cache()


def test_runtime_startup_preload_staggers_cache_refreshes(monkeypatch: pytest.MonkeyPatch) -> None:
    from modules.common import runtime as runtime_module

    async def runner() -> None:
        sleeps: list[float] = []
        calls: list[str] = []

        class Bot:
            pass

        class Result:
            ok = True
            error = None
            duration_ms = 1
            retries = 0
            snapshot = types.SimpleNamespace(
                available=True,
                last_result="ok",
                last_error=None,
                ttl_expired=False,
                item_count=1,
            )

        monkeypatch.setattr(runtime_module, "get_active_runtime", lambda: types.SimpleNamespace(bot=Bot()))
        monkeypatch.setattr(runtime_module.asyncio, "sleep", AsyncMock(side_effect=lambda sec: sleeps.append(sec)))
        import shared.cache.telemetry as cache_telemetry_module
        monkeypatch.setattr(cache_telemetry_module, "list_buckets", lambda: ["clans", "templates", "clan_tags"])

        async def fake_refresh(name, *, actor):
            calls.append(name)
            return Result()

        monkeypatch.setattr(cache_telemetry_module, "refresh_now", fake_refresh)
        monkeypatch.setattr(runtime_module, "send_log_message", AsyncMock())
        monkeypatch.setattr(runtime_module, "refresh_bucket_results", lambda results: [])

        await runtime_module._startup_preload()

        assert calls == ["clans", "templates", "clan_tags"]
        assert sleeps[:3] == [15, 8, 8]

    asyncio.run(runner())
