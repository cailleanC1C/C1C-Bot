import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest

from modules.onboarding.constants import CLAN_TAG_PROMPT_HELPER
from modules.onboarding import thread_scopes
from modules.onboarding import watcher_promo
from modules.onboarding.watcher_welcome import parse_promo_thread_name
from shared.sheets import onboarding as onboarding_sheets


class DummyMessage:
    def __init__(self, content: str | None = None, mid: int | None = None) -> None:
        self.content = content or ""
        self.id = mid if mid is not None else 0
        self.edits: list[tuple[str | None, object | None]] = []

    async def edit(self, content: str | None = None, view: object | None = None) -> None:  # pragma: no cover - helper
        self.edits.append((content, view))


class DummyThread:
    def __init__(self, name: str, parent_id: int, created_at: dt.datetime | None = None) -> None:
        self.name = name
        self.parent_id = parent_id
        self.id = hash(name) % 10000
        self.created_at = created_at or dt.datetime.now(dt.timezone.utc)
        self.archived = False
        self.locked = False
        self.sent: list[tuple[str | None, object | None, DummyMessage]] = []

    async def send(self, content: str | None = None, view: object | None = None) -> DummyMessage:
        message_id = len(self.sent) + 1
        message = DummyMessage(content, message_id)
        self.sent.append((content, view, message))
        return message

    async def fetch_message(self, message_id: int) -> DummyMessage:  # pragma: no cover - helper
        return DummyMessage(f"fetched-{message_id}")


class DummyAuthor(SimpleNamespace):
    bot: bool = False


