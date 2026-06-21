import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from cogs import recruitment_open_spots as command_module
from cogs.recruitment_open_spots import RecruitmentOpenSpotsCog


class FakeAuthor:
    def __init__(self, member_id=123, display_name="Caillean"):
        self.id = member_id
        self.display_name = display_name
        self.name = display_name


class FakeContext:
    def __init__(self):
        self.author = FakeAuthor()
        self.guild = SimpleNamespace(id=1)
        self.replies = []

    async def reply(self, content=None, **kwargs):
        self.replies.append((content, kwargs))


def _run(coro):
    return asyncio.run(coro)


def test_staff_can_set_open_spots_successfully(monkeypatch):
    ctx = FakeContext()
    logs = []
    writes = []

    monkeypatch.setattr(command_module, "is_staff_member", lambda _ctx: True)
    monkeypatch.setattr(command_module, "is_admin_member", lambda _ctx: False)

    async def fake_set(clan, value):
        writes.append((clan, value))
        return 3, value, "C1CC"

    async def fake_log(message):
        logs.append(message)

    monkeypatch.setattr(command_module.availability, "set_manual_open_spots", fake_set)
    monkeypatch.setattr(command_module.runtime_helpers, "send_log_message", fake_log)

    cog = RecruitmentOpenSpotsCog(bot=None)
    _run(
        cog.setopenspots.callback(
            cog, ctx, "C1CC", "4", reason="missed promo close correction"
        )
    )

    assert writes == [("C1CC", 4)]
    assert "Open spots corrected" in ctx.replies[0][0]
    assert "Open spots: 3 → 4" in ctx.replies[0][0]
    assert "Reason: missed promo close correction" in ctx.replies[0][0]
    assert "Updated by: Caillean" in ctx.replies[0][0]
    assert logs
    assert "Caillean" in logs[0]
    assert "123" in logs[0]
    assert "C1CC 3 → 4" in logs[0]
    assert "missed promo close correction" in logs[0]


def test_non_staff_cannot_run(monkeypatch):
    ctx = FakeContext()
    setter = AsyncMock()
    monkeypatch.setattr(command_module, "is_staff_member", lambda _ctx: False)
    monkeypatch.setattr(command_module, "is_admin_member", lambda _ctx: False)
    monkeypatch.setattr(command_module.availability, "set_manual_open_spots", setter)

    cog = RecruitmentOpenSpotsCog(bot=None)
    _run(cog.setopenspots.callback(cog, ctx, "C1CC", "4", reason="reason"))

    assert "do not have permission" in ctx.replies[0][0]
    setter.assert_not_awaited()


def test_missing_reason_fails(monkeypatch):
    ctx = FakeContext()
    setter = AsyncMock()
    monkeypatch.setattr(command_module, "is_staff_member", lambda _ctx: True)
    monkeypatch.setattr(command_module, "is_admin_member", lambda _ctx: False)
    monkeypatch.setattr(command_module.availability, "set_manual_open_spots", setter)

    cog = RecruitmentOpenSpotsCog(bot=None)
    _run(cog.setopenspots.callback(cog, ctx, "C1CC", "4", reason=None))

    assert "Usage: !setopenspots" in ctx.replies[0][0]
    setter.assert_not_awaited()


def test_invalid_open_spots_fails(monkeypatch):
    ctx = FakeContext()
    setter = AsyncMock()
    monkeypatch.setattr(command_module, "is_staff_member", lambda _ctx: True)
    monkeypatch.setattr(command_module, "is_admin_member", lambda _ctx: False)
    monkeypatch.setattr(command_module.availability, "set_manual_open_spots", setter)

    cog = RecruitmentOpenSpotsCog(bot=None)
    _run(cog.setopenspots.callback(cog, ctx, "C1CC", "4.5", reason="reason"))

    assert "whole number >= 0" in ctx.replies[0][0]
    setter.assert_not_awaited()


def test_negative_open_spots_fails(monkeypatch):
    ctx = FakeContext()
    setter = AsyncMock()
    monkeypatch.setattr(command_module, "is_staff_member", lambda _ctx: True)
    monkeypatch.setattr(command_module, "is_admin_member", lambda _ctx: False)
    monkeypatch.setattr(command_module.availability, "set_manual_open_spots", setter)

    cog = RecruitmentOpenSpotsCog(bot=None)
    _run(cog.setopenspots.callback(cog, ctx, "C1CC", "-1", reason="reason"))

    assert "whole number >= 0" in ctx.replies[0][0]
    setter.assert_not_awaited()


