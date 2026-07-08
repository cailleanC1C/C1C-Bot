import asyncio
import datetime as dt
import logging
import itertools
from typing import List

import discord
import pytest

from modules.placement import reservations as reserve_module
from shared.sheets import reservations as reservations_sheet

_REAL_AVAILABILITY_PREFLIGHT = (
    reserve_module.availability.preflight_clan_availability_update
)


@pytest.fixture(autouse=True)
def _stub_availability_preflight(monkeypatch):
    header_map = {
        "clan_tag": 2,
        "manual_open_spots": 4,
        "open_spots": 31,
        "inactives": 32,
        "reservation_count": 33,
        "reservation_summary": 34,
        "manual_open_spots_seen": 35,
    }

    class Plan:
        sheet_row = 7
        row = (
            ("", "Clan", "#ABC", "", "3")
            + ("",) * 26
            + ("3", "0", "0", "", "3")
            + ("",) * 10
        )
        numeric_values = {
            "manual_open_spots": 3,
            "open_spots": 3,
            "inactives": 0,
            "reservation_count": 0,
            "manual_open_spots_seen": 3,
        }
        write_ranges = {
            "open_spots": "AF7",
            "inactives": "AG7",
            "reservation_count": "AH7",
            "reservation_summary": "AI7",
            "manual_open_spots_seen": "AJ7",
            "manual_open_spots": "E7",
        }
        headers = type(
            "Headers", (), {"header_map": header_map, "tab_name": "bot_info"}
        )()

    async def fake_preflight(_tag, *, delta=0, **_kwargs):
        return Plan()

    monkeypatch.setattr(
        reserve_module.availability,
        "preflight_clan_availability_update",
        fake_preflight,
    )
    monkeypatch.setattr(
        reserve_module, "_resolve_configured_reservation_clan_tag", lambda tag: tag
    )

    async def fake_async_resolve(tag):
        return tag

    monkeypatch.setattr(
        reserve_module, "_aresolve_configured_reservation_clan_tag", fake_async_resolve
    )

    async def fake_afind_clan_row(tag, *, force=False):
        try:
            row = reserve_module.recruitment.get_clan_by_tag(tag)
        except Exception:
            row = None
        if row is not None:
            return (10, list(row))
        try:
            found = reserve_module.recruitment.find_clan_row(tag)
        except Exception:
            return None
        if found is None:
            return None
        row_number, values = found
        return row_number, list(values)

    monkeypatch.setattr(
        reserve_module.recruitment, "afind_clan_row", fake_afind_clan_row
    )

    async def fake_active_reservations_for_clan(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        reserve_module.reservations,
        "get_active_reservations_for_clan",
        fake_active_reservations_for_clan,
    )


class FakeMember:
    def __init__(self, user_id: int, display_name: str = "Recruit") -> None:
        self.id = user_id
        self.display_name = display_name
        self.name = display_name
        self.mention = f"<@{user_id}>"


class FakeGuild:
    def __init__(self, members: List[FakeMember]) -> None:
        self._members = {member.id: member for member in members}

    def get_member(self, member_id: int):
        return self._members.get(member_id)

    def get_channel(self, channel_id: int):
        return None

    def get_thread(self, thread_id: int):
        return None


_message_ids = itertools.count(1000)


class FakeThread:
    def __init__(
        self,
        thread_id: int,
        parent_id: int,
        *,
        name: str = "W0000-Test",
        owner_id: int | None = None,
        guild: object | None = None,
    ) -> None:
        self.id = thread_id
        self.parent_id = parent_id
        self.type = discord.ChannelType.private_thread
        self.name = name
        self.owner_id = owner_id
        self.guild = guild
        self.sent: list[FakeSentMessage] = []
        self.edited_names: list[str] = []
        self.delete_error_for_sent: Exception | None = None

    async def send(self, content: str | None = None, **kwargs):
        message = FakeSentMessage(content, kwargs)
        message.delete_error = self.delete_error_for_sent
        self.sent.append(message)
        return message

    async def edit(self, *, name: str | None = None, **_kwargs):
        if name is not None:
            self.name = name
            self.edited_names.append(name)
        return self


class FakeSentMessage:
    def __init__(self, content: str | None, kwargs: dict) -> None:
        self.id = next(_message_ids)
        self.content = content
        self.kwargs = dict(kwargs)
        self.deleted = False
        self.delete_error: Exception | None = None

    async def delete(self):
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted = True

    async def edit(self, *, content: str | None = None, **kwargs):
        if content is not None:
            self.content = content
        self.kwargs.update(kwargs)
        return self


class FakeMessage:
    def __init__(self, content: str, author, channel, mentions=None) -> None:
        self.id = next(_message_ids)
        self.content = content
        self.author = author
        self.channel = channel
        self.mentions = list(mentions or [])
        self.deleted = False
        self.delete_error: Exception | None = None

    async def delete(self):
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted = True


class FakeBot:
    def __init__(
        self,
        messages: List[FakeMessage],
        *,
        channels: dict[int, FakeThread] | None = None,
    ) -> None:
        self._messages = list(messages)
        self._channels = dict(channels or {})

    async def wait_for(self, event_name: str, *, timeout: float, check):
        while self._messages:
            message = self._messages.pop(0)
            if check(message):
                return message
        raise asyncio.TimeoutError

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)


class FakeContext:
    def __init__(
        self, bot: FakeBot, guild: FakeGuild, channel: FakeThread, author
    ) -> None:
        self.bot = bot
        self.guild = guild
        self.channel = channel
        self.author = author
        self.message = FakeMessage("!reserve ABC", author=author, channel=channel)
        self.replies: list[FakeSentMessage] = []

    async def reply(self, content: str, *, mention_author: bool = False):
        message = await self.channel.send(content=content)
        self.replies.append(message)
        return message

    async def send(self, content: str, **kwargs):
        return await self.channel.send(content=content, **kwargs)


def _verified_recompute_result(tag="#ABC", *, reserved=1, available=2):
    return reserve_module.availability.AvailabilityRecomputeResult(
        clan_tag=tag,
        sheet_row=7,
        available_after_reservations=available,
        reservation_count=reserved,
        reservation_summary="",
        cache_updated=True,
        verified=True,
    )


def _enable_feature(monkeypatch, enabled: bool = True) -> None:
    monkeypatch.setattr(
        reserve_module.feature_flags,
        "is_enabled",
        lambda key: enabled,
        raising=False,
    )


def _setup_parents(monkeypatch, parent_id: int) -> None:
    monkeypatch.setattr(reserve_module, "get_welcome_channel_id", lambda: parent_id)
    monkeypatch.setattr(reserve_module, "get_promo_channel_id", lambda: parent_id)


def _setup_permissions(monkeypatch, recruiter: bool, admin: bool = False) -> None:
    monkeypatch.setattr(reserve_module, "is_recruiter", lambda ctx: recruiter)
    monkeypatch.setattr(reserve_module, "is_admin_member", lambda ctx: admin)


def _setup_control_channels(
    monkeypatch,
    *,
    recruiters_thread: int | None = None,
    interact_channel: int | None = None,
) -> None:
    monkeypatch.setattr(
        reserve_module, "get_recruiters_thread_id", lambda: recruiters_thread
    )
    monkeypatch.setattr(
        reserve_module,
        "get_recruitment_interact_channel_id",
        lambda: interact_channel,
    )


def _make_cog(bot: FakeBot) -> reserve_module.ReservationCog:
    return reserve_module.ReservationCog(bot)  # type: ignore[arg-type]


def _reservation_row(
    row_number: int,
    *,
    clan_tag: str,
    reserved_until: dt.date | None = None,
    status: str = "active",
    thread_id: int = 555,
    ticket_user_id: int | None = 222,
    recruiter_id: int = 111,
    username_snapshot: str = "Recruit",
    created_at: dt.datetime | None = None,
) -> reservations_sheet.ReservationRow:
    return reservations_sheet.ReservationRow(
        row_number=row_number,
        thread_id=str(thread_id),
        ticket_user_id=ticket_user_id,
        recruiter_id=recruiter_id,
        clan_tag=clan_tag,
        reserved_until=reserved_until,
        created_at=created_at,
        status=status,
        notes="",
        username_snapshot=username_snapshot,
        raw=[
            str(thread_id),
            str(ticket_user_id or ""),
            str(recruiter_id),
            clan_tag,
            reserved_until.isoformat() if reserved_until else "",
            "",
            status,
            "",
            username_snapshot,
        ],
    )


def _reservation_ledger(
    rows: list[reservations_sheet.ReservationRow],
) -> reservations_sheet.ReservationLedger:
    return reservations_sheet.ReservationLedger(rows=list(rows), status_index=0)


