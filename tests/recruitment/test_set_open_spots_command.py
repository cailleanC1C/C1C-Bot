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
        resolved_clan_tag = "C1CC"

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
        resolved_clan_tag = "C1CC"

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


def test_sheet_write_failure_uses_write_message_and_logs_exc_info(monkeypatch, caplog):
    ctx = FakeContext()
    logs = []
    monkeypatch.setattr(command_module, "is_staff_member", lambda _ctx: True)
    monkeypatch.setattr(command_module, "is_admin_member", lambda _ctx: False)

    async def fake_set(_clan, _value):
        raise RuntimeError("worksheet update exploded")

    async def fake_log(message):
        logs.append(message)

    monkeypatch.setattr(command_module.availability, "set_manual_open_spots", fake_set)
    monkeypatch.setattr(command_module.runtime_helpers, "send_log_message", fake_log)

    cog = RecruitmentOpenSpotsCog(bot=None)
    with caplog.at_level("ERROR", logger=command_module.log.name):
        _run(cog.setopenspots.callback(cog, ctx, "C1CC", "4", reason="reason"))

    assert "sheet write failed" in ctx.replies[0][0]
    assert logs and "worksheet_update" in logs[0]
    record = next(
        rec for rec in caplog.records if rec.getMessage() == "setopenspots failed"
    )
    assert record.exc_info is not None


def test_success_reply_failure_does_not_report_config_failure(monkeypatch, caplog):
    class ReplyFailingContext(FakeContext):
        async def reply(self, content=None, **kwargs):
            raise RuntimeError("discord reply failed")

    ctx = ReplyFailingContext()
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
    with caplog.at_level("ERROR", logger=command_module.log.name):
        _run(cog.setopenspots.callback(cog, ctx, "C1CC", "4", reason="reason"))

    assert writes == [("C1CC", 4)]
    assert logs and "succeeded" in logs[0]
    assert "configuration invalid" not in logs[0]
    assert not any(
        "configuration/header resolution failed" in rec.getMessage()
        for rec in caplog.records
    )
    record = next(
        rec
        for rec in caplog.records
        if rec.getMessage() == "setopenspots success reply failed after sheet update"
    )
    assert record.exc_info is not None


def test_sheet_failure_logs_phase_context_and_exception_details(monkeypatch, caplog):
    ctx = FakeContext()
    logs = []
    monkeypatch.setattr(command_module, "is_staff_member", lambda _ctx: True)
    monkeypatch.setattr(command_module, "is_admin_member", lambda _ctx: False)

    async def fake_set(_clan, _value):
        raise command_module.availability.AvailabilityOperationError(
            "worksheet_update", "C1CC", "worksheet update failed: RuntimeError: boom"
        )

    async def fake_log(message):
        logs.append(message)

    monkeypatch.setattr(command_module.availability, "set_manual_open_spots", fake_set)
    monkeypatch.setattr(command_module.runtime_helpers, "send_log_message", fake_log)

    cog = RecruitmentOpenSpotsCog(bot=None)
    with caplog.at_level("ERROR", logger=command_module.log.name):
        _run(cog.setopenspots.callback(cog, ctx, "C1CC", "4", reason="reason"))

    assert "sheet write failed" in ctx.replies[0][0]
    record = next(
        rec for rec in caplog.records if rec.getMessage() == "setopenspots failed"
    )
    assert record.exc_info is not None
    assert record.clan_tag == "C1CC"
    assert record.requested_open_spots == 4
    assert record.caller_source == "staff"
    assert record.operation_phase == "worksheet_update"
    assert record.exception_type == "AvailabilityOperationError"
    assert "worksheet update failed" in record.exception_message
    assert logs and "worksheet_update" in logs[0]


def test_quota_failure_gets_rate_limit_message(monkeypatch):
    ctx = FakeContext()
    monkeypatch.setattr(command_module, "is_staff_member", lambda _ctx: True)
    monkeypatch.setattr(command_module, "is_admin_member", lambda _ctx: False)

    async def fake_set(_clan, _value):
        raise command_module.availability.AvailabilityOperationError(
            "worksheet_lookup",
            "C1CC",
            "429 RESOURCE_EXHAUSTED read requests per minute per user",
        )

    monkeypatch.setattr(command_module.availability, "set_manual_open_spots", fake_set)
    monkeypatch.setattr(command_module.runtime_helpers, "send_log_message", AsyncMock())

    cog = RecruitmentOpenSpotsCog(bot=None)
    _run(cog.setopenspots.callback(cog, ctx, "C1CC", "4", reason="reason"))

    assert "quota/rate limits" in ctx.replies[0][0]
    assert "sheet update failed" not in ctx.replies[0][0]


