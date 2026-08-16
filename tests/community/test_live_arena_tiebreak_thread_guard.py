from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
import pytest

from modules.community.live_arena import captains_table_control_center as control
from modules.community.live_arena import captains_table_runtime_repair as runtime
from modules.community.live_arena import tiebreak_thread_guard as guard


EXPECTED_NAME = "Qualification Tiebreak • smurf vs Glove"


class _Template:
    def embed(self, **_values):
        return discord.Embed(title="Qualification tiebreak", description="body")


class _Thread:
    def __init__(self, thread_id: int, *, owner_id: int = 42, parent_id: int = 123):
        self.id = thread_id
        self.owner_id = owner_id
        self.parent_id = parent_id
        self.name = EXPECTED_NAME
        self.deleted = False

    async def delete(self, **_kwargs):
        self.deleted = True


class _Bot:
    def __init__(self, threads=None):
        self.user = SimpleNamespace(id=42)
        self._threads = {thread.id: thread for thread in (threads or [])}

    def get_channel(self, channel_id):
        return self._threads.get(int(channel_id))

    async def fetch_channel(self, channel_id):
        return self._threads[int(channel_id)]


class _Forum:
    id = 123
    guild = None

    def __init__(self, bot: _Bot, threads=None):
        self.bot = bot
        self.threads = list(threads or [])
        self.created = 0
        self.next_id = 1000

    async def create_thread(self, **_kwargs):
        self.created += 1
        await asyncio.sleep(0)
        thread = _Thread(self.next_id)
        self.next_id += 1
        self.threads.append(thread)
        self.bot._threads[thread.id] = thread
        return SimpleNamespace(thread=thread)


def _state(*, thread_id=""):
    match = {
        "tournament_id": "LA-TEST",
        "round_id": "LA-TEST-TB",
        "match_id": "LA-TEST-TB-M01",
        "match_number": "1",
        "player_a_discord_user_id": "1",
        "player_a_display_name": "smurf",
        "player_b_discord_user_id": "2",
        "player_b_display_name": "Glove",
        "status": "published",
        "thread_id": thread_id,
        "final_winner_discord_user_id": "",
    }
    return control.ControlState(
        "LA-TEST",
        [
            {
                "tournament_id": "LA-TEST",
                "round_id": "LA-TEST-Q3",
                "round_stage": "qualification",
                "round_number": "3",
                "status": "closed",
            },
            {
                "tournament_id": "LA-TEST",
                "round_id": "LA-TEST-TB",
                "round_stage": "qualification_tiebreak",
                "round_number": "4",
                "status": "active",
            },
        ],
        [match],
        [],
        [["1", "2"]],
        [match],
        False,
    )


def _service():
    return SimpleNamespace(
        sheet_id="sheet",
        repository=SimpleNamespace(
            config={
                "MATCHES_TAB": "MATCHES",
                "MATCH_FORUM_CHANNEL_ID": "123",
            }
        ),
    )


async def _install_fakes(monkeypatch, forum, writes, controls, *, fresh_match=None):
    from modules.community.live_arena import qualification_panel

    async def resolve(_bot, channel_id):
        assert channel_id == 123
        return forum

    async def persist(_service, state, match, thread_id):
        writes.append(thread_id)
        match["thread_id"] = str(thread_id)
        for row in state.matches:
            if row.get("match_id") == match.get("match_id"):
                row["thread_id"] = str(thread_id)
        for row in state.tiebreak_matches:
            if row.get("match_id") == match.get("match_id"):
                row["thread_id"] = str(thread_id)

    async def ensure_controls(_manager, thread_id):
        controls.append(str(thread_id))

    async def fresh(_service, _match_id):
        if fresh_match is not None:
            return dict(fresh_match)
        state_match = getattr(forum, "state_match", None)
        if state_match is not None:
            return dict(state_match)
        raise AssertionError("test must provide current match state")

    monkeypatch.setattr(qualification_panel, "_resolve_channel", resolve)
    monkeypatch.setattr(runtime, "_persist_thread_id_without_reread", persist)
    monkeypatch.setattr(runtime, "_ensure_result_controls", ensure_controls)
    monkeypatch.setattr(guard, "_fresh_match", fresh)