def test_reserve_availability_preflight_uses_async_sheets(monkeypatch):
    header = [""] * 36
    configured = {
        "clan_tag": "Clan Tag",
        "manual_open_spots": "Manual Open Spots",
        "open_spots": "Open Spots",
        "inactives": "Inactives",
        "reservation_count": "Reservation Count",
        "reservation_summary": "Reservation Summary",
        "manual_open_spots_seen": "Manual Open Spots Seen",
    }
    indexes = {
        "clan_tag": 2,
        "manual_open_spots": 4,
        "open_spots": 31,
        "inactives": 32,
        "reservation_count": 33,
        "reservation_summary": 34,
        "manual_open_spots_seen": 35,
    }
    for key, index in indexes.items():
        header[index] = configured[key]
    clan_row = [""] * 36
    clan_row[2] = "FIT"
    clan_row[4] = "3"
    clan_row[31] = "3"
    clan_row[32] = "0"
    clan_row[33] = "0"
    clan_row[34] = ""
    clan_row[35] = "3"

    class _Worksheet:
        pass

    async def _config_value(key, default=None, *, force=False):
        if key == "clans_tab":
            return "bot_info"
        prefix = "clans_header_"
        if key.startswith(prefix):
            return configured[key.removeprefix(prefix)]
        return default

    async def _header_row(*, force=False):
        return list(header)

    async def _clans(*, force=False):
        return [list(clan_row)]

    async def _aget(sheet_id, tab_name):
        assert sheet_id == "recruitment-sheet"
        assert tab_name == "bot_info"
        return _Worksheet()

    def _sync_forbidden(*_args, **_kwargs):
        raise AssertionError(
            "sync Sheets/config helper must not run during reserve preflight"
        )

    monkeypatch.setattr(
        reserve_module.availability,
        "preflight_clan_availability_update",
        _REAL_AVAILABILITY_PREFLIGHT,
    )
    monkeypatch.setattr(
        reserve_module.recruitment,
        "get_recruitment_sheet_id",
        lambda: "recruitment-sheet",
    )
    monkeypatch.setattr(
        reserve_module.recruitment, "get_config_value_async", _config_value
    )
    monkeypatch.setattr(
        reserve_module.recruitment,
        "get_clans_tab_name_async",
        lambda: asyncio.sleep(0, result="bot_info"),
    )
    monkeypatch.setattr(reserve_module.recruitment, "aget_clan_header_row", _header_row)
    monkeypatch.setattr(reserve_module.recruitment, "afetch_clans", _clans)
    monkeypatch.setattr(reserve_module.recruitment, "get_config_value", _sync_forbidden)
    monkeypatch.setattr(
        reserve_module.recruitment, "get_clan_header_row", _sync_forbidden
    )
    monkeypatch.setattr(reserve_module.recruitment, "fetch_clans", _sync_forbidden)
    monkeypatch.setattr(reserve_module.recruitment, "find_clan_row", _sync_forbidden)
    monkeypatch.setattr(reserve_module.availability.async_core, "aget_worksheet", _aget)
    monkeypatch.setattr(
        reserve_module.reservations,
        "count_active_reservations_for_clan",
        lambda *_: asyncio.sleep(0, result=0),
    )

    parent_id = 555
    recruit = FakeMember(2222, "Recruit")
    author = FakeMember(1111, "Recruiter")
    guild = FakeGuild([recruit, author])
    thread = FakeThread(
        999, parent_id, name="W1234-Recruit", owner_id=recruit.id, guild=guild
    )
    bot = FakeBot([])
    ctx = FakeContext(bot, guild, thread, author)

    _enable_feature(monkeypatch, True)
    _setup_parents(monkeypatch, parent_id)
    _setup_permissions(monkeypatch, recruiter=True)

    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "FIT"))

    assert any(
        "Who do you want to reserve" in (message.content or "")
        for message in thread.sent
    )


def test_reserve_success(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_parents(monkeypatch, parent_id=999)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch)

    recruit = FakeMember(222, "Recruit One")
    guild = FakeGuild([recruit])
    thread = FakeThread(thread_id=555, parent_id=999)

    mention_message = FakeMessage(
        "reserve user", author=None, channel=thread, mentions=[recruit]
    )
    date_message = FakeMessage("2026-12-01", author=None, channel=thread)
    confirm_message = FakeMessage("yes", author=None, channel=thread)

    author = FakeMember(111, "Recruiter")
    for message in (mention_message, date_message, confirm_message):
        message.author = author

    bot = FakeBot([mention_message, date_message, confirm_message])
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)

    clan_row = ["", "Clan", "#ABC", "", "3"] + [""] * 40
    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (10, list(clan_row))
    )

    def fake_updated_row(tag):
        updated = list(clan_row)
        while len(updated) <= 33:
            updated.append("")
        updated[31] = "1"
        updated[33] = "2"
        return updated

    monkeypatch.setattr(reserve_module.recruitment, "get_clan_by_tag", fake_updated_row)

    async def fake_count(tag):
        return 1

    monkeypatch.setattr(
        reserve_module.reservations,
        "count_active_reservations_for_clan",
        fake_count,
    )

    async def fake_find_active(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        fake_find_active,
    )

    appended: list[list[str]] = []

    async def fake_append(row_values):
        appended.append(list(row_values))

    monkeypatch.setattr(
        reserve_module.reservations, "append_reservation_row", fake_append
    )

    recomputed = {}

    async def fake_recompute(clan_tag, **_kwargs):
        recomputed["tag"] = clan_tag
        return _verified_recompute_result(clan_tag, reserved=2, available=1)

    monkeypatch.setattr(
        reserve_module.availability, "recompute_clan_availability", fake_recompute
    )
    monkeypatch.setattr(
        reserve_module,
        "_ensure_fresh_clans_for_reservations",
        lambda **_: asyncio.sleep(0, result=True),
    )

    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "ABC"))

    assert thread.sent, "expected the reservation flow to prompt in-thread"
    if appended:
        saved_row = appended[0]
        assert saved_row[0] == str(thread.id)
        assert saved_row[1] == str(recruit.id)
        assert saved_row[3] == "#ABC"
        assert saved_row[6] == reserve_module.ACTIVE_STATUS
        assert saved_row[7] == ""
        assert saved_row[8] == recruit.display_name
        assert recomputed["tag"] == "#ABC"


def test_reserve_partial_success_when_recompute_fails(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_parents(monkeypatch, parent_id=999)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch)

    recruit = FakeMember(333, "Recruit Partial")
    guild = FakeGuild([recruit])
    thread = FakeThread(thread_id=557, parent_id=999)
    author = FakeMember(112, "Recruiter")
    bot = FakeBot([])
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)

    clan_row = ["", "Clan", "#ABC", "", "3"] + [""] * 40
    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (10, list(clan_row))
    )
    monkeypatch.setattr(
        reserve_module.recruitment, "get_clan_by_tag", lambda tag: clan_row
    )

    async def fake_count(*_args, **_kwargs):
        return 0

    async def fake_find_active(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        reserve_module.reservations, "count_active_reservations_for_clan", fake_count
    )
    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        fake_find_active,
    )

    async def fake_flow_run(self):
        return reserve_module.ReservationDetails(
            ticket_user_id=recruit.id,
            ticket_display=recruit.display_name,
            ticket_username=recruit.display_name,
            reserved_until=dt.date(2026, 12, 6),
            notes="",
        )

    monkeypatch.setattr(reserve_module.ReservationConversation, "run", fake_flow_run)
    monkeypatch.setattr(
        reserve_module.reservations,
        "append_reservation_row",
        lambda row: asyncio.sleep(0),
    )

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("sheet update exploded")

    monkeypatch.setattr(
        reserve_module.availability, "recompute_clan_availability", _boom
    )
    monkeypatch.setattr(
        reserve_module,
        "_ensure_fresh_clans_for_reservations",
        lambda **_: asyncio.sleep(0, result=True),
    )

    runtime_logs: list[tuple[str, str]] = []
    monkeypatch.setattr(
        reserve_module.human_log,
        "human",
        lambda level, message, **_: runtime_logs.append((level, message)),
    )

    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "ABC"))

    assert any(
        "Reservation row was added, but recruiter-facing availability was NOT updated."
        in m.content
        for m in thread.sent
    )
    assert any(
        level == "error"
        and "error_type=RuntimeError" in message
        and "source=reserve" in message
        for level, message in runtime_logs
    )


def test_recompute_updates_recruiter_facing_clans_tab(monkeypatch):
    worksheet_calls: list[str] = []

    monkeypatch.setattr(
        reserve_module.recruitment,
        "find_clan_row",
        lambda tag, force=True: (7, ["", "", "#ABC", "", "4"] + [""] * 60),
    )
    monkeypatch.setattr(
        reserve_module.recruitment,
        "get_clan_header_map",
        lambda: {
            "manual_open_spots": 4,
            "open_spots": 31,
            "manual_open_spots_seen": 35,
            "inactives": 32,
            "reservation_count": 33,
            "reservation_summary": 34,
        },
    )
    monkeypatch.setattr(
        reserve_module.recruitment, "get_recruitment_sheet_id", lambda: "sheet-id"
    )
    monkeypatch.setattr(
        reserve_module.recruitment, "get_clans_tab_name", lambda: "bot_info"
    )

    class _Worksheet:
        def update(self, cell_range, values, value_input_option="RAW"):
            worksheet_calls.append(cell_range)
            return {"ok": True}

    async def _aget(sheet_id, tab_name):
        assert sheet_id == "sheet-id"
        assert tab_name == "bot_info"
        return _Worksheet()

    monkeypatch.setattr(reserve_module.availability.async_core, "aget_worksheet", _aget)
    monkeypatch.setattr(
        reserve_module.availability.async_core,
        "acall_with_backoff",
        lambda fn, *a, **k: asyncio.sleep(0, result=fn(*a, **k)),
    )
    monkeypatch.setattr(
        reserve_module.availability.reservations,
        "get_active_reservations_for_clan",
        lambda *_: asyncio.sleep(0, result=[]),
    )
    monkeypatch.setattr(
        reserve_module.availability.reservations,
        "resolve_reservation_names",
        lambda *_a, **_k: asyncio.sleep(0, result=[]),
    )
    monkeypatch.setattr(
        reserve_module.recruitment, "update_cached_clan_row", lambda *_: True
    )

    asyncio.run(
        reserve_module.availability.recompute_clan_availability("#ABC", guild=None)
    )

    assert worksheet_calls, "expected worksheet updates"


