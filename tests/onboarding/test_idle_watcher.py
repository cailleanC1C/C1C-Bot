import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from modules.onboarding import idle_watcher
from modules.onboarding import welcome_flow


class _DummyThread:
    def __init__(self, thread_id: int, name: str = "W1234-user") -> None:
        self.id = thread_id
        self.name = name
        self.guild = None
        self.sent: list[str] = []
        self.archived = False
        self.locked = False

    async def send(self, content: str) -> None:
        self.sent.append(content)

    async def edit(self, *, name=None, archived=None, locked=None):
        if name is not None:
            self.name = name
        if archived is not None:
            self.archived = archived
        if locked is not None:
            self.locked = locked

    def history(self, *, limit=50):
        async def _iter():
            if False:
                yield None
        return _iter()


class _DummyBot:
    def __init__(self, threads: dict[int, _DummyThread]):
        self._threads = threads

    async def wait_until_ready(self):
        return None

    def get_channel(self, thread_id: int):
        return self._threads.get(thread_id)

    async def fetch_channel(self, thread_id: int):
        return self._threads.get(thread_id)

    def get_cog(self, _name: str):
        return None


def _fixed_now() -> datetime:
    return datetime(2025, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _freeze_now(monkeypatch):
    monkeypatch.setattr(idle_watcher, "_utc_now", _fixed_now)


def _row(age_hours: float, **overrides):
    base = {
        "user_id": 111,
        "thread_id": 999,
        "panel_message_id": None,
        "step_index": 0,
        "answers": {},
        "completed": False,
        "completed_at": None,
        "first_reminder_at": "",
        "warning_sent_at": "",
        "auto_closed_at": "",
        "updated_at": (_fixed_now() - timedelta(hours=age_hours)).isoformat(),
    }
    base.update(overrides)
    return base


def test_idle_watcher_posts_first_reminder(monkeypatch):
    thread = _DummyThread(999)
    bot = _DummyBot({999: thread})
    saves: list[dict] = []

    async def _resolve(_bot, _tid):
        return thread

    monkeypatch.setattr(idle_watcher, "_resolve_thread", _resolve)
    monkeypatch.setattr(
        idle_watcher.onboarding_sessions,
        "aload_all",
        AsyncMock(return_value=[_row(3.1)]),
    )
    monkeypatch.setattr(
        idle_watcher.onboarding_sessions,
        "aupdate_existing",
        AsyncMock(side_effect=lambda thread_id, payload: saves.append(payload)),
    )
    monkeypatch.setattr(welcome_flow, "resolve_onboarding_flow", lambda t: welcome_flow.FlowResolution("welcome"))
    monkeypatch.setattr(
        idle_watcher.reservation_jobs,
        "release_reservations_for_thread",
        AsyncMock(),
    )
    monkeypatch.setattr(idle_watcher, "get_recruitment_coordinator_role_ids", lambda: set())
    monkeypatch.setattr(
        idle_watcher,
        "_resolve_thread_parts",
        lambda flow, name: (
            type("Parts", (), {"ticket_code": "W1234", "username": "user"})(),
            lambda code, user, tag: f"Closed-{code}-{user}-{tag}",
        ),
    )

    asyncio.run(idle_watcher.run_idle_scan(bot, now=_fixed_now()))

    assert thread.sent
    assert "open questions" in thread.sent[0].lower()
    assert saves and saves[0].get("first_reminder_at")


def test_idle_watcher_skips_when_completed_at_present(monkeypatch):
    thread = _DummyThread(999)
    bot = _DummyBot({999: thread})
    saves: list[dict] = []

    async def _resolve(_bot, _tid):
        return thread

    monkeypatch.setattr(idle_watcher, "_resolve_thread", _resolve)
    monkeypatch.setattr(
        idle_watcher.onboarding_sessions,
        "aload_all",
        AsyncMock(
            return_value=[
                _row(
                    8.0,
                    completed=False,
                    completed_at="2025-01-01T10:00:00+00:00",
                )
            ]
        ),
    )
    monkeypatch.setattr(
        idle_watcher.onboarding_sessions,
        "aupdate_existing",
        AsyncMock(side_effect=lambda thread_id, payload: saves.append(payload)),
    )
    monkeypatch.setattr(welcome_flow, "resolve_onboarding_flow", lambda t: welcome_flow.FlowResolution("welcome"))
    monkeypatch.setattr(
        idle_watcher.reservation_jobs,
        "release_reservations_for_thread",
        AsyncMock(),
    )

    asyncio.run(idle_watcher.run_idle_scan(bot, now=_fixed_now()))

    assert thread.sent == []
    assert saves == []


def test_idle_watcher_posts_warning(monkeypatch):
    thread = _DummyThread(999)
    bot = _DummyBot({999: thread})
    saves: list[dict] = []

    async def _resolve(_bot, _tid):
        return thread

    monkeypatch.setattr(idle_watcher, "_resolve_thread", _resolve)
    monkeypatch.setattr(
        idle_watcher.onboarding_sessions,
        "aload_all",
        AsyncMock(return_value=[_row(24.5)]),
    )
    monkeypatch.setattr(
        idle_watcher.onboarding_sessions,
        "aupdate_existing",
        AsyncMock(side_effect=lambda thread_id, payload: saves.append(payload)),
    )
    monkeypatch.setattr(welcome_flow, "resolve_onboarding_flow", lambda t: welcome_flow.FlowResolution("welcome"))
    monkeypatch.setattr(
        idle_watcher.reservation_jobs,
        "release_reservations_for_thread",
        AsyncMock(),
    )
    monkeypatch.setattr(idle_watcher, "get_recruitment_coordinator_role_ids", lambda: {42})
    monkeypatch.setattr(
        idle_watcher,
        "_resolve_thread_parts",
        lambda flow, name: (
            type("Parts", (), {"ticket_code": "W1234", "username": "user"})(),
            lambda code, user, tag: f"Closed-{code}-{user}-{tag}",
        ),
    )

    asyncio.run(idle_watcher.run_idle_scan(bot, now=_fixed_now()))

    assert thread.sent
    assert "<@&42>" in thread.sent[0]
    assert saves and saves[0].get("warning_sent_at")


def test_idle_watcher_autoclose_welcome(monkeypatch):
    thread = _DummyThread(999, name="W1234-user")
    bot = _DummyBot({999: thread})
    saves: list[dict] = []
    releases: list[int] = []

    async def _resolve(_bot, _tid):
        return thread

    monkeypatch.setattr(idle_watcher, "_resolve_thread", _resolve)
    monkeypatch.setattr(
        idle_watcher.onboarding_sessions,
        "aload_all",
        AsyncMock(return_value=[_row(36.5)]),
    )
    monkeypatch.setattr(
        idle_watcher.onboarding_sessions,
        "aupdate_existing",
        AsyncMock(side_effect=lambda thread_id, payload: saves.append(payload)),
    )
    monkeypatch.setattr(welcome_flow, "resolve_onboarding_flow", lambda t: welcome_flow.FlowResolution("welcome"))
    monkeypatch.setattr(
        idle_watcher.reservation_jobs,
        "release_reservations_for_thread",
        AsyncMock(side_effect=lambda thread_id, **_: releases.append(thread_id)),
    )
    monkeypatch.setattr(idle_watcher, "get_recruitment_coordinator_role_ids", lambda: {7})
    monkeypatch.setattr(
        idle_watcher,
        "_resolve_thread_parts",
        lambda flow, name: (
            type("Parts", (), {"ticket_code": "W1234", "username": "user"})(),
            lambda code, user, tag: f"Closed-{code}-{user}-{tag}",
        ),
    )

    asyncio.run(idle_watcher.run_idle_scan(bot, now=_fixed_now()))

    assert thread.archived and thread.locked
    assert thread.name.startswith("Closed-")
    assert thread.sent and "remove the user" in thread.sent[-1]
    assert saves and saves[0].get("auto_closed_at")
    assert releases == [999]


def test_idle_watcher_autoclose_promo(monkeypatch):
    thread = _DummyThread(888, name="R1234-user")
    bot = _DummyBot({888: thread})
    saves: list[dict] = []

    async def _resolve(_bot, _tid):
        return thread

    monkeypatch.setattr(idle_watcher, "_resolve_thread", _resolve)
    monkeypatch.setattr(
        idle_watcher.onboarding_sessions,
        "aload_all",
        AsyncMock(return_value=[_row(36.5, thread_id=888)]),
    )
    monkeypatch.setattr(
        idle_watcher.onboarding_sessions,
        "aupdate_existing",
        AsyncMock(side_effect=lambda thread_id, payload: saves.append(payload)),
    )
    monkeypatch.setattr(welcome_flow, "resolve_onboarding_flow", lambda t: welcome_flow.FlowResolution("promo.r"))
    monkeypatch.setattr(
        idle_watcher.reservation_jobs,
        "release_reservations_for_thread",
        AsyncMock(),
    )
    monkeypatch.setattr(idle_watcher, "get_recruitment_coordinator_role_ids", lambda: {9})
    monkeypatch.setattr(
        idle_watcher,
        "_resolve_thread_parts",
        lambda flow, name: (
            type("Parts", (), {"ticket_code": "R1234", "username": "user"})(),
            lambda code, user, tag: f"Closed-{code}-{user}-{tag}",
        ),
    )

    asyncio.run(idle_watcher.run_idle_scan(bot, now=_fixed_now()))

    assert thread.sent and "promo ticket" in thread.sent[-1].lower()
    assert "remove the user" not in thread.sent[-1]
    assert saves and saves[0].get("auto_closed_at")


def test_idle_watcher_skips_closed_ticket(monkeypatch):
    thread = _DummyThread(999, name="Closed-W1234-user-NONE")
    bot = _DummyBot({999: thread})
    saves: list[dict] = []

    async def _resolve(_bot, _tid):
        return thread

    monkeypatch.setattr(idle_watcher, "_resolve_thread", _resolve)
    monkeypatch.setattr(
        idle_watcher.onboarding_sessions,
        "aload_all",
        AsyncMock(return_value=[_row(6.0)]),
    )
    monkeypatch.setattr(
        idle_watcher.onboarding_sessions,
        "aupdate_existing",
        AsyncMock(side_effect=lambda thread_id, payload: saves.append(payload)),
    )
    monkeypatch.setattr(welcome_flow, "resolve_onboarding_flow", lambda t: welcome_flow.FlowResolution("welcome"))
    monkeypatch.setattr(
        idle_watcher.reservation_jobs,
        "release_reservations_for_thread",
        AsyncMock(),
    )

    asyncio.run(idle_watcher.run_idle_scan(bot, now=_fixed_now()))

    assert thread.sent == []
    assert saves == []