@pytest.fixture(autouse=True)
def _patch_thread_type(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(watcher_promo.discord, "Thread", DummyThread)


@pytest.fixture
def promo_setup(monkeypatch: pytest.MonkeyPatch):
    calls: dict[str, list] = {"upserts": [], "finds": [], "tags": [["C1CE", "KN1F"]]}
    promo_parent = 2024
    ticket_tool_id = 999

    monkeypatch.setattr(watcher_promo, "get_promo_channel_id", lambda: promo_parent)
    monkeypatch.setattr(watcher_promo, "get_ticket_tool_bot_id", lambda: ticket_tool_id)
    monkeypatch.setattr(thread_scopes, "is_promo_parent", lambda thread: getattr(thread, "parent_id", None) == promo_parent)
    monkeypatch.setattr(
        watcher_promo.feature_flags,
        "is_enabled",
        lambda name: True,
    )

    def _fake_upsert(row, headers):
        calls["upserts"].append((list(row), list(headers)))
        return "inserted" if len(calls["upserts"]) == 1 else "updated"

    def _fake_find(ticket):
        calls["finds"].append(ticket)
        return None

    monkeypatch.setattr(onboarding_sheets, "upsert_promo", _fake_upsert)
    monkeypatch.setattr(onboarding_sheets, "find_promo_row", _fake_find)
    monkeypatch.setattr(onboarding_sheets, "load_clan_tags", lambda force=False: calls["tags"][0])

    watcher = watcher_promo.PromoTicketWatcher(bot=SimpleNamespace())
    return watcher, calls, promo_parent, ticket_tool_id


def test_parse_promo_thread_name_maps_types() -> None:
    parsed = parse_promo_thread_name("M0123-scout")
    assert parsed is not None
    assert parsed.ticket_code == "M0123"
    assert parsed.username == "scout"
    assert parsed.promo_type == "player move request"

    with_tag = parse_promo_thread_name("L9999-lead-ABC")
    assert with_tag is not None
    assert with_tag.clan_tag == "ABC"

    assert parse_promo_thread_name("bad-name") is None


def test_promo_recognizes_leadership_thread_name() -> None:
    parsed = parse_promo_thread_name("L0005-caillean")

    assert parsed is not None
    assert parsed.ticket_code == "L0005"
    assert parsed.username == "caillean"
    assert parsed.promo_type == "clan lead move request"


def test_upsert_promo_inserts_and_updates(monkeypatch: pytest.MonkeyPatch) -> None:
    rows: list[list[str]] = []

    class FakeWorksheet:
        id = 1

        def row_values(self, _idx):
            return rows[0] if rows else []

        def get_all_values(self):
            return rows

        def update(self, _range, values):
            if _range == "A1":
                rows[:1] = list(values)
                return
            row_label = _range.split(":", 1)[0]
            digits = "".join(ch for ch in row_label if ch.isdigit())
            row_idx = int(digits or "1")
            while len(rows) < row_idx:
                rows.append([""] * len(values[0]))
            rows[row_idx - 1] = list(values[0])

        def append_row(self, row, value_input_option=None):  # pragma: no cover - helper
            rows.append(list(row))

    monkeypatch.setattr(onboarding_sheets.core, "call_with_backoff", lambda func, *args, **kwargs: func(*args, **kwargs))
    monkeypatch.setattr(onboarding_sheets, "_worksheet", lambda tab: FakeWorksheet())
    monkeypatch.setattr(onboarding_sheets, "_promo_tab", lambda: "PromoTickets")
    monkeypatch.setattr(onboarding_sheets, "_sheet_id", lambda: "sheet")

    base_row = [
        "R0001",
        "user",
        "",
        "",
        "returning player",
        "2025-11-24 00:00:00",
        "2025",
        "November",
        "",
        "",
        "",
    ]

    result_insert = onboarding_sheets.upsert_promo(base_row, onboarding_sheets.PROMO_HEADERS)
    assert result_insert == "inserted"
    assert rows[0] == onboarding_sheets.PROMO_HEADERS
    assert len(rows) == 2
    assert rows[1][:5] == base_row[:5]

    updated_row = base_row[:]
    updated_row[2] = "C1CE"
    result_update = onboarding_sheets.upsert_promo(updated_row, onboarding_sheets.PROMO_HEADERS)
    assert result_update == "updated"
    assert len(rows) == 2
    assert rows[1][2] == "C1CE"

    found = onboarding_sheets.find_promo_row("R0001")
    assert found is not None
    row_idx, mapping = found
    assert row_idx == 2
    assert mapping["clantag"] == "C1CE"


def test_promo_clan_tag_helper_text_matches_welcome(promo_setup):
    watcher, _calls, promo_parent, ticket_tool_id = promo_setup
    thread = DummyThread("R3333-helper", promo_parent)

    async def run_flow():
        await watcher.on_thread_create(thread)
        close_message = SimpleNamespace(
            content="Ticket closed via bot",
            author=DummyAuthor(id=ticket_tool_id, bot=False),
            channel=thread,
        )
        await watcher.on_message(close_message)

    asyncio.run(run_flow())

    assert thread.sent, "expected clan tag prompt to be sent"
    prompt_content, _view, _message = thread.sent[0]
    assert CLAN_TAG_PROMPT_HELPER in prompt_content
    lower_content = (prompt_content or "").lower()
    assert "progression" not in lower_content
    assert "skip" not in lower_content


def test_promo_watcher_logs_open_on_thread_create(promo_setup):
    watcher, calls, promo_parent, _ = promo_setup
    thread = DummyThread("R1111-alpha", promo_parent)

    async def run():
        await watcher.on_thread_create(thread)

    asyncio.run(run())
    assert calls["upserts"], "expected promo upsert on thread create"
    row, headers = calls["upserts"][0]
    assert row[0] == "R1111"
    assert "type" in [h.lower() for h in headers]


def test_promo_watcher_close_flow_updates_sheet(promo_setup, monkeypatch: pytest.MonkeyPatch):
    watcher, calls, promo_parent, ticket_tool_id = promo_setup
    thread = DummyThread("M0002-beta", promo_parent)

    async def run_flow():
        await watcher.on_thread_create(thread)
        close_message = SimpleNamespace(
            content="Ticket closed via bot",
            author=DummyAuthor(id=ticket_tool_id, bot=False),
            channel=thread,
        )
        await watcher.on_message(close_message)

        assert thread.sent, "expected prompt to be sent"
        clan_message = SimpleNamespace(content="C1CE", author=DummyAuthor(bot=False), channel=thread)
        await watcher.on_message(clan_message)

        progression_message = SimpleNamespace(
            content="TH10 | Clan Name",
            author=DummyAuthor(bot=False),
            channel=thread,
        )
        await watcher.on_message(progression_message)

    asyncio.run(run_flow())
    assert len(calls["upserts"]) >= 2, "expected additional upsert after closure"
    final_row = calls["upserts"][-1][0]
    assert final_row[2] == "C1CE"
    assert final_row[-1] == "TH10"
    assert final_row[-2] == "Clan Name"


def test_promo_watcher_respects_feature_flags(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(watcher_promo, "get_promo_channel_id", lambda: 1)
    monkeypatch.setattr(watcher_promo, "get_ticket_tool_bot_id", lambda: 1)
    monkeypatch.setattr(thread_scopes, "is_promo_parent", lambda thread: True)
    monkeypatch.setattr(
        watcher_promo.feature_flags,
        "is_enabled",
        lambda name: False if name == "promo_enabled" else True,
    )

    monkeypatch.setattr(onboarding_sheets, "upsert_promo", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("should not run")))

    watcher = watcher_promo.PromoTicketWatcher(bot=SimpleNamespace())
    thread = DummyThread("R2222-user", parent_id=1)

    async def runner():
        await watcher.on_thread_create(thread)

    asyncio.run(runner())


def test_setup_is_idempotent_for_existing_promo_watcher() -> None:
    class DummyBot:
        def __init__(self) -> None:
            self._cogs = {}
            self.add_calls = 0

        def get_cog(self, name: str):
            return self._cogs.get(name)

        async def add_cog(self, cog) -> None:
            self.add_calls += 1
            self._cogs[type(cog).__name__] = cog

    async def _run() -> None:
        bot = DummyBot()
        await watcher_promo.setup(bot)
        await watcher_promo.setup(bot)
        assert bot.add_calls == 1

    asyncio.run(_run())