def test_reserve_accepts_inline_recruit(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_parents(monkeypatch, parent_id=999)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch)

    recruit = FakeMember(222, "Recruit Inline")
    guild = FakeGuild([recruit])
    thread = FakeThread(thread_id=556, parent_id=999)

    date_message = FakeMessage("2026-12-05", author=None, channel=thread)
    confirm_message = FakeMessage("yes", author=None, channel=thread)
    author = FakeMember(111, "Recruiter")
    for message in (date_message, confirm_message):
        message.author = author

    bot = FakeBot([date_message, confirm_message])
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)

    clan_row = ["", "Clan", "#ABC", "", "3"] + [""] * 40
    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (10, list(clan_row))
    )
    monkeypatch.setattr(
        reserve_module.recruitment, "get_clan_by_tag", lambda tag: clan_row
    )

    async def fake_count(tag):
        return 0

    monkeypatch.setattr(
        reserve_module.reservations,
        "count_active_reservations_for_clan",
        fake_count,
    )

    async def fake_find_active(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        fake_find_active,
    )

    appended: list[list[str]] = []

    async def fake_append(row_values):
        appended.append(list(row_values))

    monkeypatch.setattr(
        reserve_module.reservations, "append_reservation_row", fake_append
    )
    monkeypatch.setattr(
        reserve_module.availability,
        "recompute_clan_availability",
        lambda tag, **kwargs: asyncio.sleep(0, result=_verified_recompute_result(tag)),
    )
    monkeypatch.setattr(
        reserve_module,
        "_ensure_fresh_clans_for_reservations",
        lambda **_: asyncio.sleep(0, result=True),
    )

    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "ABC", f"<@{recruit.id}>"))

    assert any("Until which date" in sent.content for sent in thread.sent[:1])
    if appended:
        assert appended[0][1] == str(recruit.id)


def test_ensure_fresh_clans_for_reservations_unavailable(monkeypatch, caplog):
    class _Snapshot:
        available = False
        last_result = "error"
        last_error = "boom"

    caplog.set_level(logging.WARNING, logger=reserve_module.log.name)
    monkeypatch.setattr(
        reserve_module.cache_telemetry, "refresh_now", lambda *_, **__: asyncio.sleep(0)
    )
    monkeypatch.setattr(
        reserve_module.cache_telemetry, "get_snapshot", lambda *_: _Snapshot()
    )

    result = asyncio.run(
        reserve_module._ensure_fresh_clans_for_reservations(
            actor="placement_reservation", clan_tag="C1C9", user="u", source="reserve"
        )
    )

    assert result is False
    assert any(
        getattr(record, "reason", None) == "fresh_clans_unavailable"
        for record in caplog.records
    )


def test_reserve_inline_recruit_validation_error(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_parents(monkeypatch, parent_id=999)
    _setup_permissions(monkeypatch, recruiter=True)

    recruit = FakeMember(222, "Recruit Inline")
    guild = FakeGuild([recruit])
    thread = FakeThread(thread_id=556, parent_id=999)
    author = FakeMember(111, "Recruiter")

    bot = FakeBot([])
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)

    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "ABC", "nonsense"))

    assert ctx.replies, "should respond with guidance when recruit argument is invalid"
    assert "understand" in ctx.replies[0].content


def test_reserve_duplicate_blocked(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_parents(monkeypatch, parent_id=1000)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch)

    recruit = FakeMember(3000, "Duplicate User")
    guild = FakeGuild([recruit])
    thread = FakeThread(
        thread_id=2000,
        parent_id=1000,
        name="W0500-Duplicate User",
        owner_id=recruit.id,
    )
    author = FakeMember(3001)

    mention_message = FakeMessage(
        "reserve user", author=author, channel=thread, mentions=[recruit]
    )
    date_message = FakeMessage("2026-12-01", author=author, channel=thread)
    confirm_message = FakeMessage("yes", author=author, channel=thread)

    bot = FakeBot([mention_message, date_message, confirm_message])
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)

    clan_row = ["", "Clan", "#ZZZ", "", "5"] + [""] * 40
    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (12, list(clan_row))
    )
    monkeypatch.setattr(
        reserve_module.recruitment, "get_clan_by_tag", lambda tag: clan_row
    )

    async def fake_count(*_args, **_kwargs):
        return 0

    monkeypatch.setattr(
        reserve_module.reservations,
        "count_active_reservations_for_clan",
        fake_count,
    )

    existing_row = _reservation_row(
        99,
        clan_tag="#OLD",
        reserved_until=dt.date(2025, 11, 30),
        thread_id=5555,
        ticket_user_id=recruit.id,
        username_snapshot="Duplicate User",
    )

    async def fake_existing(*_args, **_kwargs):
        return [existing_row]

    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        fake_existing,
    )

    async def fake_resolve(bot, thread_id):
        return FakeThread(
            thread_id=int(thread_id),
            parent_id=1000,
            name="Res-W0499-Other-C1CM",
            owner_id=recruit.id,
        )

    monkeypatch.setattr(reserve_module, "_resolve_thread", fake_resolve)

    appended: list[list[str]] = []

    async def fake_append(row_values):
        appended.append(list(row_values))

    monkeypatch.setattr(
        reserve_module.reservations, "append_reservation_row", fake_append
    )

    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "ZZZ"))

    assert not appended, "should not append when duplicate detected"
    assert ctx.replies or thread.sent, "expected the duplicate guard to respond"


def test_reserve_requires_reason(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_parents(monkeypatch, parent_id=777)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch)

    recruit = FakeMember(333, "Applicant")
    guild = FakeGuild([recruit])
    thread = FakeThread(thread_id=666, parent_id=777)

    author = FakeMember(444, "Recruiter")
    messages = [
        FakeMessage("who", author=author, channel=thread, mentions=[recruit]),
        FakeMessage("2026-11-30", author=author, channel=thread),
        FakeMessage(
            "Because they confirmed a start date", author=author, channel=thread
        ),
        FakeMessage("yes", author=author, channel=thread),
    ]

    bot = FakeBot(messages)
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)

    clan_row = ["", "Clan", "#DEF", "", "1"] + [""] * 40

    class ReasonPlan:
        sheet_row = 11
        row = tuple(clan_row)
        numeric_values = {"manual_open_spots": 1}
        headers = type("Headers", (), {"header_map": {"clan_tag": 2}})()

    monkeypatch.setattr(
        reserve_module.availability,
        "preflight_clan_availability_update",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=ReasonPlan()),
    )
    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (11, list(clan_row))
    )
    monkeypatch.setattr(
        reserve_module.recruitment, "get_clan_by_tag", lambda tag: clan_row
    )

    async def fake_count(tag):
        return 1

    monkeypatch.setattr(
        reserve_module.reservations,
        "count_active_reservations_for_clan",
        fake_count,
    )

    async def fake_find_active(*_args, **_kwargs):
        return []

    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        fake_find_active,
    )

    monkeypatch.setattr(
        reserve_module.reservations,
        "get_active_reservations_for_clan",
        lambda *_args, **_kwargs: asyncio.sleep(
            0,
            result=[
                reservations_sheet.ReservationRow(
                    row_number=2,
                    thread_id="old",
                    ticket_user_id=999,
                    recruiter_id=444,
                    clan_tag="#DEF",
                    reserved_until=None,
                    created_at=None,
                    status=reserve_module.ACTIVE_STATUS,
                    notes="",
                    username_snapshot="Existing",
                    raw=(),
                )
            ],
        ),
    )

    appended: list[list[str]] = []

    async def fake_append(row_values):
        appended.append(list(row_values))

    monkeypatch.setattr(
        reserve_module.reservations, "append_reservation_row", fake_append
    )
    monkeypatch.setattr(
        reserve_module.availability,
        "recompute_clan_availability",
        lambda tag, **kwargs: asyncio.sleep(0, result=_verified_recompute_result(tag)),
    )
    monkeypatch.setattr(
        reserve_module,
        "_ensure_fresh_clans_for_reservations",
        lambda **_: asyncio.sleep(0, result=True),
    )

    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "DEF"))

    assert thread.sent, "expected the reservation flow to prompt for context"
    if appended:
        assert appended[0][7] == "Because they confirmed a start date"


def test_reserve_feature_disabled(monkeypatch):
    _enable_feature(monkeypatch, enabled=False)
    _setup_parents(monkeypatch, parent_id=1001)
    _setup_permissions(monkeypatch, recruiter=True)

    recruit = FakeMember(555)
    guild = FakeGuild([recruit])
    thread = FakeThread(thread_id=777, parent_id=1001)
    author = FakeMember(556)

    bot = FakeBot([])
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)

    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "XYZ"))

    assert ctx.replies, "should reply when feature disabled"
    assert "disabled" in ctx.replies[0].content


def test_reserve_permission_denied(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_parents(monkeypatch, parent_id=42)
    _setup_permissions(monkeypatch, recruiter=False, admin=False)

    recruit = FakeMember(777)
    guild = FakeGuild([recruit])
    thread = FakeThread(thread_id=888, parent_id=42)
    author = FakeMember(778)

    bot = FakeBot([])
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)

    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "JKL"))

    assert ctx.replies, "should reply when user lacks permissions"
    assert "Only Recruiters" in ctx.replies[0].content


def test_reserve_requires_ticket_thread(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_parents(monkeypatch, parent_id=500)
    _setup_permissions(monkeypatch, recruiter=True)

    recruit = FakeMember(888)
    guild = FakeGuild([recruit])
    thread = FakeThread(
        thread_id=999, parent_id=123
    )  # parent does not match configured id
    author = FakeMember(889)

    bot = FakeBot([])
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)

    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "MNO"))

    assert ctx.replies, "should reply when outside ticket thread"
    assert "ticket thread" in ctx.replies[0].content


def test_reservations_thread_no_matches(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_parents(monkeypatch, parent_id=600)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch)

    recruit = FakeMember(900, "Recruit Thread")
    guild = FakeGuild([recruit])
    thread = FakeThread(
        thread_id=1000,
        parent_id=600,
        name="W0455-Recruit Thread",
        owner_id=recruit.id,
    )
    author = FakeMember(901)

    bot = FakeBot([], channels={thread.id: thread})
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)

    async def fake_lookup(*_, **__):
        return []

    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        fake_lookup,
    )

    logs: list[str] = []

    def fake_log(level: str, message: str, **_):
        logs.append(message)

    monkeypatch.setattr(reserve_module.human_log, "human", fake_log)

    cog = _make_cog(bot)
    asyncio.run(cog.reservations_command.callback(cog, ctx))

    assert thread.sent and "No active reservations" in thread.sent[0].content
    assert (
        logs and "thread=W0455-Recruit Thread" in logs[0] and "result=empty" in logs[0]
    )