def test_post_write_verification_failure_has_distinct_message(monkeypatch):
    ctx = FakeContext()
    monkeypatch.setattr(command_module, "is_staff_member", lambda _ctx: True)
    monkeypatch.setattr(command_module, "is_admin_member", lambda _ctx: False)

    async def fake_set(_clan, _value):
        raise command_module.availability.AvailabilityOperationError(
            "post-write verification", "C1CC", "clan cache refresh failed for C1CC"
        )

    monkeypatch.setattr(command_module.availability, "set_manual_open_spots", fake_set)
    monkeypatch.setattr(command_module.runtime_helpers, "send_log_message", AsyncMock())

    cog = RecruitmentOpenSpotsCog(bot=None)
    _run(cog.setopenspots.callback(cog, ctx, "C1CC", "4", reason="reason"))

    assert "post-write verification failed" in ctx.replies[0][0]
    assert "sheet write failed" not in ctx.replies[0][0]


def test_set_manual_open_spots_reuses_preflight_plan_and_skips_post_write_reread(
    monkeypatch,
):
    from modules.recruitment import availability

    updates = []
    preflight_calls = []

    class Worksheet:
        async def update(self, write_range, values, **kwargs):
            updates.append((write_range, values))
            return {"ok": True}

    class Headers:
        tab_name = "configured-tab"
        header_map = {"clan_tag": 0}

    class Plan:
        clan_tag = "C1CT"
        sheet_row = 9
        row = ("C1CT", "Clan", "6", "", "", "2", "", "6")
        manual_open_index = 2
        open_index = 5
        seen_index = 7
        manual_open = 6
        current_available = 2
        seen_manual_open = 6
        tab_key = "clans_tab"
        tab_name = "configured-tab"
        manual_header_key = "manual_open_spots"
        manual_header_name = "Manual Open Spots"
        open_range = "Open9"
        seen_range = "Seen9"
        combined_range = None
        headers = Headers()
        resolved_clan_tag = "C1CT"

        def __init__(self, delta=0):
            self.delta = delta
            self.new_value = self.current_available + delta

    async def fake_preflight(clan, delta, *, preflight_plan=None):
        preflight_calls.append((clan, delta, preflight_plan is not None))
        plan = Plan(delta)
        plan.preflight_plan = preflight_plan or object()
        return plan

    async def fake_worksheet(_sheet_id, _tab_name):
        return Worksheet()

    async def fake_backoff(func, *args, **kwargs):
        return await func(*args, **kwargs)

    monkeypatch.setattr(
        availability, "preflight_manual_open_spots_adjustment", fake_preflight
    )
    monkeypatch.setattr(
        availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet-id"
    )
    monkeypatch.setattr(availability.async_core, "aget_worksheet", fake_worksheet)
    monkeypatch.setattr(availability.async_core, "acall_with_backoff", fake_backoff)
    monkeypatch.setattr(
        availability.recruitment, "update_cached_clan_row", lambda *_args: None
    )

    async def fail_post_write_reread(*_args, **_kwargs):
        raise AssertionError("post-write reread should not run")

    monkeypatch.setattr(
        availability.recruitment, "afind_clan_row", fail_post_write_reread
    )

    assert _run(availability.set_manual_open_spots("C1CT", 1)) == (2, 1, "C1CT")
    assert preflight_calls == [("C1CT", 0, False), ("C1CT", -1, True)]
    assert updates == [("Open9", [["1"]]), ("Seen9", [["6"]])]


