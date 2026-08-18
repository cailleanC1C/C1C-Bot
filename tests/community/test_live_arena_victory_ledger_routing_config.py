import asyncio
from types import SimpleNamespace

from modules.community.live_arena import victory_ledger_routing_config as routing


class FakeRepository:
    def __init__(self, config=None):
        self.config = dict(config or {})
        self.initialize_calls = 0

    async def initialize(self):
        self.initialize_calls += 1
        self.config = {"TOURNAMENT_DISCORD_RESOURCES_TAB": "RESOURCES"}


def test_workspace_routing_is_loaded_when_registration_config_lacks_ui_keys(monkeypatch):
    repo = FakeRepository({"TOURNAMENT_DISCORD_RESOURCES_TAB": "RESOURCES"})
    seen = []

    async def load(_sheet_id):
        return {
            "ROUND_OVERVIEW_CHANNEL_ID": "12345",
            "MESSAGES_TAB": "MESSAGES",
        }

    async def original(_bot, sheet_id, repository):
        seen.append((sheet_id, dict(repository.config)))
        return "workspace"

    workspace = SimpleNamespace(
        _WORKSPACE_CACHE={},
        LiveArenaRepository=lambda _sid: None,
    )
    monkeypatch.setattr(routing, "_load_workspace_routing", load)

    result = asyncio.run(
        routing._ensure_workspace_with_routing(
            original,
            workspace,
            object(),
            "sheet-123",
            repo,
        )
    )

    assert result == "workspace"
    assert repo.initialize_calls == 0
    assert repo.config["ROUND_OVERVIEW_CHANNEL_ID"] == "12345"
    assert repo.config["MESSAGES_TAB"] == "MESSAGES"
    assert seen == [("sheet-123", repo.config)]


def test_workspace_routing_initializes_empty_registration_repository(monkeypatch):
    repo = FakeRepository()

    async def load(_sheet_id):
        return {
            "ROUND_OVERVIEW_CHANNEL_ID": "12345",
            "MESSAGES_TAB": "MESSAGES",
        }

    async def original(_bot, _sheet_id, repository):
        assert repository is repo
        assert repository.config["ROUND_OVERVIEW_CHANNEL_ID"] == "12345"
        assert repository.config["MESSAGES_TAB"] == "MESSAGES"
        return "workspace"

    workspace = SimpleNamespace(
        _WORKSPACE_CACHE={},
        LiveArenaRepository=lambda _sid: None,
    )
    monkeypatch.setattr(routing, "_load_workspace_routing", load)

    assert (
        asyncio.run(
            routing._ensure_workspace_with_routing(
                original,
                workspace,
                object(),
                "sheet-123",
                repo,
            )
        )
        == "workspace"
    )
    assert repo.initialize_calls == 1


def test_cached_workspace_keeps_fast_path_without_config_read(monkeypatch):
    repo = FakeRepository()
    cached = object()
    load_calls = []

    async def load(_sheet_id):
        load_calls.append(True)
        raise AssertionError("cached workspace should not reload CONFIG")

    async def original(_bot, _sheet_id, _repository):
        return cached

    workspace = SimpleNamespace(
        _WORKSPACE_CACHE={"sheet-123": cached},
        LiveArenaRepository=lambda _sid: None,
    )
    monkeypatch.setattr(routing, "_load_workspace_routing", load)

    result = asyncio.run(
        routing._ensure_workspace_with_routing(
            original,
            workspace,
            object(),
            "sheet-123",
            repo,
        )
    )

    assert result is cached
    assert load_calls == []