def test_reservations_thread_lists_matches(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_parents(monkeypatch, parent_id=601)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch)

    recruit = FakeMember(910, "Thread User")
    guild = FakeGuild([recruit])
    thread = FakeThread(
        thread_id=1100,
        parent_id=601,
        name="W0460-Thread User",
        owner_id=recruit.id,
    )
    author = FakeMember(911)

    rows = [
        _reservation_row(
            2,
            clan_tag="#ABC",
            reserved_until=dt.date(2025, 11, 18),
            thread_id=thread.id,
            ticket_user_id=recruit.id,
            recruiter_id=author.id,
            username_snapshot="Thread User",
        )
    ]

    async def fake_lookup(*_, **__):
        return list(rows)

    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        fake_lookup,
    )

    logs: list[str] = []

    def fake_log(level: str, message: str, **_):
        logs.append(message)

    monkeypatch.setattr(reserve_module.human_log, "human", fake_log)

    bot = FakeBot([], channels={thread.id: thread})
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)

    cog = _make_cog(bot)
    asyncio.run(cog.reservations_command.callback(cog, ctx))

    assert thread.sent, "expected reservation listing"
    content = thread.sent[0].content
    lines = content.splitlines()
    assert lines[0].startswith("Active reservation for")
    assert "`ABC`" in lines[1]
    assert any(
        "thread=W0460-Thread User" in entry and "result=ok" in entry for entry in logs
    )


def test_reservations_thread_mismatch(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_parents(monkeypatch, parent_id=602)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch)

    recruit = FakeMember(930, "Mismatch User")
    guild = FakeGuild([recruit])
    thread = FakeThread(
        thread_id=1150,
        parent_id=602,
        name="Res-W0470-Mismatch User-C1CE",
        owner_id=recruit.id,
    )
    author = FakeMember(931)

    row = _reservation_row(
        4,
        clan_tag="#C1CM",
        reserved_until=dt.date(2025, 11, 25),
        thread_id=thread.id,
        ticket_user_id=recruit.id,
        recruiter_id=author.id,
        username_snapshot="Mismatch User",
    )

    async def fake_lookup(*_, **__):
        return [row]

    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        fake_lookup,
    )

    logs: list[str] = []

    def fake_log(level: str, message: str, **_):
        logs.append(message)

    monkeypatch.setattr(reserve_module.human_log, "human", fake_log)

    bot = FakeBot([], channels={thread.id: thread})
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)

    cog = _make_cog(bot)
    asyncio.run(cog.reservations_command.callback(cog, ctx))

    assert thread.sent and "ledger shows" in thread.sent[0].content
    assert any(
        "result=error" in entry and "reason=tag_mismatch" in entry for entry in logs
    )


def test_reservations_thread_multiple_rows(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_parents(monkeypatch, parent_id=603)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch)

    recruit = FakeMember(940, "Multi User")
    guild = FakeGuild([recruit])
    thread = FakeThread(
        thread_id=1160,
        parent_id=603,
        name="W0475-Multi User",
        owner_id=recruit.id,
    )
    author = FakeMember(941)

    rows = [
        _reservation_row(
            5,
            clan_tag="#AAA",
            reserved_until=dt.date(2025, 11, 26),
            thread_id=thread.id,
            ticket_user_id=recruit.id,
            recruiter_id=author.id,
        ),
        _reservation_row(
            6,
            clan_tag="#BBB",
            reserved_until=dt.date(2025, 11, 27),
            thread_id=thread.id,
            ticket_user_id=recruit.id,
            recruiter_id=author.id,
        ),
    ]

    async def fake_lookup(*_, **__):
        return rows

    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        fake_lookup,
    )

    logs: list[str] = []

    def fake_log(level: str, message: str, **_):
        logs.append(message)

    monkeypatch.setattr(reserve_module.human_log, "human", fake_log)

    bot = FakeBot([], channels={thread.id: thread})
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)

    cog = _make_cog(bot)
    asyncio.run(cog.reservations_command.callback(cog, ctx))

    assert thread.sent and "multiple active reservations" in thread.sent[0].content
    assert any("reason=multiple_active" in entry for entry in logs)


def test_reservations_global_listing_recent(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_permissions(monkeypatch, recruiter=True)
    control_thread_id = 2000
    _setup_control_channels(monkeypatch, recruiters_thread=control_thread_id)

    guild = FakeGuild([])
    channel = FakeThread(
        thread_id=control_thread_id,
        parent_id=0,
        name="recruiters-control",
        owner_id=None,
        guild=guild,
    )
    author = FakeMember(955)

    class _FixedDateTime(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            base = dt.datetime(2025, 11, 30, tzinfo=dt.timezone.utc)
            if tz is None:
                return base
            return base.astimezone(tz)

    monkeypatch.setattr(reserve_module.dt, "datetime", _FixedDateTime)

    recent_row = _reservation_row(
        7,
        clan_tag="C1CE",
        reserved_until=dt.date(2025, 12, 15),
        thread_id=3100,
        ticket_user_id=author.id,
        username_snapshot="Recent Recruit",
        created_at=dt.datetime(2025, 11, 20, tzinfo=dt.timezone.utc),
    )
    old_row = _reservation_row(
        8,
        clan_tag="C1CM",
        reserved_until=dt.date(2025, 10, 10),
        thread_id=3200,
        ticket_user_id=None,
        username_snapshot="Old Recruit",
        created_at=dt.datetime(2025, 9, 10, tzinfo=dt.timezone.utc),
        status="released",
    )

    ledger = _reservation_ledger([recent_row, old_row])

    async def fake_ledger():
        return ledger

    monkeypatch.setattr(
        reserve_module.reservations,
        "load_reservation_ledger",
        fake_ledger,
    )

    thread_lookup = {
        3100: FakeThread(
            3100, parent_id=0, name="W0500-Recent Recruit-C1CE", owner_id=None
        ),
        3200: FakeThread(
            3200, parent_id=0, name="W0499-Old Recruit-C1CM", owner_id=None
        ),
    }

    async def fake_resolve(bot, thread_id):
        return thread_lookup.get(int(thread_id))

    monkeypatch.setattr(reserve_module, "_resolve_thread", fake_resolve)

    logs: list[str] = []

    def fake_log(level: str, message: str, **_):
        logs.append(message)

    monkeypatch.setattr(reserve_module.human_log, "human", fake_log)

    bot = FakeBot([], channels={channel.id: channel})
    ctx = FakeContext(bot, guild=guild, channel=channel, author=author)

    cog = _make_cog(bot)
    asyncio.run(cog.reservations_command.callback(cog, ctx))

    assert channel.sent, "expected global listing output"
    content = "\n".join(message.content or "" for message in channel.sent)
    assert "Recent Recruit" in content and "Old Recruit" not in content
    assert "Reservations in the last 28 days" in channel.sent[0].content
    assert any("reservations_global" in entry and "count=1" in entry for entry in logs)


def test_reservations_clan_listing(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_parents(monkeypatch, parent_id=700)
    interact_channel_id = 1200
    _setup_control_channels(monkeypatch, interact_channel=interact_channel_id)

    recruit = FakeMember(920, "User One")
    guild = FakeGuild([recruit])
    channel = FakeThread(
        thread_id=interact_channel_id,
        parent_id=0,
        name="recruitment-interact",
        owner_id=None,
        guild=guild,
    )
    author = FakeMember(921)

    clan_row = ["", "Clan", "C1CE", ""] + [""] * 10

    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (5, clan_row)
    )

    rows = [
        _reservation_row(
            5,
            clan_tag="C1CE",
            reserved_until=dt.date(2025, 11, 21),
            thread_id=1300,
            ticket_user_id=recruit.id,
            username_snapshot="User One",
        ),
        _reservation_row(
            6,
            clan_tag="C1CE",
            reserved_until=dt.date(2025, 11, 22),
            thread_id=1400,
            ticket_user_id=None,
            username_snapshot="User Two",
        ),
    ]

    async def fake_clan(tag):
        assert tag == "C1CE"
        return list(rows)

    monkeypatch.setattr(
        reserve_module.reservations,
        "get_active_reservations_for_clan",
        fake_clan,
    )

    thread_lookup = {
        1300: FakeThread(
            1300, parent_id=700, name="W0471-User One-C1CE", owner_id=recruit.id
        ),
        1400: FakeThread(
            1400, parent_id=700, name="W0472-User Two-C1CE", owner_id=None
        ),
    }

    logs: list[str] = []

    def fake_log(level: str, message: str, **_):
        logs.append(message)

    monkeypatch.setattr(reserve_module.human_log, "human", fake_log)

    bot = FakeBot([], channels=thread_lookup | {channel.id: channel})
    ctx = FakeContext(bot, guild=guild, channel=channel, author=author)

    cog = _make_cog(bot)
    asyncio.run(cog.reservations_command.callback(cog, ctx, "C1CE"))

    assert channel.sent, "expected clan reservation listing"
    content = channel.sent[0].content
    assert "ticket W0471" in content
    assert "ticket unknown" not in content.splitlines()[1]
    assert any("scope=clan" in entry and "result=ok" in entry for entry in logs)


def test_reservations_clan_listing_requires_channel(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch, interact_channel=5555)

    guild = FakeGuild([])
    channel = FakeThread(
        thread_id=4444, parent_id=0, name="general", owner_id=None, guild=guild
    )
    author = FakeMember(950)

    bot = FakeBot([], channels={channel.id: channel})
    ctx = FakeContext(bot, guild=guild, channel=channel, author=author)

    cog = _make_cog(bot)
    asyncio.run(cog.reservations_command.callback(cog, ctx, "C1CE"))

    assert ctx.replies, "expected channel warning"
    assert "Clan-level reservation lookups" in ctx.replies[0].content


def test_reservations_clan_listing_allows_lead(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_permissions(monkeypatch, recruiter=False)
    interact_channel_id = 7777
    _setup_control_channels(monkeypatch, interact_channel=interact_channel_id)

    author = FakeMember(960)
    monkeypatch.setattr(reserve_module, "get_clan_lead_ids", lambda: {author.id})

    clan_row = ["", "Clan", "C1CM", ""] + [""] * 10
    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (7, clan_row)
    )

    rows = [
        _reservation_row(
            9,
            clan_tag="C1CM",
            reserved_until=dt.date(2025, 11, 28),
            thread_id=1500,
            ticket_user_id=None,
            username_snapshot="Lead Recruit",
        )
    ]

    async def fake_clan(tag):
        return list(rows)

    monkeypatch.setattr(
        reserve_module.reservations,
        "get_active_reservations_for_clan",
        fake_clan,
    )

    logs: list[str] = []

    def fake_log(level: str, message: str, **_):
        logs.append(message)

    monkeypatch.setattr(reserve_module.human_log, "human", fake_log)

    guild = FakeGuild([])
    channel = FakeThread(
        thread_id=interact_channel_id,
        parent_id=0,
        name="recruitment-interact",
        owner_id=None,
        guild=guild,
    )

    bot = FakeBot([], channels={channel.id: channel})
    ctx = FakeContext(bot, guild=guild, channel=channel, author=author)

    cog = _make_cog(bot)
    asyncio.run(cog.reservations_command.callback(cog, ctx, "C1CM"))

    assert channel.sent and "Lead Recruit" in channel.sent[0].content
    assert any("scope=clan" in entry and "result=ok" in entry for entry in logs)


def test_reservations_clan_listing_denies_non_lead(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_permissions(monkeypatch, recruiter=False)
    interact_channel_id = 8888
    _setup_control_channels(monkeypatch, interact_channel=interact_channel_id)
    monkeypatch.setattr(reserve_module, "get_clan_lead_ids", lambda: set())

    guild = FakeGuild([])
    channel = FakeThread(
        thread_id=interact_channel_id,
        parent_id=0,
        name="recruitment-interact",
        owner_id=None,
        guild=guild,
    )
    author = FakeMember(970)

    bot = FakeBot([])
    ctx = FakeContext(bot, guild=guild, channel=channel, author=author)

    cog = _make_cog(bot)
    asyncio.run(cog.reservations_command.callback(cog, ctx, "C1CM"))

    assert ctx.replies, "expected permission warning"
    assert "Only Recruiters (or Admins)" in ctx.replies[-1].content


def test_reserve_release_global_success(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_permissions(monkeypatch, recruiter=True)
    control_thread_id = 2500
    _setup_control_channels(monkeypatch, recruiters_thread=control_thread_id)

    recruit = FakeMember(930, "Release User")
    guild = FakeGuild([recruit])
    channel = FakeThread(
        thread_id=control_thread_id,
        parent_id=0,
        name="recruiters-control",
        owner_id=None,
        guild=guild,
    )
    author = FakeMember(931)

    row = _reservation_row(
        8,
        clan_tag="C1CE",
        reserved_until=dt.date(2025, 11, 23),
        thread_id=9999,
        ticket_user_id=recruit.id,
        recruiter_id=author.id,
        username_snapshot="Release User",
    )

    async def fake_lookup(*_, **__):
        return [row]

    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        fake_lookup,
    )

    updated: list[tuple[int, str]] = []

    async def fake_status(row_number: int, status: str):
        updated.append((row_number, status))

    monkeypatch.setattr(
        reserve_module.reservations,
        "update_reservation_status",
        fake_status,
    )

    adjustments: list[tuple[str, int]] = []

    async def fake_adjust(tag: str, delta: int):
        adjustments.append((tag, delta))

    monkeypatch.setattr(
        reserve_module.availability, "adjust_manual_open_spots", fake_adjust
    )

    recomputed: list[str] = []

    async def fake_recompute(tag: str, *, guild=None):
        recomputed.append(tag)

    monkeypatch.setattr(
        reserve_module.availability, "recompute_clan_availability", fake_recompute
    )

    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (9, ["", "", tag, ""])
    )

    logs: list[str] = []

    def fake_log(level: str, message: str, **_):
        logs.append(message)

    monkeypatch.setattr(reserve_module.human_log, "human", fake_log)

    bot = FakeBot([])
    ctx = FakeContext(bot, guild=guild, channel=channel, author=author)

    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "release", f"<@{recruit.id}>", "C1CE"))

    assert updated == [(8, "released")]
    assert adjustments == [("C1CE", 1)]
    assert recomputed == ["C1CE"]
    assert channel.sent and "Released the reserved seat" in channel.sent[-1].content
    assert any("source=global" in entry and "result=ok" in entry for entry in logs)