def test_setopenspots_logs_compact_quota_summary(monkeypatch, caplog):
    ctx = FakeContext()
    monkeypatch.setattr(command_module, "is_staff_member", lambda _ctx: True)
    monkeypatch.setattr(command_module, "is_admin_member", lambda _ctx: False)
    monkeypatch.setattr(command_module.runtime_helpers, "send_log_message", AsyncMock())

    async def fake_set(clan, value):
        summary = command_module.availability.current_quota_summary()
        summary.sheet_reads_count = 0
        summary.sheet_writes_count = 1
        summary.cache_hit = True
        summary.used_cached_header = True
        summary.used_cached_row = True
        return 2, value, clan

    monkeypatch.setattr(command_module.availability, "set_manual_open_spots", fake_set)

    cog = RecruitmentOpenSpotsCog(bot=None)
    with caplog.at_level("INFO", logger=command_module.log.name):
        _run(cog.setopenspots.callback(cog, ctx, "C1CT", "1", reason="member left"))

    record = next(
        rec
        for rec in caplog.records
        if rec.getMessage() == "setopenspots quota summary"
    )
    assert record.command == "setopenspots"
    assert record.clan_tag == "C1CT"
    assert record.sheet_reads_count == 0
    assert record.sheet_writes_count == 1
    assert record.cache_hit is True
    assert record.used_cached_header is True
    assert record.used_cached_row is True


def _event_loop_sync_guard(*_args, **_kwargs):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return None
    raise RuntimeError(
        "_retry_with_backoff must not run inside an active event loop; use the async variant"
    )


def _patch_real_setopenspots_sheet_path(monkeypatch, *, warm_cache):
    from modules.recruitment import availability

    header = [
        "Clan Tag",
        "Manual Open Spots",
        "Open Spots",
        "Inactives",
        "Reservation Count",
        "Reservation Summary",
        "Manual Open Spots Seen",
    ]
    row = ["C1CT", "6", "2", "0", "0", "", "6"]
    config = {
        "clans_header_clan_tag": "Clan Tag",
        "clans_header_manual_open_spots": "Manual Open Spots",
        "clans_header_open_spots": "Open Spots",
        "clans_header_inactives": "Inactives",
        "clans_header_reservation_count": "Reservation Count",
        "clans_header_reservation_summary": "Reservation Summary",
        "clans_header_manual_open_spots_seen": "Manual Open Spots Seen",
    }
    updates = []
    cache_updates = []
    sync_calls = {"header": 0, "rows": 0}

    class Worksheet:
        async def update(self, write_range, values, **kwargs):
            updates.append((write_range, values, kwargs))
            return {"ok": True}

        async def batch_update(self, data, **kwargs):
            updates.append(("batch_update", data, kwargs))
            return {"ok": True}

    async def fake_config_value(key, default=None, *, force=False):
        return config.get(key, default)

    async def fake_aget_header(*, force=False):
        if warm_cache:
            raise AssertionError("warm header path should not call async header loader")
        return list(header)

    async def fake_afetch_clans(*, force=False):
        if warm_cache:
            raise AssertionError("warm clan path should not call async clan loader")
        return [list(row)]

    def fake_get_header(*, force=False):
        if not warm_cache:
            return _event_loop_sync_guard()
        sync_calls["header"] += 1
        return list(header)

    def fake_fetch_clans(*, force=False):
        if not warm_cache:
            return _event_loop_sync_guard()
        sync_calls["rows"] += 1
        return [list(row)]

    async def fake_aget_worksheet(_sheet_id, _tab_name):
        return Worksheet()

    async def fake_backoff(func, *args, **kwargs):
        return await func(*args, **kwargs)

    monkeypatch.setattr(command_module, "is_staff_member", lambda _ctx: True)
    monkeypatch.setattr(command_module, "is_admin_member", lambda _ctx: False)
    monkeypatch.setattr(command_module.runtime_helpers, "send_log_message", AsyncMock())
    monkeypatch.setattr(
        availability.recruitment, "get_recruitment_sheet_id", lambda: "sheet-id"
    )
    monkeypatch.setattr(
        availability.recruitment,
        "get_clans_tab_name_async",
        lambda: asyncio.sleep(0, result="configured-tab"),
    )
    monkeypatch.setattr(
        availability.recruitment, "get_config_value_async", fake_config_value
    )
    monkeypatch.setattr(
        availability.recruitment, "aget_clan_header_row", fake_aget_header
    )
    monkeypatch.setattr(availability.recruitment, "afetch_clans", fake_afetch_clans)
    monkeypatch.setattr(
        availability.recruitment, "get_clan_header_row", fake_get_header
    )
    monkeypatch.setattr(availability.recruitment, "fetch_clans", fake_fetch_clans)
    monkeypatch.setattr(
        availability.recruitment, "get_config_value", _event_loop_sync_guard
    )
    monkeypatch.setattr(
        availability.recruitment, "find_clan_row", _event_loop_sync_guard
    )
    monkeypatch.setattr(
        availability.recruitment, "get_clans_tab_name", _event_loop_sync_guard
    )
    monkeypatch.setattr(
        availability.recruitment, "clan_header_cache_ready", lambda: warm_cache
    )
    monkeypatch.setattr(
        availability.recruitment, "clan_cache_ready", lambda: warm_cache
    )
    monkeypatch.setattr(availability.async_core, "aget_worksheet", fake_aget_worksheet)
    monkeypatch.setattr(availability.async_core, "acall_with_backoff", fake_backoff)
    monkeypatch.setattr(
        availability.recruitment,
        "update_cached_clan_row",
        lambda sheet_row, values: cache_updates.append((sheet_row, list(values))),
    )
    return updates, cache_updates, sync_calls


