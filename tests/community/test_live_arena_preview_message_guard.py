import asyncio
from types import SimpleNamespace

import discord
import pytest

from modules.community.live_arena import preview_message_guard as guard


class FakeMessage:
    def __init__(self, message_id, embed, author_id):
        self.id = message_id
        self.embeds = [embed]
        self.author = SimpleNamespace(id=author_id)
        self.deleted = False
        self.edit_count = 0

    async def edit(self, *, embed=None, **_kwargs):
        if embed is not None:
            self.embeds = [embed]
        self.edit_count += 1

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self, bot_id="42"):
        self.bot_id = bot_id
        self.messages = {}
        self.send_count = 0
        self.next_id = 1000

    async def send(self, *, embed=None, **_kwargs):
        self.send_count += 1
        message = FakeMessage(self.next_id, embed, self.bot_id)
        self.next_id += 1
        self.messages[message.id] = message
        return message

    async def fetch_message(self, message_id):
        message = self.messages[int(message_id)]
        if message.deleted:
            raise KeyError(message_id)
        return message

    def history(self, *, limit=75):
        async def iterate():
            visible = [
                message
                for message in self.messages.values()
                if not message.deleted
            ]
            for message in sorted(visible, key=lambda item: item.id, reverse=True)[:limit]:
                yield message

        return iterate()


class FakeRepository:
    def __init__(self, resource=None, *, stale_reads=False):
        self.resource = resource
        self.stale_reads = stale_reads
        self.upserts = []

    async def discord_resource(self, _tid, _resource_type, _resource_key):
        if self.stale_reads:
            return None
        return dict(self.resource) if self.resource else None

    async def upsert_discord_resource(self, **kwargs):
        self.upserts.append(dict(kwargs))
        self.resource = dict(kwargs)


def _manager(channel):
    bot = SimpleNamespace(
        user=SimpleNamespace(id="42"),
        get_channel=lambda _channel_id: channel,
        fetch_channel=None,
    )
    return SimpleNamespace(sheet_id="sheet-1", bot=bot)


def _service(repository):
    return SimpleNamespace(registration_repository=repository)


def _snapshot():
    return SimpleNamespace(
        round_row={
            "tournament_id": "LA-TEST",
            "round_id": "LA-TEST-Q3",
            "round_number": "3",
            "round_stage": "qualification",
        }
    )


@pytest.fixture(autouse=True)
def reset_guard_state(monkeypatch):
    guard._locks.clear()
    guard._canonical_message_ids.clear()

    async def config(_sheet_id):
        return {"ORGANIZER_CHANNEL_ID": "123"}, []

    monkeypatch.setattr(guard, "load_pr5_config", config)


@pytest.mark.asyncio
async def test_concurrent_preview_sync_sends_only_once_when_sheet_read_is_stale():
    """Exact startup race: both callers can initially see no resource row."""
    channel = FakeChannel()
    repository = FakeRepository(stale_reads=True)
    manager = _manager(channel)
    service = _service(repository)
    snapshot = _snapshot()

    def embed():
        return discord.Embed(
            title="Qualification Round 3 · Organizer Preview",
            description="same preview",
        )

    await asyncio.gather(
        guard._sync_preview_resource(
            manager,
            service,
            snapshot,
            resource_type="swiss_preview",
            resource_key="q3",
            embed=embed(),
            notes="preview",
        ),
        guard._sync_preview_resource(
            manager,
            service,
            snapshot,
            resource_type="swiss_preview",
            resource_key="q3",
            embed=embed(),
            notes="preview",
        ),
    )

    assert channel.send_count == 1
    assert len(repository.upserts) == 2
    assert {row["message_id"] for row in repository.upserts} == {"1000"}
    assert len([m for m in channel.messages.values() if not m.deleted]) == 1


@pytest.mark.asyncio
async def test_existing_exact_duplicate_previews_converge_to_oldest_registered_message():
    """A deploy repairs the duplicate Q3 previews already visible in Captain's Table."""
    embed = discord.Embed(
        title="Qualification Round 3 · Organizer Preview",
        description="same preview",
    )
    channel = FakeChannel()
    older = FakeMessage(900, embed.copy(), "42")
    newer = FakeMessage(901, embed.copy(), "42")
    channel.messages = {900: older, 901: newer}

    repository = FakeRepository(
        {
            "state": "active",
            "message_id": "901",
            "created_at_utc": "2026-08-16T15:57:00Z",
        }
    )

    canonical = await guard._sync_preview_resource(
        _manager(channel),
        _service(repository),
        _snapshot(),
        resource_type="swiss_preview",
        resource_key="q3",
        embed=embed,
        notes="preview",
    )

    assert canonical.id == 900
    assert older.deleted is False
    assert newer.deleted is True
    assert repository.upserts[-1]["message_id"] == "900"


@pytest.mark.asyncio
async def test_duplicate_cleanup_never_deletes_non_bot_messages():
    embed = discord.Embed(
        title="Semifinal · Organizer Preview",
        description="same preview",
    )
    channel = FakeChannel()
    bot_preview = FakeMessage(800, embed.copy(), "42")
    user_copy = FakeMessage(799, embed.copy(), "999")
    channel.messages = {800: bot_preview, 799: user_copy}

    repository = FakeRepository(
        {
            "state": "active",
            "message_id": "800",
            "created_at_utc": "2026-08-16T15:57:00Z",
        }
    )

    canonical = await guard._sync_preview_resource(
        _manager(channel),
        _service(repository),
        SimpleNamespace(
            round_row={
                "tournament_id": "LA-TEST",
                "round_id": "LA-TEST-SF",
                "round_stage": "semifinal",
            }
        ),
        resource_type="knockout_preview",
        resource_key="semifinal",
        embed=embed,
        notes="preview",
    )

    assert canonical.id == 800
    assert bot_preview.deleted is False
    assert user_copy.deleted is False
    assert repository.upserts[-1]["resource_type"] == "knockout_preview"
    assert repository.upserts[-1]["resource_key"] == "semifinal"