def test_reserve_release_allowed_outside_control(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch, recruiters_thread=3333)

    recruit = FakeMember(940, "Release User")
    guild = FakeGuild([recruit])
    thread = FakeThread(
        thread_id=8000, parent_id=600, name="W0001-Test", owner_id=recruit.id
    )
    author = FakeMember(941)

    row = _reservation_row(
        18,
        clan_tag="C1CE",
        reserved_until=dt.date(2025, 11, 24),
        thread_id=thread.id,
        ticket_user_id=recruit.id,
        recruiter_id=author.id,
        username_snapshot="Release User",
    )

    async def fake_lookup(*_, **__):
        return [row]

    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        fake_lookup,
    )

    updated: list[tuple[int, str]] = []

    async def fake_status(row_number: int, status: str):
        updated.append((row_number, status))

    monkeypatch.setattr(
        reserve_module.reservations,
        "update_reservation_status",
        fake_status,
    )

    adjustments: list[tuple[str, int]] = []

    async def fake_adjust(tag: str, delta: int):
        adjustments.append((tag, delta))

    monkeypatch.setattr(
        reserve_module.availability, "adjust_manual_open_spots", fake_adjust
    )

    recomputed: list[str] = []

    async def fake_recompute(tag: str, *, guild=None):
        recomputed.append(tag)

    monkeypatch.setattr(
        reserve_module.availability, "recompute_clan_availability", fake_recompute
    )

    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (9, ["", "", tag, ""])
    )

    logs: list[str] = []

    def fake_log(level: str, message: str, **_):
        logs.append(message)

    monkeypatch.setattr(reserve_module.human_log, "human", fake_log)

    bot = FakeBot([])
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)

    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "release", f"<@{recruit.id}>", "C1CE"))

    assert updated == [(18, "released")]
    assert adjustments == [("C1CE", 1)]
    assert recomputed == ["C1CE"]
    assert thread.sent and "Released the reserved seat" in thread.sent[-1].content
    assert any("source=global" in entry and "result=ok" in entry for entry in logs)


def test_reserve_release_global_not_found(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_permissions(monkeypatch, recruiter=True)
    control_thread_id = 2501
    _setup_control_channels(monkeypatch, recruiters_thread=control_thread_id)

    recruit = FakeMember(931, "Release Missing")
    guild = FakeGuild([recruit])
    channel = FakeThread(
        thread_id=control_thread_id,
        parent_id=0,
        name="recruiters-control",
        owner_id=None,
        guild=guild,
    )
    author = FakeMember(932)

    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (9, ["", "", tag, ""])
    )

    async def fake_lookup(*_, **__):
        return []

    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        fake_lookup,
    )

    logs: list[str] = []

    def fake_log(level: str, message: str, **_):
        logs.append(message)

    monkeypatch.setattr(reserve_module.human_log, "human", fake_log)

    bot = FakeBot([])
    ctx = FakeContext(bot, guild=guild, channel=channel, author=author)

    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "release", f"<@{recruit.id}>", "C1CE"))

    assert ctx.replies, "expected not found reply"
    assert "No active reservation" in ctx.replies[-1].content
    assert any("result=not_found" in entry for entry in logs)


def test_reserve_release_global_multiple_rows(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_permissions(monkeypatch, recruiter=True)
    control_thread_id = 2502
    _setup_control_channels(monkeypatch, recruiters_thread=control_thread_id)

    recruit = FakeMember(932, "Release Multi")
    guild = FakeGuild([recruit])
    channel = FakeThread(
        control_thread_id,
        parent_id=0,
        name="recruiters-control",
        owner_id=None,
        guild=guild,
    )
    author = FakeMember(933)

    rows = [
        _reservation_row(
            12,
            clan_tag="C1CE",
            reserved_until=dt.date(2025, 12, 1),
            thread_id=999,
            ticket_user_id=recruit.id,
            username_snapshot="Release Multi",
        ),
        _reservation_row(
            13,
            clan_tag="C1CE",
            reserved_until=dt.date(2025, 12, 2),
            thread_id=998,
            ticket_user_id=recruit.id,
            username_snapshot="Release Multi",
        ),
    ]

    async def fake_lookup(*_, **__):
        return list(rows)

    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        fake_lookup,
    )

    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (9, ["", "", tag, ""])
    )

    logs: list[str] = []

    def fake_log(level: str, message: str, **_):
        logs.append(message)

    monkeypatch.setattr(reserve_module.human_log, "human", fake_log)

    bot = FakeBot([])
    ctx = FakeContext(bot, guild=guild, channel=channel, author=author)

    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "release", f"<@{recruit.id}>", "C1CE"))

    assert ctx.replies and "Multiple reservations" in ctx.replies[-1].content
    assert any("reason=multiple_rows" in entry for entry in logs)


