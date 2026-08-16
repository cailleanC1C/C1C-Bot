from __future__ import annotations

import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from modules.community.live_arena import round_close_milestone_ux as milestone


def run(awaitable):
    return asyncio.run(awaitable)


def test_round_close_reconciliation_crosses_fresh_read_scopes(monkeypatch):
    """Reproduce the live bug: cached pre-close rows must not survive the write."""
    durable = {"round_status": "ready_to_close", "next_preview": False}
    scope_snapshots = []
    active = []

    @contextmanager
    def fake_read_scope():
        snapshot = dict(durable)
        scope_snapshots.append(snapshot)
        active.append(snapshot)
        try:
            yield SimpleNamespace()
        finally:
            active.pop()

    monkeypatch.setattr(milestone, "sheet_read_scope", fake_read_scope)

    events = []

    class FakeService:
        async def close_round(self, actor_id, round_id):
            # A read in this scope still reflects ready_to_close, exactly like the
            # production cache that caused Q2 to stall. The durable write changes
            # only what the *next* scope is allowed to observe.
            events.append(("close", active[-1]["round_status"]))
            durable["round_status"] = "closed"
            return {
                "round_id": round_id,
                "round_name": "Qualification Round 2",
                "round_stage": "qualification",
                "round_number": "2",
                "status": "closed",
            }

    async def stage_sync(_manager):
        events.append(("stage", active[-1]["round_status"]))
        assert active[-1]["round_status"] == "closed"
        durable["next_preview"] = True
        return []

    async def ensure_next(_manager, _closed):
        events.append(("ensure", active[-1]["next_preview"]))
        assert active[-1]["next_preview"] is True

    async def panel_sync():
        events.append(("panel", active[-1]["next_preview"]))
        assert active[-1]["next_preview"] is True
        return SimpleNamespace(ok=True)

    monkeypatch.setattr(milestone, "_ensure_next_stage_state", ensure_next)
    runtime_hooks = SimpleNamespace(
        _best_effort_competition_sync=stage_sync,
        log=SimpleNamespace(exception=lambda *args, **kwargs: None),
    )
    manager = SimpleNamespace(sync=panel_sync)

    closed, warnings = run(
        milestone._close_and_reconcile(
            FakeService(),
            manager,
            runtime_hooks,
            actor_id="42",
            round_id="T-Q2",
        )
    )

    assert closed["status"] == "closed"
    assert warnings == []
    assert events == [
        ("close", "ready_to_close"),
        ("stage", "closed"),
        ("ensure", True),
        ("panel", True),
    ]
    assert len(scope_snapshots) == 4
    assert scope_snapshots[0] is not scope_snapshots[1]
    assert scope_snapshots[1] is not scope_snapshots[2]
    assert scope_snapshots[2] is not scope_snapshots[3]


def test_q2_close_safety_net_creates_q3_preview(monkeypatch):
    from modules.community.live_arena import swiss, swiss_runtime

    generated = SimpleNamespace(
        round_row={"round_number": "3", "round_stage": "qualification", "status": "preview"},
        matches=(),
    )

    class FakeSwissService:
        def __init__(self, sheet_id):
            assert sheet_id == "sheet"
            self.generated = AsyncMock(return_value=generated)

        async def initialize(self):
            return None

        async def snapshot(self, number):
            assert number == 3
            return SimpleNamespace(round_row=None, matches=())

        async def generate_preview(self, actor_id, number):
            assert actor_id == "system"
            assert number == 3
            return await self.generated(actor_id, number)

    service_holder = {}

    def service_factory(sheet_id):
        service = FakeSwissService(sheet_id)
        service_holder["service"] = service
        return service

    sync_preview = AsyncMock()
    monkeypatch.setattr(swiss, "SwissQualificationService", service_factory)
    monkeypatch.setattr(swiss_runtime, "_sync_preview_message", sync_preview)

    manager = SimpleNamespace(
        sheet_id="sheet",
        _captains_table_stage_reconciling=False,
    )
    run(
        milestone._ensure_next_stage_state(
            manager,
            {"round_stage": "qualification", "round_number": "2"},
        )
    )

    service_holder["service"].generated.assert_awaited_once_with("system", 3)
    sync_preview.assert_awaited_once()
    assert manager._captains_table_stage_reconciling is False


def test_q3_close_keeps_top8_as_organizer_decision(monkeypatch):
    """Q3 must expose Lock Top 8; it must never freeze seeds behind the organizer's back."""
    from modules.community.live_arena import swiss, knockout

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Q3 closure must not auto-create or freeze knockout state")

    monkeypatch.setattr(swiss, "SwissQualificationService", unexpected)
    monkeypatch.setattr(knockout, "KnockoutService", unexpected)

    manager = SimpleNamespace(sheet_id="sheet")
    run(
        milestone._ensure_next_stage_state(
            manager,
            {"round_stage": "qualification", "round_number": "3"},
        )
    )


@pytest.mark.parametrize(
    ("closed_stage", "next_stage"),
    [
        ("quarterfinal", "semifinal"),
        ("semifinal", "final"),
    ],
)
def test_knockout_close_safety_net_creates_next_preview(
    monkeypatch, closed_stage, next_stage
):
    from modules.community.live_arena import knockout, knockout_runtime

    generated = SimpleNamespace(
        round_row={"round_stage": next_stage, "status": "preview"},
        matches=(),
    )
    generated_calls = []

    class FakeKnockoutService:
        def __init__(self, sheet_id):
            assert sheet_id == "sheet"

        async def initialize(self):
            return None

        async def snapshot(self, stage):
            assert stage == next_stage
            return SimpleNamespace(round_row=None, matches=())

        async def generate_next_preview(self, actor_id, stage):
            generated_calls.append((actor_id, stage))
            return generated

    sync_preview = AsyncMock()
    monkeypatch.setattr(knockout, "KnockoutService", FakeKnockoutService)
    monkeypatch.setattr(knockout_runtime, "_sync_preview_message", sync_preview)

    manager = SimpleNamespace(
        sheet_id="sheet",
        _captains_table_stage_reconciling=False,
    )
    run(
        milestone._ensure_next_stage_state(
            manager,
            {"round_stage": closed_stage, "round_number": "1"},
        )
    )

    assert generated_calls == [("system", next_stage)]
    sync_preview.assert_awaited_once()
    assert manager._captains_table_stage_reconciling is False


def test_final_close_has_no_preview_and_leaves_finish_tournament_for_panel(monkeypatch):
    from modules.community.live_arena import knockout

    def unexpected(*_args, **_kwargs):
        raise AssertionError("Final closure must transition to Finish Tournament, not another preview")

    monkeypatch.setattr(knockout, "KnockoutService", unexpected)

    manager = SimpleNamespace(sheet_id="sheet")
    run(
        milestone._ensure_next_stage_state(
            manager,
            {"round_stage": "final", "round_number": "1"},
        )
    )