def test_unknown_clan_fails(monkeypatch):
    ctx = FakeContext()
    monkeypatch.setattr(command_module, "is_staff_member", lambda _ctx: True)
    monkeypatch.setattr(command_module, "is_admin_member", lambda _ctx: False)

    async def fake_set(_clan, _value):
        raise ValueError("Unknown clan tag: nope")

    monkeypatch.setattr(command_module.availability, "set_manual_open_spots", fake_set)
    monkeypatch.setattr(command_module.runtime_helpers, "send_log_message", AsyncMock())

    cog = RecruitmentOpenSpotsCog(bot=None)
    _run(cog.setopenspots.callback(cog, ctx, "NOPE", "4", reason="reason"))

    assert "could not be found" in ctx.replies[0][0]


def test_ambiguous_clan_fails(monkeypatch):
    ctx = FakeContext()
    monkeypatch.setattr(command_module, "is_staff_member", lambda _ctx: True)
    monkeypatch.setattr(command_module, "is_admin_member", lambda _ctx: False)

    async def fake_set(_clan, _value):
        raise ValueError("ambiguous clan input")

    monkeypatch.setattr(command_module.availability, "set_manual_open_spots", fake_set)
    monkeypatch.setattr(command_module.runtime_helpers, "send_log_message", AsyncMock())

    cog = RecruitmentOpenSpotsCog(bot=None)
    _run(cog.setopenspots.callback(cog, ctx, "C1C", "4", reason="reason"))

    assert "matches multiple clans" in ctx.replies[0][0]


def test_config_resolution_failure_fails_safely_and_logs(monkeypatch):
    ctx = FakeContext()
    logs = []
    monkeypatch.setattr(command_module, "is_staff_member", lambda _ctx: True)
    monkeypatch.setattr(command_module, "is_admin_member", lambda _ctx: False)

    async def fake_set(_clan, _value):
        raise ValueError("missing required Config key: clans_header_open_spots")

    async def fake_log(message):
        logs.append(message)

    monkeypatch.setattr(command_module.availability, "set_manual_open_spots", fake_set)
    monkeypatch.setattr(command_module.runtime_helpers, "send_log_message", fake_log)

    cog = RecruitmentOpenSpotsCog(bot=None)
    _run(cog.setopenspots.callback(cog, ctx, "C1CC", "4", reason="reason"))

    assert "configuration is incomplete or invalid" in ctx.replies[0][0]
    assert logs and "configuration invalid" in logs[0]


def test_set_manual_open_spots_uses_existing_adjustment_writes_only(monkeypatch):
    from modules.recruitment import availability

    updates = []

    class Worksheet:
        async def update(self, write_range, values, **kwargs):
            updates.append((write_range, values, kwargs))
            return {"ok": True}

    class Headers:
        tab_name = "configured-tab"
        header_map = {
            "clan_tag": 0,
            "manual_open_spots": 2,
            "open_spots": 5,
            "manual_open_spots_seen": 7,
            "reservation_count": 8,
            "reservation_summary": 9,
        }

    class Plan:
        clan_tag = "C1CC"
        sheet_row = 12
        row = ("C1CC", "Clan", "5", "x", "y", "3", "z", "5", "2", "A, B")
        manual_open_index = 2
        open_index = 5
        seen_index = 7
        manual_open = 5
        current_available = 3
        seen_manual_open = 5
        tab_key = "clans_tab"
        tab_name = "configured-tab"
        manual_header_key = "manual_open_spots"
        manual_header_name = "Manual Open Spots"
        open_range = "CustomOpen12"
        seen_range = "CustomSeen12"
        combined_range = None
        headers = Headers()

        def __init__(self, delta=0):
            self.delta = delta
            self.new_value = self.current_available + delta

    async def fake_preflight(clan, delta):
        assert clan == "C1CC"
        return Plan(delta)

    async def fake_worksheet(sheet_id, tab_name):
        assert sheet_id == "sheet-id"
        assert tab_name == "configured-tab"
        return Worksheet()

    async def fake_backoff(func, *args, **kwargs):
        return await func(*args, **kwargs)

    cached = []
    monkeypatch.setattr(
        availability, "preflight_manual_open_spots_adjustment", fake_preflight
    )
    monkeypatch.setattr(
        availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet-id"
    )
    monkeypatch.setattr(
        availability.recruitment,
        "find_clan_row",
        lambda clan, force=True: (12, list(Plan.row)),
    )
    monkeypatch.setattr(availability.async_core, "aget_worksheet", fake_worksheet)
    monkeypatch.setattr(availability.async_core, "acall_with_backoff", fake_backoff)
    monkeypatch.setattr(
        availability.recruitment,
        "update_cached_clan_row",
        lambda row, values: cached.append((row, values)),
    )

    old_value, new_value, clan = _run(availability.set_manual_open_spots("C1CC", 4))

    assert (old_value, new_value, clan) == (3, 4, "C1CC")
    assert [item[0] for item in updates] == ["CustomOpen12", "CustomSeen12"]
    assert updates[0][1] == [["4"]]
    assert updates[1][1] == [["5"]]
    assert cached and cached[0][0] == 12
    updated_row = cached[0][1]
    assert updated_row[2] == "5"  # manual baseline preserved
    assert updated_row[5] == "4"  # visible open spots corrected
    assert updated_row[7] == "5"  # seen marker follows existing adjustment flow
    assert updated_row[8] == "2"  # reservation count untouched
    assert updated_row[9] == "A, B"  # reservation summary untouched