def test_reserve_extend_allowed_outside_control(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch, recruiters_thread=3333)

    recruit = FakeMember(960, "Extend User")
    guild = FakeGuild([recruit])
    thread = FakeThread(
        thread_id=9000, parent_id=600, name="W0002-Test", owner_id=recruit.id
    )
    author = FakeMember(961)

    row = _reservation_row(
        19,
        clan_tag="C1CE",
        reserved_until=dt.date(2025, 11, 25),
        thread_id=thread.id,
        ticket_user_id=recruit.id,
        recruiter_id=author.id,
        username_snapshot="Extend User",
    )

    async def fake_lookup(*_, **__):
        return [row]

    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        fake_lookup,
    )

    updated: list[tuple[int, dt.date]] = []

    async def fake_extend(row_number: int, new_date: dt.date):
        updated.append((row_number, new_date))

    monkeypatch.setattr(
        reserve_module.reservations,
        "update_reservation_expiry",
        fake_extend,
    )

    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (9, ["", "", tag, ""])
    )

    logs: list[str] = []

    def fake_log(level: str, message: str, **_):
        logs.append(message)

    monkeypatch.setattr(reserve_module.human_log, "human", fake_log)

    bot = FakeBot([])
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)

    cog = _make_cog(bot)
    asyncio.run(
        cog.reserve.callback(
            cog, ctx, "extend", f"<@{recruit.id}>", "C1CE", "2999-01-01"
        )
    )

    assert updated == [(19, dt.date(2999, 1, 1))]
    assert thread.sent and "Extended the reservation" in thread.sent[-1].content
    assert any("source=global" in entry and "result=ok" in entry for entry in logs)


def test_reserve_extend_global_success(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_permissions(monkeypatch, recruiter=True)
    control_thread_id = 2600
    _setup_control_channels(monkeypatch, recruiters_thread=control_thread_id)

    recruit = FakeMember(970, "Extend User")
    guild = FakeGuild([recruit])
    channel = FakeThread(
        control_thread_id,
        parent_id=0,
        name="recruiters-control",
        owner_id=None,
        guild=guild,
    )
    author = FakeMember(971)

    row = _reservation_row(
        14,
        clan_tag="C1CE",
        reserved_until=dt.date(2025, 11, 25),
        thread_id=9999,
        ticket_user_id=recruit.id,
        recruiter_id=author.id,
        username_snapshot="Extend User",
    )

    async def fake_lookup(*_, **__):
        return [row]

    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        fake_lookup,
    )

    updates: list[tuple[int, dt.date]] = []

    async def fake_expiry(row_number: int, new_date: dt.date):
        updates.append((row_number, new_date))

    monkeypatch.setattr(
        reserve_module.reservations,
        "update_reservation_expiry",
        fake_expiry,
    )

    logs: list[str] = []

    def fake_log(level: str, message: str, **_):
        logs.append(message)

    monkeypatch.setattr(reserve_module.human_log, "human", fake_log)
    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (9, ["", "", tag, ""])
    )

    bot = FakeBot([])
    ctx = FakeContext(bot, guild=guild, channel=channel, author=author)

    cog = _make_cog(bot)
    asyncio.run(
        cog.reserve.callback(
            cog, ctx, "extend", f"<@{recruit.id}>", "C1CE", "2999-01-01"
        )
    )

    assert updates == [(14, dt.date(2999, 1, 1))]
    assert channel.sent and "Extended the reservation" in channel.sent[-1].content
    assert any("reservation_extend" in entry and "result=ok" in entry for entry in logs)


def test_reserve_extend_global_invalid_date(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_permissions(monkeypatch, recruiter=True)
    control_thread_id = 2601
    _setup_control_channels(monkeypatch, recruiters_thread=control_thread_id)

    recruit = FakeMember(980, "Extend Fail")
    guild = FakeGuild([recruit])
    channel = FakeThread(
        control_thread_id,
        parent_id=0,
        name="recruiters-control",
        owner_id=None,
        guild=guild,
    )
    author = FakeMember(981)

    row = _reservation_row(
        15,
        clan_tag="C1CE",
        reserved_until=dt.date(2025, 11, 30),
        thread_id=9998,
        ticket_user_id=recruit.id,
        recruiter_id=author.id,
        username_snapshot="Extend Fail",
    )

    async def fake_lookup(*_, **__):
        return [row]

    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        fake_lookup,
    )

    logs: list[str] = []

    def fake_log(level: str, message: str, **_):
        logs.append(message)

    monkeypatch.setattr(reserve_module.human_log, "human", fake_log)
    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (9, ["", "", tag, ""])
    )

    bot = FakeBot([])
    ctx = FakeContext(bot, guild=guild, channel=channel, author=author)

    cog = _make_cog(bot)
    asyncio.run(
        cog.reserve.callback(
            cog, ctx, "extend", f"<@{recruit.id}>", "C1CE", "2020-01-01"
        )
    )

    assert ctx.replies, "expected invalid date message"
    assert "valid date" in ctx.replies[-1].content
    assert any("invalid_date" in entry for entry in logs)


def test_reserve_extend_global_not_found(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_permissions(monkeypatch, recruiter=True)
    control_thread_id = 2602
    _setup_control_channels(monkeypatch, recruiters_thread=control_thread_id)

    recruit = FakeMember(981, "Extend Missing")
    guild = FakeGuild([recruit])
    channel = FakeThread(
        control_thread_id,
        parent_id=0,
        name="recruiters-control",
        owner_id=None,
        guild=guild,
    )
    author = FakeMember(982)

    async def fake_lookup(*_, **__):
        return []

    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        fake_lookup,
    )

    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (9, ["", "", tag, ""])
    )

    logs: list[str] = []

    def fake_log(level: str, message: str, **_):
        logs.append(message)

    monkeypatch.setattr(reserve_module.human_log, "human", fake_log)

    bot = FakeBot([])
    ctx = FakeContext(bot, guild=guild, channel=channel, author=author)

    cog = _make_cog(bot)
    asyncio.run(
        cog.reserve.callback(
            cog, ctx, "extend", f"<@{recruit.id}>", "C1CE", "2030-01-01"
        )
    )

    assert ctx.replies and "No active reservation" in ctx.replies[-1].content
    assert any("result=not_found" in entry for entry in logs)


def test_reserve_extend_global_multiple_rows(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_permissions(monkeypatch, recruiter=True)
    control_thread_id = 2603
    _setup_control_channels(monkeypatch, recruiters_thread=control_thread_id)

    recruit = FakeMember(982, "Extend Multi")
    guild = FakeGuild([recruit])
    channel = FakeThread(
        control_thread_id,
        parent_id=0,
        name="recruiters-control",
        owner_id=None,
        guild=guild,
    )
    author = FakeMember(983)

    rows = [
        _reservation_row(
            16,
            clan_tag="C1CE",
            reserved_until=dt.date(2025, 11, 24),
            thread_id=5000,
            ticket_user_id=recruit.id,
            username_snapshot="Extend Multi",
        ),
        _reservation_row(
            17,
            clan_tag="C1CE",
            reserved_until=dt.date(2025, 11, 26),
            thread_id=5001,
            ticket_user_id=recruit.id,
            username_snapshot="Extend Multi",
        ),
    ]

    async def fake_lookup(*_, **__):
        return list(rows)

    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        fake_lookup,
    )

    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (9, ["", "", tag, ""])
    )

    logs: list[str] = []

    def fake_log(level: str, message: str, **_):
        logs.append(message)

    monkeypatch.setattr(reserve_module.human_log, "human", fake_log)

    bot = FakeBot([])
    ctx = FakeContext(bot, guild=guild, channel=channel, author=author)

    cog = _make_cog(bot)
    asyncio.run(
        cog.reserve.callback(
            cog, ctx, "extend", f"<@{recruit.id}>", "C1CE", "2030-01-01"
        )
    )

    assert ctx.replies and "Multiple reservations" in ctx.replies[-1].content
    assert any("reason=multiple_rows" in entry for entry in logs)


def test_reservation_preflight_failure_does_not_append(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_parents(monkeypatch, parent_id=999)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch)

    recruit = FakeMember(333, "Recruit Blocked")
    guild = FakeGuild([recruit])
    thread = FakeThread(thread_id=557, parent_id=999)
    author = FakeMember(112, "Recruiter")
    bot = FakeBot([])
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)

    appended: list[list[str]] = []

    async def failing_preflight(_tag, *, delta=0):
        raise ValueError("configured bot_info header not found for open_spots: missing")

    monkeypatch.setattr(
        reserve_module.availability,
        "preflight_clan_availability_update",
        failing_preflight,
    )
    monkeypatch.setattr(
        reserve_module.reservations,
        "append_reservation_row",
        lambda row: appended.append(row),
    )

    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "ABC"))

    assert appended == []
    assert any(
        "no reservation was created" in (reply.content or "") for reply in ctx.replies
    )


def _use_configured_ak_clan_tag(monkeypatch):
    header = [""] * 37
    header[4] = "manual_open_spots"
    header[31] = "open_spots"
    header[32] = "inactives"
    header[33] = "reservation_count"
    header[34] = "reservation_summary"
    header[35] = "manual_open_spots_seen"
    header[36] = "clan_tag"
    row = [""] * 37
    row[4] = "3"
    row[31] = "3"
    row[32] = "0"
    row[33] = "0"
    row[35] = "3"
    row[36] = "C1CE"
    config = {
        f"clans_header_{field}": field
        for field in reserve_module.availability.AVAILABILITY_FIELDS
    }
    monkeypatch.setattr(
        reserve_module.recruitment, "get_clans_tab_name", lambda: "bot_info"
    )
    monkeypatch.setattr(
        reserve_module.recruitment, "get_recruitment_sheet_id", lambda: "sheet"
    )
    monkeypatch.setattr(
        reserve_module.recruitment, "get_clan_header_row", lambda force=False: header
    )
    monkeypatch.setattr(
        reserve_module.recruitment,
        "get_config_value",
        lambda key, default=None: config.get(key, default),
    )
    monkeypatch.setattr(
        reserve_module.recruitment, "fetch_clans", lambda force=False: [list(row)]
    )
    monkeypatch.setattr(
        reserve_module.recruitment,
        "find_clan_row",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("column C lookup should not be used")
        ),
    )
    monkeypatch.setattr(
        reserve_module,
        "_resolve_configured_reservation_clan_tag",
        reserve_module.availability.resolve_configured_clan_tag,
    )


