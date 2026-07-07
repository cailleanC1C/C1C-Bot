import asyncio
import datetime as dt
from types import SimpleNamespace

import pytest


from modules.onboarding.constants import CLAN_TAG_PROMPT_HELPER
from modules.onboarding import thread_scopes
from modules.onboarding import watcher_promo
from modules.onboarding.watcher_welcome import parse_promo_thread_name
from shared.sheets import onboarding as onboarding_sheets


@pytest.fixture(autouse=True)
def _promo_source_header_config(monkeypatch):
    monkeypatch.setattr(
        "shared.sheets.onboarding.get_promo_source_clan_tag_header",
        lambda **_kwargs: "source_clan_tag",
    )


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
        self.edits: list[dict[str, object]] = []
        self.guild = SimpleNamespace(id=123)

    async def edit(self, **kwargs) -> None:
        self.edits.append(dict(kwargs))
        if "name" in kwargs:
            self.name = str(kwargs["name"])

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
        for idx, (row, headers) in enumerate(calls["upserts"], start=2):
            values = dict(zip(headers, row))
            if values.get("ticket number") == ticket:
                return idx, values
        return None

    monkeypatch.setattr(onboarding_sheets, "upsert_promo", _fake_upsert)

    def _fake_append(ticket, username, clan_tag, source_clan_tag, promo_type, thread_created, year, month, join_month, clan_name, progression, **_kwargs):
        row = [ticket, username, clan_tag, source_clan_tag, "", promo_type, thread_created, year, month, join_month, clan_name, progression]
        return _fake_upsert(row, onboarding_sheets.PROMO_HEADERS)

    monkeypatch.setattr(onboarding_sheets, "append_promo_ticket_row", _fake_append)
    monkeypatch.setattr(onboarding_sheets, "find_promo_row", _fake_find)
    monkeypatch.setattr(onboarding_sheets, "find_promo_row_by_thread_id", lambda _thread_id: None)
    monkeypatch.setattr(onboarding_sheets, "get_ticket_finalization_state", lambda _flow, values: {
        "finalization_status": values.get("finalization_status", ""),
        "reservation_status": values.get("reservation_status", ""),
        "clan_update_status": values.get("clan_update_status", ""),
        "finalization_note": values.get("finalization_note", ""),
    })
    monkeypatch.setattr(onboarding_sheets, "update_ticket_finalization_state", lambda *a, **k: "updated")
    monkeypatch.setattr(onboarding_sheets, "patch_promo_prompt_source", lambda **kwargs: "updated")
    monkeypatch.setattr(onboarding_sheets, "patch_promo_ticket_metadata", lambda **kwargs: "updated")
    monkeypatch.setattr(onboarding_sheets, "patch_promo_final_close", lambda **kwargs: _fake_upsert([kwargs.get("ticket"), "", kwargs.get("clan_tag"), kwargs.get("source_clan_tag"), kwargs.get("date_closed"), "", "", kwargs.get("year"), kwargs.get("month"), kwargs.get("join_month"), kwargs.get("clan_name"), kwargs.get("progression")], onboarding_sheets.PROMO_HEADERS))
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
    assert with_tag.username == "lead-ABC"
    assert with_tag.clan_tag is None

    duplicated = parse_promo_thread_name("M0392-M0392-J_Turbo")
    assert duplicated is not None
    assert duplicated.ticket_code == "M0392"
    assert duplicated.username == "J_Turbo"

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
    monkeypatch.setattr(onboarding_sheets, "get_promo_source_clan_tag_header", lambda **kwargs: "source_clan_tag")
    monkeypatch.setattr(onboarding_sheets, "get_finalization_headers", lambda flow, **kwargs: {
        "finalization_status": "finalization_status",
        "reservation_status": "reservation_status",
        "clan_update_status": "clan_update_status",
        "finalization_note": "finalization_note",
    })
    live_headers = list(onboarding_sheets.PROMO_HEADERS) + [
        "finalization_status",
        "reservation_status",
        "clan_update_status",
        "finalization_note",
    ]
    rows.append(live_headers)

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

    result_insert = onboarding_sheets.upsert_promo(base_row, live_headers)
    assert result_insert == "inserted"
    assert rows[0] == live_headers
    assert len(rows) == 2
    assert rows[1][:5] == base_row[:5]

    updated_row = base_row[:]
    updated_row[2] = "C1CE"
    result_update = onboarding_sheets.upsert_promo(updated_row, live_headers)
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
        source_message = SimpleNamespace(content="NONE", author=DummyAuthor(bot=False), channel=thread)
        await watcher.on_message(source_message)

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
    assert final_row[-1] == ""
    assert final_row[-2] == ""



