import asyncio
from types import SimpleNamespace

from modules.community.live_arena import victory_ledger_workspace as workspace


class FakeMessage:
    def __init__(self, message_id="100", embeds=None):
        self.id = int(message_id)
        self.embeds = list(embeds or [])
        self.deleted = False
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        if "embeds" in kwargs:
            self.embeds = list(kwargs["embeds"])

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self, channel_id="10"):
        self.id = int(channel_id)
        self.guild = SimpleNamespace(id=1)
        self.messages = {}
        self.sent = []

    async def fetch_message(self, message_id):
        return self.messages.get(int(message_id))

    async def send(self, **kwargs):
        message = FakeMessage(str(1000 + len(self.sent)), kwargs.get("embeds"))
        self.messages[message.id] = message
        self.sent.append((message, kwargs))
        return message


class FakeRepository:
    def __init__(self, resources=None):
        self.resources = dict(resources or {})
        self.upserts = []

    async def discord_resource(self, tournament_id, resource_type, resource_key="main"):
        return self.resources.get((tournament_id, resource_type, resource_key))

    async def upsert_discord_resource(self, **kwargs):
        self.upserts.append(kwargs)
        self.resources[
            (kwargs["tournament_id"], kwargs["resource_type"], kwargs["resource_key"])
        ] = dict(kwargs)


async def _noop_index(*_args, **_kwargs):
    return None


def test_closed_round_archives_once_and_retires_current(monkeypatch):
    parent = FakeChannel("10")
    current = FakeMessage("55")
    parent.messages[current.id] = current
    archive = FakeChannel("20")
    repo = FakeRepository(
        {
            (
                workspace._GLOBAL_RESOURCE_ID,
                workspace._CURRENT_RESOURCE_TYPE,
                "main",
            ): {
                "channel_id": "10",
                "message_id": "55",
                "state": "active",
                "notes": "T-QF",
                "created_at_utc": "old",
            }
        }
    )
    ws = SimpleNamespace(
        repository=repo,
        parent=parent,
        archive=archive,
        results=FakeChannel("30"),
        hall_of_fame=FakeChannel("40"),
        templates={},
    )

    async def ensure(*_args, **_kwargs):
        return ws

    monkeypatch.setattr(workspace, "ensure_workspace", ensure)
    monkeypatch.setattr(workspace, "refresh_index", _noop_index)

    service = SimpleNamespace(
        sheet_id="sheet",
        registration_repository=repo,
    )
    snapshot = SimpleNamespace(
        round_row={
            "tournament_id": "T",
            "round_id": "T-QF",
            "status": "closed",
            "overview_message_id": "55",
        }
    )

    asyncio.run(workspace.sync_round_overview(None, service, snapshot, ["embed"]))

    assert len(archive.sent) == 1
    assert current.deleted is True
    archive_resource = repo.resources[("T", workspace._ARCHIVE_RESOURCE_TYPE, "T-QF")]
    assert archive_resource["thread_id"] == "20"
    current_resource = repo.resources[
        (workspace._GLOBAL_RESOURCE_ID, workspace._CURRENT_RESOURCE_TYPE, "main")
    ]
    assert current_resource["state"] == "retired"

    # Reconciliation must not rewrite or duplicate an existing immutable snapshot.
    asyncio.run(workspace.sync_round_overview(None, service, snapshot, ["new embed"]))
    assert len(archive.sent) == 1


def test_active_round_owns_single_parent_message(monkeypatch):
    parent = FakeChannel("10")
    archive = FakeChannel("20")
    repo = FakeRepository()
    ws = SimpleNamespace(
        repository=repo,
        parent=parent,
        archive=archive,
        results=FakeChannel("30"),
        hall_of_fame=FakeChannel("40"),
        templates={},
    )

    async def ensure(*_args, **_kwargs):
        return ws

    monkeypatch.setattr(workspace, "ensure_workspace", ensure)
    monkeypatch.setattr(workspace, "refresh_index", _noop_index)

    recorded = []

    async def record(round_id, message_id):
        recorded.append((round_id, message_id))

    service = SimpleNamespace(
        sheet_id="sheet",
        registration_repository=repo,
        record_overview_message_id=record,
    )
    snapshot = SimpleNamespace(
        round_row={
            "tournament_id": "T",
            "round_id": "T-Q1",
            "status": "active",
            "overview_message_id": "",
        }
    )

    asyncio.run(workspace.sync_round_overview(None, service, snapshot, ["embed"]))

    assert len(parent.sent) == 1
    current = repo.resources[
        (workspace._GLOBAL_RESOURCE_ID, workspace._CURRENT_RESOURCE_TYPE, "main")
    ]
    assert current["state"] == "active"
    assert current["notes"] == "T-Q1"
    assert recorded == [("T-Q1", current["message_id"])]


def test_final_recap_is_routed_to_results_thread_and_config_restored(monkeypatch):
    repo = FakeRepository()
    repo.config = {"ROUND_OVERVIEW_CHANNEL_ID": "10"}
    results = FakeChannel("30")
    ws = SimpleNamespace(
        repository=repo,
        parent=FakeChannel("10"),
        archive=FakeChannel("20"),
        results=results,
        hall_of_fame=FakeChannel("40"),
        templates={},
    )

    async def ensure(*_args, **_kwargs):
        return ws

    monkeypatch.setattr(workspace, "ensure_workspace", ensure)
    monkeypatch.setattr(workspace, "refresh_index", _noop_index)

    seen = []

    async def original(_manager, service, _summary):
        seen.append(service.repository.config["ROUND_OVERVIEW_CHANNEL_ID"])

    monkeypatch.setattr(workspace, "_original_final_recap", original)
    manager = SimpleNamespace(bot=object(), sheet_id="sheet")
    service = SimpleNamespace(repository=repo, registration_repository=repo)

    asyncio.run(workspace._sync_final_recap(manager, service, {"tournament_id": "T"}))

    assert seen == ["30"]
    assert repo.config["ROUND_OVERVIEW_CHANNEL_ID"] == "10"


def test_index_copy_contract_is_mobile_navigation_shape():
    template = workspace.Template(
        "victory_ledger_index",
        "Victory Ledger",
        "{current_round}|{round_archive}|{tournament_results}|{hall_of_fame}",
        0,
    )
    title, description = template.render(
        current_round="current",
        round_archive="archive",
        tournament_results="results",
        hall_of_fame="hall",
    )
    assert title == "Victory Ledger"
    assert description == "current|archive|results|hall"