def test_existing_adjustment_flow_still_decrements_after_manual_correction(monkeypatch):
    from modules.recruitment import availability

    updates = []
    state = {"row": ["C1CC", "Clan", "5", "x", "y", "3", "z", "5"]}

    class Worksheet:
        async def update(self, write_range, values, **kwargs):
            updates.append((write_range, values))
            return {"ok": True}

    class Headers:
        tab_name = "configured-tab"
        header_map = {"clan_tag": 0}

    class Plan:
        clan_tag = "C1CC"
        sheet_row = 12
        manual_open_index = 2
        open_index = 5
        seen_index = 7
        tab_key = "clans_tab"
        tab_name = "configured-tab"
        manual_header_key = "manual_open_spots"
        manual_header_name = "Manual Open Spots"
        open_range = "Open12"
        seen_range = "Seen12"
        combined_range = None
        headers = Headers()

        def __init__(self, delta=0):
            self.delta = delta
            self.row = tuple(state["row"])
            self.manual_open = int(state["row"][self.manual_open_index])
            self.current_available = int(state["row"][self.open_index])
            self.seen_manual_open = int(state["row"][self.seen_index])
            base_available = (
                self.manual_open
                if self.manual_open != self.seen_manual_open
                else self.current_available
            )
            self.new_value = max(base_available + delta, 0)

    async def fake_preflight(_clan, delta):
        return Plan(delta)

    async def fake_worksheet(_sheet_id, _tab_name):
        return Worksheet()

    async def fake_backoff(func, *args, **kwargs):
        return await func(*args, **kwargs)

    def fake_cache_update(_row_number, values):
        state["row"] = list(values)

    monkeypatch.setattr(
        availability, "preflight_manual_open_spots_adjustment", fake_preflight
    )
    monkeypatch.setattr(
        availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet-id"
    )
    monkeypatch.setattr(
        availability.recruitment,
        "find_clan_row",
        lambda clan, force=True: (12, list(state["row"])),
    )
    monkeypatch.setattr(availability.async_core, "aget_worksheet", fake_worksheet)
    monkeypatch.setattr(availability.async_core, "acall_with_backoff", fake_backoff)
    monkeypatch.setattr(
        availability.recruitment, "update_cached_clan_row", fake_cache_update
    )

    assert _run(availability.set_manual_open_spots("C1CC", 4)) == (3, 4, "C1CC")
    assert state["row"][2] == "5"
    assert state["row"][5] == "4"
    assert state["row"][7] == "5"

    updates.clear()
    assert _run(availability.adjust_manual_open_spots("C1CC", -1)) == 3

    assert updates == [("Open12", [["3"]]), ("Seen12", [["5"]])]
    assert state["row"][2] == "5"
    assert state["row"][5] == "3"
    assert state["row"][7] == "5"
