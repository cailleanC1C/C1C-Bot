import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.onboarding import watcher_welcome


class DummyThread:
    def __init__(
        self, name: str, thread_id: int = 4242, panel_message_id: int = 9999
    ) -> None:
        self.name = name
        self.id = thread_id
        self.panel_message_id = panel_message_id
        self.parent = SimpleNamespace()

    async def send(
        self, *_args, **_kwargs
    ):  # pragma: no cover - patched behavior verified via outcome
        return SimpleNamespace(id=self.panel_message_id)


@pytest.fixture(autouse=True)
def _patch_panel_dependencies(monkeypatch):
    monkeypatch.setattr(
        watcher_welcome.thread_membership,
        "ensure_thread_membership",
        AsyncMock(return_value=(True, None)),
    )
    monkeypatch.setattr(
        watcher_welcome.panels, "find_panel_message", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        watcher_welcome.panels, "OpenQuestionsPanelView", lambda: SimpleNamespace()
    )
    monkeypatch.setattr(watcher_welcome.logs, "question_stats", lambda flow: (1, "v1"))

    def _fake_resolution(thread):
        name = (getattr(thread, "name", "") or "").strip()
        if name and name[0].upper() in {"R", "M", "L"}:
            return SimpleNamespace(flow=f"promo.{name[0].lower()}", error=None)
        return SimpleNamespace(flow="welcome", error=None)

    monkeypatch.setattr(
        watcher_welcome.welcome_flow, "resolve_onboarding_flow", _fake_resolution
    )


def test_welcome_trigger_creates_session(monkeypatch):
    saved: list[dict] = []

    async def _record(**payload):
        payload.setdefault("step_index", 0)
        payload.setdefault("completed", False)
        payload.setdefault("answers", {})
        saved.append(payload)
        return True

    monkeypatch.setattr(watcher_welcome.onboarding_sessions, "aupsert_session", _record)

    thread = DummyThread("W0603-smurf")
    trigger_message = SimpleNamespace(
        mentions=[SimpleNamespace(id=12345)],
        content="🔥 Welcome to C1C <@12345>",
    )

    outcome = asyncio.run(
        watcher_welcome.post_open_questions_panel(
            SimpleNamespace(user=SimpleNamespace(id=1)),
            thread,
            actor=SimpleNamespace(id=999),
            flow="welcome",
            trigger_message=trigger_message,
        )
    )

    assert outcome.panel_message_id == 9999
    assert saved
    payload = saved[0]
    assert payload["thread_name"] == "W0603-smurf"
    assert payload["thread_id"] == "4242"
    assert payload["user_id"] == "12345"
    assert payload["panel_message_id"] == "9999"
    assert payload["step_index"] == 0
    assert payload["completed"] is False
    assert payload["answers"] == {}


def test_promo_trigger_creates_session(monkeypatch):
    saved: list[dict] = []

    async def _record(**payload):
        payload.setdefault("step_index", 0)
        payload.setdefault("completed", False)
        payload.setdefault("answers", {})
        saved.append(payload)
        return True

    monkeypatch.setattr(watcher_welcome.onboarding_sessions, "aupsert_session", _record)

    thread_id = 1523691234567890123
    user_id = 1523691234567890456
    panel_message_id = 1523691234567890789
    thread = DummyThread(
        "R1234-smurf", thread_id=thread_id, panel_message_id=panel_message_id
    )
    trigger_message = SimpleNamespace(
        mentions=[SimpleNamespace(id=user_id)],
        content=f"✅ Promo ticket <@{user_id}>",
    )

    outcome = asyncio.run(
        watcher_welcome.post_open_questions_panel(
            SimpleNamespace(user=SimpleNamespace(id=1)),
            thread,
            actor=SimpleNamespace(id=999),
            flow="promo.r",
            trigger_message=trigger_message,
        )
    )

    assert outcome.panel_message_id == panel_message_id
    assert saved
    payload = saved[0]
    assert payload["thread_name"] == "R1234-smurf"
    assert payload["thread_id"] == str(thread_id)
    assert payload["user_id"] == str(user_id)
    assert payload["panel_message_id"] == str(panel_message_id)
    assert payload["step_index"] == 0
    assert payload["completed"] is False
    assert payload["answers"] == {}


def test_missing_subject_user_logs_warning(monkeypatch, caplog):
    saved: list[dict] = []

    async def _record(**payload):
        saved.append(payload)

    monkeypatch.setattr(watcher_welcome.onboarding_sessions, "aupsert_session", _record)

    thread = DummyThread("W1234-smurf")
    trigger_message = SimpleNamespace(mentions=[], content="🔥 Welcome to C1C")

    with caplog.at_level(logging.WARNING):
        outcome = asyncio.run(
            watcher_welcome.post_open_questions_panel(
                SimpleNamespace(user=SimpleNamespace(id=1)),
                thread,
                actor=SimpleNamespace(id=999),
                flow="welcome",
                trigger_message=trigger_message,
            )
        )

    assert outcome.panel_message_id == 9999
    assert saved == []
    assert any(
        "onboarding_session_save_skipped" in record.getMessage()
        and "no_subject_user" in record.getMessage()
        for record in caplog.records
    )


def test_explicit_subject_user_id_used_when_present(monkeypatch):
    saved: list[dict] = []

    async def _record(**payload):
        saved.append(payload)
        return True

    monkeypatch.setattr(watcher_welcome.onboarding_sessions, "aupsert_session", _record)

    thread = DummyThread("W0603-smurf")
    trigger_message = SimpleNamespace(
        mentions=[SimpleNamespace(id=12345)],
        content="🔥 Welcome to C1C <@12345>",
    )

    outcome = asyncio.run(
        watcher_welcome.post_open_questions_panel(
            SimpleNamespace(user=SimpleNamespace(id=1)),
            thread,
            actor=SimpleNamespace(id=999),
            flow="welcome",
            trigger_message=trigger_message,
            subject_user_id=77777,
        )
    )

    assert outcome.panel_message_id == 9999
    assert saved
    payload = saved[0]
    assert payload["user_id"] == "77777"


def test_welcome_session_saves_subject_not_actor(monkeypatch):
    saved: list[dict] = []

    async def _record(**payload):
        saved.append(payload)
        return True

    monkeypatch.setattr(watcher_welcome.onboarding_sessions, "aupsert_session", _record)
    monkeypatch.setattr(
        watcher_welcome, "locate_welcome_message", AsyncMock(return_value=None)
    )

    thread = DummyThread("W0603-smurf")
    thread.owner_id = 44444

    outcome = asyncio.run(
        watcher_welcome.post_open_questions_panel(
            SimpleNamespace(user=SimpleNamespace(id=1)),
            thread,
            actor=SimpleNamespace(id=99999),
            flow="welcome",
            trigger_message=SimpleNamespace(mentions=[], content="no subject"),
        )
    )

    assert outcome.panel_message_id == 9999
    assert saved
    payload = saved[0]
    assert payload["user_id"] == "44444"
    assert payload["thread_id"] == str(thread.id)