def _patch_promo_close_dependencies(monkeypatch: pytest.MonkeyPatch, *, open_spots: int = 2, reservations=None):
    state = {"open_spots": open_spots}
    deltas: list[tuple[str, int]] = []
    log_messages: list[str] = []

    async def fresh(**_kwargs):
        return True

    async def find_reservations(*_args, **_kwargs):
        return list(reservations or [])

    def find_clan_row(tag, force=False):
        normalized = str(tag).strip().upper()
        if normalized not in {"F-IT", "C1CV"}:
            return None
        return (7, ["", "", normalized, "", str(state["open_spots"]), "0", "0", ""])

    async def adjust(tag, delta):
        deltas.append((tag, delta))
        state["open_spots"] += delta
        return state["open_spots"]

    async def recompute(*_args, **_kwargs):
        return None

    async def update_status(*_args, **_kwargs):
        return None

    async def send_log(message):
        log_messages.append(message)

    monkeypatch.setattr(watcher_promo, "_ensure_fresh_clans_for_placement", fresh)
    monkeypatch.setattr(watcher_promo.reservations_sheets, "find_active_reservations_for_recruit", find_reservations)
    monkeypatch.setattr(watcher_promo.reservations_sheets, "update_reservation_status", update_status)
    monkeypatch.setattr(watcher_promo.recruitment_sheets, "find_clan_row", find_clan_row)
    monkeypatch.setattr(watcher_promo.recruitment_sheets, "get_clan_header_map", lambda: {
        "open_spots": 4,
        "inactives": 5,
        "reservation_count": 6,
        "reservation_summary": 7,
    })
    def find_promo_row(ticket):
        return (2, {
            "ticket number": ticket,
            "username": "",
            "clantag": "",
            "source_clan_tag": "C1CV",
            "finalization_status": "pending",
            "reservation_status": "pending",
            "clan_update_status": "pending",
            "finalization_note": "",
        })

    monkeypatch.setattr(watcher_promo.availability, "adjust_manual_open_spots", adjust)
    monkeypatch.setattr(watcher_promo.availability, "recompute_clan_availability", recompute)
    monkeypatch.setattr(watcher_promo.rt, "send_log_message", send_log)
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "find_promo_row", find_promo_row)
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "get_ticket_finalization_state", lambda _flow, values: {
        "finalization_status": values.get("finalization_status", ""),
        "reservation_status": values.get("reservation_status", ""),
        "clan_update_status": values.get("clan_update_status", ""),
        "finalization_note": values.get("finalization_note", ""),
    })
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "update_ticket_finalization_state", lambda *a, **k: "updated")
    monkeypatch.setattr(watcher_promo.onboarding_sheets, "patch_promo_final_close", lambda **kwargs: "updated")
    monkeypatch.setattr(watcher_promo.onboarding_sessions, "mark_completed", lambda *_args, **_kwargs: None)
    return state, deltas, log_messages


def test_promo_move_close_preserves_full_ticket_id_and_consumes_unreserved_spot(
    promo_setup, monkeypatch: pytest.MonkeyPatch
):
    watcher, calls, promo_parent, _ticket_tool_id = promo_setup
    state, deltas, log_messages = _patch_promo_close_dependencies(
        monkeypatch, open_spots=2
    )
    thread = DummyThread("M0352-Lucifer", promo_parent)
    context = watcher_promo.PromoTicketContext(
        thread_id=thread.id,
        ticket_number="M0352",
        username="Lucifer",
        promo_type="move",
        thread_created="2026-06-08 00:00:00",
        year="2026",
        month="June",
        clan_tag="F-IT",
        source_clan_tag="C1CV",
        user_id=12345,
    )

    async def run():
        await watcher._complete_close(
            thread, context, progression="", clan_name="", previous_final=""
        )

    asyncio.run(run())

    assert context.state == "closed"
    assert thread.name == "Closed-M0352-Lucifer-F-IT"
    assert thread.edits[-1]["name"] == "Closed-M0352-Lucifer-F-IT"
    assert sorted(deltas) == [("C1CV", 1), ("F-IT", -1)]
    assert state["open_spots"] == 2
    assert log_messages, "expected logging-channel open spot delta entry"
    assert "M0352 • Lucifer → F-IT" in log_messages[-1]
    assert "F-IT:-1" in log_messages[-1]



