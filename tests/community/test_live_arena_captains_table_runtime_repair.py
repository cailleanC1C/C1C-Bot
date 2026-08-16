from __future__ import annotations

from types import SimpleNamespace

import discord
import pytest

from modules.community.live_arena import captains_table_control_center as control
from modules.community.live_arena import captains_table_runtime_repair as repair


class _Template:
    def __init__(self, title="Template"):
        self.title = title

    def embed(self, **_values):
        return discord.Embed(title=self.title, description="body")

    def render(self, **values):
        rendered = ", ".join(f"{key}={value}" for key, value in values.items())
        return self.title, rendered or "body"


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


@pytest.mark.asyncio
async def test_thread_id_persistence_uses_known_row_without_rereading_matches(monkeypatch):
    state = _state()
    match = state.tiebreak_matches[0]
    writes = []

    class Repo:
        config = {"MATCHES_TAB": "MATCHES"}

        async def matches(self):
            raise AssertionError("MATCHES must not be re-read after the tiebreak write")

    class Worksheet:
        def update_cell(self, row, column, value):
            writes.append((row, column, value))

    async def fake_worksheet(sheet_id, tab):
        assert sheet_id == "sheet"
        assert tab == "MATCHES"
        return Worksheet()

    async def fake_call(func, *args, **_kwargs):
        return func(*args)

    monkeypatch.setattr(repair, "aget_worksheet", fake_worksheet)
    monkeypatch.setattr(repair, "acall_with_backoff", fake_call)

    service = SimpleNamespace(
        sheet_id="sheet",
        repository=Repo(),
    )
    await repair._persist_thread_id_without_reread(service, state, match, "999")

    assert writes == [(2, 15, "999")]
    assert match["thread_id"] == "999"
    assert state.matches[0]["thread_id"] == "999"


@pytest.mark.asyncio
async def test_existing_blank_tiebreak_row_creates_one_thread_and_becomes_restart_safe(monkeypatch):
    state = _state()
    created_count = 0
    writes = []

    class Repo:
        config = {
            "MATCHES_TAB": "MATCHES",
            "MATCH_FORUM_CHANNEL_ID": "123",
        }

        async def matches(self):
            raise AssertionError("publication must not re-read MATCHES")

    class Worksheet:
        def update_cell(self, row, column, value):
            writes.append((row, column, value))

    class Thread:
        id = 999
        owner_id = 42
        parent_id = 123
        name = "Qualification Tiebreak • smurf vs Glove"

        async def delete(self, **_kwargs):
            raise AssertionError("successful tracked thread must not be deleted")

    thread = Thread()

    class Bot:
        user = SimpleNamespace(id=42)

        def get_channel(self, channel_id):
            return thread if int(channel_id) == 999 else None

        async def fetch_channel(self, channel_id):
            assert int(channel_id) == 999
            return thread

    bot = Bot()

    class Forum:
        id = 123
        guild = None

        def __init__(self):
            self.threads = []

        async def create_thread(self, **_kwargs):
            nonlocal created_count
            created_count += 1
            self.threads.append(thread)
            return SimpleNamespace(thread=thread)

    forum = Forum()

    async def fake_resolve(_bot, channel_id):
        assert channel_id == 123
        return forum

    async def fake_worksheet(_sheet_id, _tab):
        return Worksheet()

    async def fake_call(func, *args, **_kwargs):
        return func(*args)

    async def no_controls(_manager, _thread_id):
        return None

    from modules.community.live_arena import qualification_panel

    monkeypatch.setattr(qualification_panel, "_resolve_channel", fake_resolve)
    monkeypatch.setattr(repair, "aget_worksheet", fake_worksheet)
    monkeypatch.setattr(repair, "acall_with_backoff", fake_call)
    monkeypatch.setattr(repair, "_ensure_result_controls", no_controls)

    service = SimpleNamespace(sheet_id="sheet", repository=Repo())
    manager = SimpleNamespace(bot=bot, sheet_id="sheet")
    templates = {"qualification_tiebreak_thread": _Template("Qualification tiebreak")}

    await repair._publish_tiebreak_threads(manager, service, state, templates)
    await repair._publish_tiebreak_threads(manager, service, state, templates)

    assert created_count == 1
    assert writes == [(2, 15, "999")]
    assert state.tiebreak_matches[0]["thread_id"] == "999"


@pytest.mark.asyncio
async def test_control_center_edits_known_partial_message_without_fetching_it(monkeypatch):
    state = _state(thread_id="999")
    edits = []

    class Partial:
        async def edit(self, **kwargs):
            edits.append(kwargs)

    class Channel:
        guild = SimpleNamespace()

        def get_partial_message(self, message_id):
            assert message_id == 456
            return Partial()

        async def fetch_message(self, _message_id):
            raise AssertionError("Captain's Table render must not fetch its known message")

    channel = Channel()

    class Bot:
        def get_channel(self, channel_id):
            assert channel_id == 123
            return channel

    tournament = SimpleNamespace(
        tournament_name="Trial Cup",
        status="signup_closed",
        max_participants=16,
    )

    class Manager:
        sheet_id = "sheet"
        bot = Bot()

        async def data(self, _guild):
            return (
                {
                    "ORGANIZER_PANEL_MESSAGE_ID": "456",
                    "ORGANIZER_CHANNEL_ID": "123",
                    "MESSAGES_TAB": "MESSAGES",
                },
                tournament,
                [],
                {"confirmed": 8},
                {},
            )

        def view(self, _status):
            return discord.ui.View(timeout=None)

    async def config(_sheet_id):
        return (
            {
                "ORGANIZER_PANEL_MESSAGE_ID": "456",
                "ORGANIZER_CHANNEL_ID": "123",
                "MESSAGES_TAB": "MESSAGES",
            },
            None,
        )

    async def templates(_sheet_id):
        return {
            "organizer_control_stage": _Template("Current tournament state"),
            "organizer_control_attention": _Template("Attention needed"),
            "organizer_control_progress": _Template("Tournament progress"),
            "organizer_control_standings": _Template("Current qualification order"),
        }

    async def base_messages(_sheet_id, _tab, _keys):
        return {"organizer_panel": _Template("Captain's Table")}

    monkeypatch.setattr(repair, "load_pr5_config", config)
    monkeypatch.setattr(control, "_load_templates", templates)
    monkeypatch.setattr(repair, "load_messages", base_messages)

    await repair._render_control_center(Manager(), state)

    assert len(edits) == 1
    assert edits[0]["embed"].title == "Captain's Table"
    assert edits[0]["view"] is not None