def test_setopenspots_warm_cache_uses_cached_header_and_row_without_sheet_reads(
    monkeypatch, caplog
):
    updates, cache_updates, sync_calls = _patch_real_setopenspots_sheet_path(
        monkeypatch, warm_cache=True
    )
    ctx = FakeContext()
    cog = RecruitmentOpenSpotsCog(bot=None)

    with caplog.at_level("INFO", logger=command_module.log.name):
        _run(cog.setopenspots.callback(cog, ctx, "C1CT", "1", reason="member left"))

    summaries = [
        rec
        for rec in caplog.records
        if rec.getMessage() == "setopenspots quota summary"
    ]
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.cache_hit is True
    assert summary.cache_miss is False
    assert summary.used_cached_header is True
    assert summary.used_cached_row is True
    assert summary.sheet_reads_count == 0
    assert summary.sheet_writes_count == 1
    assert sync_calls == {"header": 1, "rows": 1}
    assert updates == [
        (
            "batch_update",
            [
                {"range": "C4", "values": [["1"]]},
                {"range": "G4", "values": [["6"]]},
            ],
            {"value_input_option": "RAW"},
        )
    ]
    assert cache_updates and cache_updates[0][1][2] == "1"


def test_setopenspots_cold_cache_uses_async_helpers_not_sync_event_loop_helpers(
    monkeypatch, caplog
):
    updates, cache_updates, sync_calls = _patch_real_setopenspots_sheet_path(
        monkeypatch, warm_cache=False
    )
    ctx = FakeContext()
    cog = RecruitmentOpenSpotsCog(bot=None)

    with caplog.at_level("INFO", logger=command_module.log.name):
        _run(cog.setopenspots.callback(cog, ctx, "C1CT", "1", reason="member left"))

    assert "Open spots corrected" in ctx.replies[0][0]
    summaries = [
        rec
        for rec in caplog.records
        if rec.getMessage() == "setopenspots quota summary"
    ]
    assert len(summaries) == 1
    summary = summaries[0]
    assert summary.cache_hit is False
    assert summary.cache_miss is True
    assert summary.used_cached_header is False
    assert summary.used_cached_row is False
    assert summary.sheet_reads_count == 1
    assert summary.sheet_writes_count == 1
    assert sync_calls == {"header": 0, "rows": 0}
    assert updates
    assert cache_updates


def test_setopenspots_quota_summary_logged_once_on_failure(monkeypatch, caplog):
    ctx = FakeContext()
    monkeypatch.setattr(command_module, "is_staff_member", lambda _ctx: True)
    monkeypatch.setattr(command_module, "is_admin_member", lambda _ctx: False)
    monkeypatch.setattr(command_module.runtime_helpers, "send_log_message", AsyncMock())

    async def fake_set(_clan, _value):
        raise command_module.availability.AvailabilityOperationError(
            "worksheet_update", "C1CT", "boom"
        )

    monkeypatch.setattr(command_module.availability, "set_manual_open_spots", fake_set)
    cog = RecruitmentOpenSpotsCog(bot=None)

    with caplog.at_level("INFO", logger=command_module.log.name):
        _run(cog.setopenspots.callback(cog, ctx, "C1CT", "1", reason="member left"))

    summaries = [
        rec
        for rec in caplog.records
        if rec.getMessage() == "setopenspots quota summary"
    ]
    assert len(summaries) == 1
    assert "sheet write failed" in ctx.replies[0][0]