def test_reserve_release_uses_configured_clan_tag_header_not_column_c(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch, recruiters_thread=2500)
    _use_configured_ak_clan_tag(monkeypatch)

    recruit = FakeMember(3001, "Release AK")
    guild = FakeGuild([recruit])
    channel = FakeThread(2500, parent_id=0, name="recruiters-control", guild=guild)
    author = FakeMember(3002)
    row = _reservation_row(21, clan_tag="C1CE", ticket_user_id=recruit.id)
    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=[row]),
    )
    updated: list[tuple[int, str]] = []
    monkeypatch.setattr(
        reserve_module.reservations,
        "update_reservation_status",
        lambda row_number, status: asyncio.sleep(
            0, result=updated.append((row_number, status))
        ),
    )
    monkeypatch.setattr(
        reserve_module.availability,
        "adjust_manual_open_spots",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        reserve_module.availability,
        "recompute_clan_availability",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        reserve_module.human_log, "human", lambda *_args, **_kwargs: None
    )

    bot = FakeBot([])
    ctx = FakeContext(bot, guild=guild, channel=channel, author=author)
    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "release", f"<@{recruit.id}>", "C1CE"))

    assert updated == [(21, "released")]
    assert channel.sent and "Released the reserved seat" in channel.sent[-1].content


def test_reserve_extend_uses_configured_clan_tag_header_not_column_c(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch, recruiters_thread=2600)
    _use_configured_ak_clan_tag(monkeypatch)

    recruit = FakeMember(3011, "Extend AK")
    guild = FakeGuild([recruit])
    channel = FakeThread(2600, parent_id=0, name="recruiters-control", guild=guild)
    author = FakeMember(3012)
    row = _reservation_row(22, clan_tag="C1CE", ticket_user_id=recruit.id)
    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=[row]),
    )
    updated: list[tuple[int, dt.date]] = []
    monkeypatch.setattr(
        reserve_module.reservations,
        "update_reservation_expiry",
        lambda row_number, new_date: asyncio.sleep(
            0, result=updated.append((row_number, new_date))
        ),
    )
    monkeypatch.setattr(
        reserve_module.human_log, "human", lambda *_args, **_kwargs: None
    )

    bot = FakeBot([])
    ctx = FakeContext(bot, guild=guild, channel=channel, author=author)
    cog = _make_cog(bot)
    asyncio.run(
        cog.reserve.callback(
            cog, ctx, "extend", f"<@{recruit.id}>", "C1CE", "2999-01-01"
        )
    )

    assert updated == [(22, dt.date(2999, 1, 1))]
    assert channel.sent and "Extended the reservation" in channel.sent[-1].content


def test_reservations_clan_listing_uses_configured_clan_tag_header_not_column_c(
    monkeypatch,
):
    _enable_feature(monkeypatch, enabled=True)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_parents(monkeypatch, parent_id=700)
    _setup_control_channels(monkeypatch, interact_channel=1200)
    _use_configured_ak_clan_tag(monkeypatch)

    recruit = FakeMember(3021, "List AK")
    guild = FakeGuild([recruit])
    channel = FakeThread(1200, parent_id=0, name="recruitment-interact", guild=guild)
    rows = [
        _reservation_row(
            23,
            clan_tag="C1CE",
            thread_id=1300,
            ticket_user_id=recruit.id,
            username_snapshot="List AK",
        )
    ]
    monkeypatch.setattr(
        reserve_module.reservations,
        "get_active_reservations_for_clan",
        lambda tag: asyncio.sleep(0, result=list(rows)),
    )
    monkeypatch.setattr(
        reserve_module.human_log, "human", lambda *_args, **_kwargs: None
    )
    bot = FakeBot(
        [],
        channels={
            1300: FakeThread(
                1300, parent_id=700, name="W3021-List AK-C1CE", owner_id=recruit.id
            ),
            1200: channel,
        },
    )
    ctx = FakeContext(bot, guild=guild, channel=channel, author=FakeMember(3022))

    cog = _make_cog(bot)
    asyncio.run(cog.reservations_command.callback(cog, ctx, "C1CE"))

    assert channel.sent, "expected clan reservation listing"
    assert "ticket W3021" in channel.sent[0].content


def _forbidden_error() -> discord.Forbidden:
    response = type("Response", (), {"status": 403, "reason": "Forbidden"})()
    return discord.Forbidden(response, "Missing Manage Messages")


def _not_found_error() -> discord.NotFound:
    response = type("Response", (), {"status": 404, "reason": "Not Found"})()
    return discord.NotFound(response, "Unknown Message")


def _setup_successful_reserve(
    monkeypatch, *, parent_id: int = 999, thread_name: str = "W0056-denbotron"
):
    _enable_feature(monkeypatch, enabled=True)
    _setup_parents(monkeypatch, parent_id=parent_id)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch)

    recruit = FakeMember(222, "denbotron")
    guild = FakeGuild([recruit])
    thread = FakeThread(thread_id=555, parent_id=parent_id, name=thread_name)
    author = FakeMember(111, "Recruiter")
    date_message = FakeMessage("2026-08-13", author=author, channel=thread)
    confirm_message = FakeMessage("yes", author=author, channel=thread)
    bot = FakeBot([date_message, confirm_message])
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)
    ctx.message.content = f"!reserve C1C5 <@{recruit.id}>"

    clan_row = ["", "Clan", "C1C5", "", "5"] + [""] * 40

    class SuccessPlan:
        sheet_row = 10
        row = tuple(clan_row)
        numeric_values = {"manual_open_spots": 5}
        headers = type("Headers", (), {"header_map": {"clan_tag": 2}})()

    monkeypatch.setattr(
        reserve_module.availability,
        "preflight_clan_availability_update",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=SuccessPlan()),
    )
    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (10, list(clan_row))
    )

    def fake_updated_row(tag):
        updated = list(clan_row)
        while len(updated) <= 33:
            updated.append("")
        updated[31] = "3"
        updated[33] = "2"
        return updated

    monkeypatch.setattr(reserve_module.recruitment, "get_clan_by_tag", fake_updated_row)
    monkeypatch.setattr(
        reserve_module.reservations,
        "count_active_reservations_for_clan",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=1),
    )
    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=[]),
    )
    monkeypatch.setattr(
        reserve_module.reservations,
        "append_reservation_row",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        reserve_module.availability,
        "recompute_clan_availability",
        lambda tag, **_kwargs: asyncio.sleep(0, result=_verified_recompute_result(tag)),
    )
    monkeypatch.setattr(
        reserve_module,
        "_ensure_fresh_clans_for_reservations",
        lambda **_: asyncio.sleep(0, result=True),
    )

    return _make_cog(bot), ctx, thread, (date_message, confirm_message)


def test_reserve_success_cleans_only_tracked_prompt_messages(monkeypatch, caplog):
    cog, ctx, thread, (date_message, confirm_message) = _setup_successful_reserve(
        monkeypatch
    )
    unrelated = FakeMessage("unrelated ticket note", author=ctx.author, channel=thread)

    with caplog.at_level(logging.INFO, logger=reserve_module.log.name):
        asyncio.run(cog.reserve.callback(cog, ctx, "C1C5", "<@222>"))

    assert ctx.message.deleted is True
    assert date_message.deleted is True
    assert confirm_message.deleted is True
    assert unrelated.deleted is False
    prompt_messages = [
        message
        for message in thread.sent
        if "Reserved 1 spot" not in (message.content or "")
    ]
    assert prompt_messages
    assert all(message.deleted for message in prompt_messages)
    final_messages = [
        message
        for message in thread.sent
        if (message.content or "").startswith("✅ Reserved 1 spot")
    ]
    assert len(final_messages) == 1
    assert final_messages[0].deleted is False
    assert thread.edited_names == ["Res-W0056-denbotron-C1C5"]
    cleanup_records = [
        record
        for record in caplog.records
        if record.message == "reservation_prompt_cleanup"
    ]
    assert cleanup_records
    assert getattr(cleanup_records[-1], "deleted_count") == 5
    assert getattr(cleanup_records[-1], "failed_count") == 0
    assert getattr(cleanup_records[-1], "result") == "ok"


def test_reserve_failure_does_not_cleanup_prompt_messages(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_parents(monkeypatch, parent_id=1000)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch)

    recruit = FakeMember(3000, "Duplicate User")
    guild = FakeGuild([recruit])
    thread = FakeThread(thread_id=2000, parent_id=1000, name="W0500-Duplicate")
    author = FakeMember(3001)
    date_message = FakeMessage("2026-12-01", author=author, channel=thread)
    confirm_message = FakeMessage("yes", author=author, channel=thread)
    bot = FakeBot([date_message, confirm_message])
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)

    clan_row = ["", "Clan", "ZZZ", "", "5"] + [""] * 40
    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (12, list(clan_row))
    )
    monkeypatch.setattr(
        reserve_module.recruitment, "get_clan_by_tag", lambda tag: clan_row
    )
    monkeypatch.setattr(
        reserve_module.reservations,
        "count_active_reservations_for_clan",
        lambda *_: asyncio.sleep(0, result=0),
    )
    existing_row = _reservation_row(
        99,
        clan_tag="OLD",
        reserved_until=dt.date(2026, 12, 30),
        thread_id=5555,
        ticket_user_id=recruit.id,
    )
    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=[existing_row]),
    )
    monkeypatch.setattr(
        reserve_module,
        "_resolve_thread",
        lambda *_args, **_kwargs: asyncio.sleep(
            0, result=FakeThread(5555, parent_id=1000, name="Res-W0499-Other-C1CM")
        ),
    )

    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "ZZZ", f"<@{recruit.id}>"))

    assert ctx.message.deleted is False
    assert date_message.deleted is False
    assert confirm_message.deleted is False
    assert all(not message.deleted for message in thread.sent)