@pytest.mark.asyncio
async def test_eight_existing_bot_threads_converge_to_oldest_and_delete_seven(monkeypatch):
    threads = [_Thread(thread_id) for thread_id in range(100, 108)]
    bot = _Bot(threads)
    forum = _Forum(bot, threads)
    state = _state(thread_id="107")
    forum.state_match = state.tiebreak_matches[0]
    writes, controls = [], []
    await _install_fakes(monkeypatch, forum, writes, controls)

    manager = SimpleNamespace(bot=bot, sheet_id="sheet")
    await guard._publish_tiebreak_threads(
        manager,
        _service(),
        state,
        {"qualification_tiebreak_thread": _Template()},
    )

    assert forum.created == 0
    assert writes == ["100"]
    assert state.tiebreak_matches[0]["thread_id"] == "100"
    assert controls == ["100"]
    assert threads[0].deleted is False
    assert sum(thread.deleted for thread in threads[1:]) == 7


@pytest.mark.asyncio
async def test_same_named_user_thread_is_never_adopted_or_deleted(monkeypatch):
    user_thread = _Thread(50, owner_id=777)
    canonical = _Thread(100)
    duplicate = _Thread(101)
    threads = [user_thread, canonical, duplicate]
    bot = _Bot(threads)
    forum = _Forum(bot, threads)
    state = _state(thread_id="101")
    forum.state_match = state.tiebreak_matches[0]
    writes, controls = [], []
    await _install_fakes(monkeypatch, forum, writes, controls)

    manager = SimpleNamespace(bot=bot, sheet_id="sheet")
    await guard._publish_tiebreak_threads(
        manager,
        _service(),
        state,
        {"qualification_tiebreak_thread": _Template()},
    )

    assert writes == ["100"]
    assert user_thread.deleted is False
    assert canonical.deleted is False
    assert duplicate.deleted is True


@pytest.mark.asyncio
async def test_simultaneous_in_process_reconciliation_creates_only_one_thread(monkeypatch):
    bot = _Bot()
    forum = _Forum(bot)
    state = _state()
    forum.state_match = state.tiebreak_matches[0]
    writes, controls = [], []
    await _install_fakes(monkeypatch, forum, writes, controls)

    manager = SimpleNamespace(bot=bot, sheet_id="sheet")
    templates = {"qualification_tiebreak_thread": _Template()}

    await asyncio.gather(
        guard._publish_tiebreak_threads(manager, _service(), state, templates),
        guard._publish_tiebreak_threads(manager, _service(), state, templates),
    )

    assert forum.created == 1
    assert writes == ["1000"]
    assert state.tiebreak_matches[0]["thread_id"] == "1000"
    assert controls == ["1000", "1000"]


@pytest.mark.asyncio
async def test_stale_open_state_cannot_recreate_thread_after_match_finalizes(monkeypatch):
    """Exact production race: old reconciliation resumes after organizer confirmation."""
    bot = _Bot()
    forum = _Forum(bot)
    state = _state(thread_id="")
    writes, controls = [], []
    finalized = dict(state.tiebreak_matches[0])
    finalized.update(
        status="finalized",
        final_score_a="1",
        final_score_b="2",
        final_winner_discord_user_id="2",
        finalized_by_discord_user_id="1",
        finalized_at_utc="2026-08-16T18:48:55Z",
    )
    await _install_fakes(
        monkeypatch,
        forum,
        writes,
        controls,
        fresh_match=finalized,
    )

    manager = SimpleNamespace(bot=bot, sheet_id="sheet")
    await guard._publish_tiebreak_threads(
        manager,
        _service(),
        state,
        {"qualification_tiebreak_thread": _Template()},
    )

    assert forum.created == 0
    assert writes == []
    assert controls == []
    assert state.tiebreak_matches[0]["status"] == "finalized"
    assert state.tiebreak_matches[0]["final_winner_discord_user_id"] == "2"


@pytest.mark.asyncio
async def test_finalized_tiebreak_cleans_duplicate_threads_but_never_restores_controls(monkeypatch):
    canonical = _Thread(100)
    resurrected = _Thread(101)
    threads = [canonical, resurrected]
    bot = _Bot(threads)
    forum = _Forum(bot, threads)
    state = _state(thread_id="101")
    finalized = dict(state.tiebreak_matches[0])
    finalized.update(
        status="finalized",
        final_score_a="1",
        final_score_b="2",
        final_winner_discord_user_id="2",
    )
    writes, controls = [], []
    await _install_fakes(
        monkeypatch,
        forum,
        writes,
        controls,
        fresh_match=finalized,
    )

    manager = SimpleNamespace(bot=bot, sheet_id="sheet")
    await guard._publish_tiebreak_threads(
        manager,
        _service(),
        state,
        {"qualification_tiebreak_thread": _Template()},
    )

    assert forum.created == 0
    assert writes == ["100"]
    assert canonical.deleted is False
    assert resurrected.deleted is True
    assert controls == []