def test_promo_move_close_normalizes_ticket_prefixed_username_and_logs_rows(
    promo_setup, monkeypatch: pytest.MonkeyPatch
):
    watcher, calls, promo_parent, _ticket_tool_id = promo_setup
    _state, deltas, log_messages = _patch_promo_close_dependencies(
        monkeypatch, open_spots=2
    )
    thread = DummyThread("M0392-J_Turbo", promo_parent)
    context = watcher_promo.PromoTicketContext(
        thread_id=thread.id,
        ticket_number="M0392",
        username="M0392-J_Turbo",
        promo_type="move",
        thread_created="2026-07-07 00:00:00",
        year="2026",
        month="July",
        clan_tag="F-IT",
        source_clan_tag="C1CV",
        user_id=12345,
    )

    asyncio.run(watcher._complete_close(thread, context, progression="", clan_name="", previous_final=""))

    assert context.state == "closed"
    assert thread.name == "Closed-M0392-J_Turbo-F-IT"
    assert sorted(deltas) == [("C1CV", 1), ("F-IT", -1)]
    assert log_messages
    assert "M0392 • J_Turbo" in log_messages[-1]
    assert "M0392 • M0392-J_Turbo" not in log_messages[-1]
    assert "snapshot unavailable" not in log_messages[-1]


def test_promo_close_recompute_uses_promo_lookup_for_source_and_destination(
    promo_setup, monkeypatch: pytest.MonkeyPatch
):
    watcher, _calls, promo_parent, _ticket_tool_id = promo_setup
    recomputed: list[str] = []
    lookup_functions = []

    async def fake_recompute(tag, **kwargs):
        recomputed.append(tag)
        lookup = kwargs.get("find_clan_row_fn")
        lookup_functions.append(lookup)
        assert lookup is not None
        assert lookup(tag, SimpleNamespace(header_map={"clan_tag": 2})) is not None

    _state, _deltas, log_messages = _patch_promo_close_dependencies(
        monkeypatch, open_spots=2
    )
    monkeypatch.setattr(watcher_promo.availability, "recompute_clan_availability", fake_recompute)

    def find_clan_row(tag, force=False):
        normalized = str(tag).strip().upper()
        if normalized not in {"C1CV", "C1CZ"}:
            return None
        return (7 if normalized == "C1CZ" else 8, ["", "", normalized, "", "2", "0", "0", ""])

    monkeypatch.setattr(watcher_promo.recruitment_sheets, "find_clan_row", find_clan_row)
    monkeypatch.setattr(
        watcher_promo.onboarding_sheets,
        "find_promo_row",
        lambda ticket: (2, {
            "ticket number": ticket,
            "username": "",
            "clantag": "",
            "source_clan_tag": "C1CZ",
            "finalization_status": "pending",
            "reservation_status": "pending",
            "clan_update_status": "pending",
            "finalization_note": "",
        }),
    )

    thread = DummyThread("M0392-J_Turbo", promo_parent)
    context = watcher_promo.PromoTicketContext(
        thread_id=thread.id,
        ticket_number="M0392",
        username="M0392-J_Turbo",
        promo_type="move",
        thread_created="2026-07-07 00:00:00",
        year="2026",
        month="July",
        clan_tag="C1CV",
        source_clan_tag="C1CZ",
        user_id=12345,
    )

    asyncio.run(watcher._complete_close(thread, context, progression="", clan_name="", previous_final=""))

    assert context.state == "closed"
    assert sorted(recomputed) == ["C1CV", "C1CZ"]
    assert all(lookup is watcher_promo._find_promo_availability_clan_row for lookup in lookup_functions)
    assert thread.name == "Closed-M0392-J_Turbo-C1CV"
    assert log_messages
    assert "M0392 • J_Turbo" in log_messages[-1]
    assert "snapshot unavailable" not in log_messages[-1]

def test_reserved_promo_move_same_clan_does_not_double_consume_open_spot(
    promo_setup, monkeypatch: pytest.MonkeyPatch
):
    watcher, _calls, promo_parent, _ticket_tool_id = promo_setup
    reservation = watcher_promo.reservations_sheets.ReservationRow(
        row_number=4,
        thread_id="111",
        ticket_user_id=12345,
        recruiter_id=None,
        clan_tag="F-IT",
        reserved_until=None,
        created_at=None,
        status="active",
        notes="",
        username_snapshot="Lucifer",
        raw=[],
    )
    state, deltas, log_messages = _patch_promo_close_dependencies(
        monkeypatch, open_spots=2, reservations=[reservation]
    )
    thread = DummyThread("M0352-Lucifer", promo_parent)
    context = watcher_promo.PromoTicketContext(
        thread_id=thread.id,
        ticket_number="M0352",
        username="Lucifer",
        promo_type="move",
        thread_created="2026-06-08 00:00:00",
        year="2026",
        month="June",
        clan_tag="F-IT",
        source_clan_tag="C1CV",
        user_id=12345,
    )

    async def run():
        await watcher._complete_close(
            thread, context, progression="", clan_name="", previous_final=""
        )

    asyncio.run(run())

    assert thread.name == "Closed-M0352-Lucifer-F-IT"
    assert deltas == [("C1CV", 1)]
    assert state["open_spots"] == 3
    assert log_messages
    assert "reservation=row4(same)" in log_messages[-1]
    assert "C1CV:+1" in log_messages[-1]

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