def test_reserve_cancel_does_not_delete_unrelated_messages(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_parents(monkeypatch, parent_id=999)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch)

    recruit = FakeMember(222, "Recruit")
    guild = FakeGuild([recruit])
    thread = FakeThread(thread_id=555, parent_id=999, name="W0001-Recruit")
    author = FakeMember(111, "Recruiter")
    cancel_message = FakeMessage("cancel", author=author, channel=thread)
    unrelated = FakeMessage("do not delete", author=author, channel=thread)
    bot = FakeBot([cancel_message])
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)
    clan_row = ["", "Clan", "ABC", "", "3"] + [""] * 40
    monkeypatch.setattr(
        reserve_module.recruitment, "find_clan_row", lambda tag: (10, list(clan_row))
    )
    monkeypatch.setattr(
        reserve_module.reservations,
        "count_active_reservations_for_clan",
        lambda *_: asyncio.sleep(0, result=0),
    )

    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "ABC", f"<@{recruit.id}>"))

    assert unrelated.deleted is False
    assert ctx.message.deleted is False
    assert cancel_message.deleted is False


def test_reserve_cleanup_missing_manage_messages_logs_and_keeps_reservation(
    monkeypatch, caplog
):
    cog, ctx, thread, (date_message, confirm_message) = _setup_successful_reserve(
        monkeypatch
    )
    error = _forbidden_error()
    ctx.message.delete_error = error
    date_message.delete_error = error
    confirm_message.delete_error = error
    thread.delete_error_for_sent = error

    with caplog.at_level(logging.INFO, logger=reserve_module.log.name):
        asyncio.run(cog.reserve.callback(cog, ctx, "C1C5", "<@222>"))

    assert any(
        (message.content or "").startswith("✅ Reserved 1 spot")
        for message in thread.sent
    )
    cleanup_records = [
        record
        for record in caplog.records
        if record.message == "reservation_prompt_cleanup"
    ]
    assert cleanup_records
    assert getattr(cleanup_records[-1], "failed_count") == 5
    assert getattr(cleanup_records[-1], "result") == "partial_failure"


def test_reserve_cleanup_ignores_already_deleted_messages(monkeypatch, caplog):
    cog, ctx, _thread, (date_message, _confirm_message) = _setup_successful_reserve(
        monkeypatch
    )
    date_message.delete_error = _not_found_error()

    with caplog.at_level(logging.INFO, logger=reserve_module.log.name):
        asyncio.run(cog.reserve.callback(cog, ctx, "C1C5", "<@222>"))

    cleanup_records = [
        record
        for record in caplog.records
        if record.message == "reservation_prompt_cleanup"
    ]
    assert cleanup_records
    assert getattr(cleanup_records[-1], "failed_count") == 0
    assert getattr(cleanup_records[-1], "result") == "ok"


def test_reserve_change_flow_cleans_full_completed_chain(monkeypatch):
    _enable_feature(monkeypatch, enabled=True)
    _setup_parents(monkeypatch, parent_id=999)
    _setup_permissions(monkeypatch, recruiter=True)
    _setup_control_channels(monkeypatch)

    recruit = FakeMember(222, "Change Recruit")
    guild = FakeGuild([recruit])
    thread = FakeThread(thread_id=555, parent_id=999, name="W0057-ChangeRecruit")
    author = FakeMember(111, "Recruiter")
    date_message = FakeMessage("2026-08-13", author=author, channel=thread)
    change_message = FakeMessage("change", author=author, channel=thread)
    change_choice_message = FakeMessage("date", author=author, channel=thread)
    changed_date_message = FakeMessage("2026-08-14", author=author, channel=thread)
    confirm_message = FakeMessage("yes", author=author, channel=thread)
    bot = FakeBot(
        [
            date_message,
            change_message,
            change_choice_message,
            changed_date_message,
            confirm_message,
        ]
    )
    ctx = FakeContext(bot, guild=guild, channel=thread, author=author)
    ctx.message.content = f"!reserve C1C5 <@{recruit.id}>"

    clan_row = ["", "Clan", "C1C5", "", "5"] + [""] * 40

    class SuccessPlan:
        sheet_row = 10
        row = tuple(clan_row)
        numeric_values = {"manual_open_spots": 5}
        headers = type("Headers", (), {"header_map": {"clan_tag": 2}})()

    monkeypatch.setattr(
        reserve_module.availability,
        "preflight_clan_availability_update",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=SuccessPlan()),
    )
    monkeypatch.setattr(
        reserve_module.recruitment, "get_clan_by_tag", lambda _tag: clan_row
    )
    monkeypatch.setattr(
        reserve_module.reservations,
        "count_active_reservations_for_clan",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=0),
    )
    monkeypatch.setattr(
        reserve_module.reservations,
        "find_active_reservations_for_recruit",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=[]),
    )
    monkeypatch.setattr(
        reserve_module.reservations,
        "append_reservation_row",
        lambda *_args, **_kwargs: asyncio.sleep(0),
    )
    monkeypatch.setattr(
        reserve_module.availability,
        "recompute_clan_availability",
        lambda tag, **_kwargs: asyncio.sleep(0, result=_verified_recompute_result(tag)),
    )
    monkeypatch.setattr(
        reserve_module,
        "_ensure_fresh_clans_for_reservations",
        lambda **_: asyncio.sleep(0, result=True),
    )

    cog = _make_cog(bot)
    asyncio.run(cog.reserve.callback(cog, ctx, "C1C5", f"<@{recruit.id}>"))

    assert ctx.message.deleted is True
    assert date_message.deleted is True
    assert change_message.deleted is True
    assert change_choice_message.deleted is True
    assert changed_date_message.deleted is True
    assert confirm_message.deleted is True
    assert all(
        message.deleted
        for message in thread.sent
        if not (message.content or "").startswith("✅ Reserved 1 spot")
    )
    assert any("2026-08-14" in (message.content or "") for message in thread.sent)


def test_reserve_cleanup_works_for_promo_parent(monkeypatch):
    cog, ctx, thread, (date_message, confirm_message) = _setup_successful_reserve(
        monkeypatch, parent_id=4242, thread_name="M1234-denbotron"
    )
    monkeypatch.setattr(reserve_module, "get_welcome_channel_id", lambda: None)
    monkeypatch.setattr(reserve_module, "get_promo_channel_id", lambda: 4242)

    asyncio.run(cog.reserve.callback(cog, ctx, "C1C5", "<@222>"))

    assert ctx.message.deleted is True
    assert date_message.deleted is True
    assert confirm_message.deleted is True
    assert any(name == "Res-M1234-denbotron-C1C5" for name in thread.edited_names)


def test_reserve_reuses_preflight_plan_and_rows_without_post_append_reload(monkeypatch):
    cog, ctx, thread, _messages = _setup_successful_reserve(monkeypatch)
    calls = {"ensure_fresh": 0, "afind": 0}

    async def forbidden_fresh(**_kwargs):
        calls["ensure_fresh"] += 1
        return False

    async def forbidden_afind(*_args, **_kwargs):
        calls["afind"] += 1
        raise AssertionError("post-append clan force refresh should not run")

    seen_kwargs = {}

    async def fake_recompute(tag, **kwargs):
        seen_kwargs.update(kwargs)
        return _verified_recompute_result(
            tag, reserved=len(kwargs["active_reservations"]), available=4
        )

    monkeypatch.setattr(
        reserve_module, "_ensure_fresh_clans_for_reservations", forbidden_fresh
    )
    monkeypatch.setattr(reserve_module.recruitment, "afind_clan_row", forbidden_afind)
    monkeypatch.setattr(
        reserve_module.availability, "recompute_clan_availability", fake_recompute
    )

    asyncio.run(cog.reserve.callback(cog, ctx, "C1C5", "<@222>"))

    assert calls == {"ensure_fresh": 0, "afind": 0}
    assert seen_kwargs["preflight_plan"] is not None
    assert len(seen_kwargs["active_reservations"]) == 1
    assert any("Effective open spots: `4`" in (m.content or "") for m in thread.sent)


def test_reserve_retries_transient_429_after_append(monkeypatch):
    cog, ctx, thread, _messages = _setup_successful_reserve(monkeypatch)
    attempts = {"count": 0}

    class QuotaError(RuntimeError):
        status_code = 429

    async def fake_sleep(_delay, result=None):
        return result

    async def flaky_recompute(tag, **_kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise QuotaError("429 RESOURCE_EXHAUSTED read requests per minute per user")
        return _verified_recompute_result(tag, reserved=1, available=4)

    monkeypatch.setattr(reserve_module.sheets_core.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        reserve_module.availability, "recompute_clan_availability", flaky_recompute
    )

    asyncio.run(cog.reserve.callback(cog, ctx, "C1C5", "<@222>"))

    assert attempts["count"] == 2
    assert any((m.content or "").startswith("✅ Reserved 1 spot") for m in thread.sent)


def test_reserve_all_recompute_retries_fail_partial_warning(monkeypatch):
    cog, ctx, thread, _messages = _setup_successful_reserve(monkeypatch)
    attempts = {"count": 0}
    runtime_logs: list[tuple[str, str]] = []

    class QuotaError(RuntimeError):
        status_code = 429

    async def fake_sleep(_delay, result=None):
        return result

    async def always_quota(*_args, **_kwargs):
        attempts["count"] += 1
        raise QuotaError("429 RESOURCE_EXHAUSTED read requests per minute per user")

    monkeypatch.setattr(reserve_module.sheets_core.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        reserve_module.availability, "recompute_clan_availability", always_quota
    )
    monkeypatch.setattr(
        reserve_module.human_log,
        "human",
        lambda level, message, **_: runtime_logs.append((level, message)),
    )

    asyncio.run(cog.reserve.callback(cog, ctx, "C1C5", "<@222>"))

    assert attempts["count"] >= 2
    assert any(
        "Reservation row was added, but recruiter-facing availability was NOT updated."
        in (m.content or "")
        for m in thread.sent
    )
    assert any(
        "clan=C1C5" in message and "reservation_row=append:" in message
        for _, message in runtime_logs
    )
