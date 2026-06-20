import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

from modules.community.fusion import role_cleanup
from shared.sheets import fusion as fusion_sheets


def _fusion_row(*, opt_in_role_id: int | None = 777) -> fusion_sheets.FusionRow:
    return fusion_sheets.FusionRow(
        fusion_id="f-ended",
        fusion_name="Old Fusion",
        champion="Mavara",
        champion_image_url="",
        fusion_type="traditional",
        fusion_structure="",
        reward_type="fragments",
        needed=400,
        available=450,
        start_at_utc=dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc),
        end_at_utc=dt.datetime(2026, 4, 3, tzinfo=dt.timezone.utc),
        announcement_channel_id=123,
        opt_in_role_id=opt_in_role_id,
        announcement_message_id=456,
        published_at=dt.datetime(2026, 3, 31, tzinfo=dt.timezone.utc),
        last_announcement_refresh_at=None,
        last_announcement_status_hash="",
        status="published",
    )


class _Member:
    def __init__(self, member_id: int, *, fail: bool = False) -> None:
        self.id = member_id
        self._fail = fail
        self.remove_roles = AsyncMock(side_effect=self._remove_roles)

    async def _remove_roles(self, _role, reason=None):
        if self._fail:
            raise RuntimeError("nope")


class _Role:
    def __init__(self, role_id: int, members: list[_Member]) -> None:
        self.id = role_id
        self.name = "Fusion Ping"
        self.members = members


class _Guild:
    def __init__(self, role):
        self.id = 1
        self._role = role

    def get_role(self, _role_id):
        return self._role


def test_ended_fusion_triggers_role_cleanup(monkeypatch):
    async def _run() -> None:
        role_cleanup.clear_recent_role_cleanup_summaries()
        members = [_Member(1), _Member(2)]
        role = _Role(777, members)
        guild = _Guild(role)
        channel = SimpleNamespace(guild=guild)
        bot = SimpleNamespace(guilds=[guild])

        monkeypatch.setattr(fusion_sheets, "get_ended_fusions", AsyncMock(return_value=[_fusion_row()]))
        monkeypatch.setattr(fusion_sheets, "transition_fusion_to_ended", AsyncMock(return_value=True))
        monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
        monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
        monkeypatch.setattr(fusion_sheets, "upsert_role_cleanup_summary", AsyncMock())
        monkeypatch.setattr(role_cleanup, "resolve_announcement_channel", AsyncMock(return_value=channel))
        monkeypatch.setattr(role_cleanup, "_resolve_cleanup_guild", AsyncMock(return_value=guild))

        await role_cleanup.process_ended_fusion_role_cleanup(bot)

        for member in members:
            member.remove_roles.assert_awaited_once_with(role, reason="Fusion ended: f-ended")
        fusion_sheets.transition_fusion_to_ended.assert_awaited_once_with("f-ended")
        fusion_sheets.mark_reminder_sent.assert_awaited_once()
        summaries = role_cleanup.get_recent_role_cleanup_summaries()
        assert len(summaries) == 1
        assert summaries[0].members_found == 2
        assert summaries[0].removed_count == 2
        assert summaries[0].failed_count == 0

    asyncio.run(_run())


def test_cleanup_is_one_shot_via_dedupe(monkeypatch):
    async def _run() -> None:
        role_cleanup.clear_recent_role_cleanup_summaries()
        member = _Member(1)
        role = _Role(777, [member])
        guild = _Guild(role)
        channel = SimpleNamespace(guild=guild)
        bot = SimpleNamespace(guilds=[guild])

        monkeypatch.setattr(fusion_sheets, "get_ended_fusions", AsyncMock(return_value=[_fusion_row()]))
        monkeypatch.setattr(fusion_sheets, "transition_fusion_to_ended", AsyncMock(return_value=False))
        monkeypatch.setattr(
            fusion_sheets,
            "get_sent_reminder_keys",
            AsyncMock(return_value={("__fusion_role_cleanup__", "ended")}),
        )
        monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
        monkeypatch.setattr(fusion_sheets, "upsert_role_cleanup_summary", AsyncMock())
        monkeypatch.setattr(fusion_sheets, "has_role_cleanup_summary", AsyncMock(return_value=False))
        monkeypatch.setattr(role_cleanup, "resolve_announcement_channel", AsyncMock(return_value=channel))

        await role_cleanup.process_ended_fusion_role_cleanup(bot)

        member.remove_roles.assert_not_awaited()
        fusion_sheets.transition_fusion_to_ended.assert_awaited_once_with("f-ended")
        fusion_sheets.mark_reminder_sent.assert_not_awaited()
        summaries = role_cleanup.get_recent_role_cleanup_summaries()
        assert len(summaries) == 1
        assert summaries[0].already_processed is True
        assert summaries[0].skipped_count == 1

    asyncio.run(_run())


def test_missing_role_is_handled_safely(monkeypatch):
    async def _run() -> None:
        guild = _Guild(role=None)
        channel = SimpleNamespace(guild=guild)
        bot = SimpleNamespace(guilds=[guild])

        monkeypatch.setattr(fusion_sheets, "get_ended_fusions", AsyncMock(return_value=[_fusion_row()]))
        monkeypatch.setattr(fusion_sheets, "transition_fusion_to_ended", AsyncMock(return_value=True))
        monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
        monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
        monkeypatch.setattr(fusion_sheets, "upsert_role_cleanup_summary", AsyncMock())
        monkeypatch.setattr(role_cleanup, "resolve_announcement_channel", AsyncMock(return_value=channel))
        monkeypatch.setattr(role_cleanup, "_resolve_cleanup_guild", AsyncMock(return_value=guild))

        await role_cleanup.process_ended_fusion_role_cleanup(bot)

        fusion_sheets.mark_reminder_sent.assert_awaited_once()
        summaries = role_cleanup.get_recent_role_cleanup_summaries()
        assert summaries[-1].status == "skipped"
        assert summaries[-1].skipped_count == 1
        assert summaries[-1].failure_reasons == ["role missing"]

    asyncio.run(_run())


def test_partial_member_failure_does_not_abort(monkeypatch):
    async def _run() -> None:
        role_cleanup.clear_recent_role_cleanup_summaries()
        members = [_Member(1, fail=True), _Member(2, fail=False)]
        role = _Role(777, members)
        guild = _Guild(role)
        channel = SimpleNamespace(guild=guild)
        bot = SimpleNamespace(guilds=[guild])

        monkeypatch.setattr(fusion_sheets, "get_ended_fusions", AsyncMock(return_value=[_fusion_row()]))
        monkeypatch.setattr(fusion_sheets, "transition_fusion_to_ended", AsyncMock(return_value=True))
        monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
        monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
        monkeypatch.setattr(fusion_sheets, "upsert_role_cleanup_summary", AsyncMock())
        monkeypatch.setattr(role_cleanup, "resolve_announcement_channel", AsyncMock(return_value=channel))

        await role_cleanup.process_ended_fusion_role_cleanup(bot)

        assert members[0].remove_roles.await_count == 1
        assert members[1].remove_roles.await_count == 1
        fusion_sheets.mark_reminder_sent.assert_awaited_once()
        summaries = role_cleanup.get_recent_role_cleanup_summaries()
        assert summaries[-1].members_found == 2
        assert summaries[-1].removed_count == 1
        assert summaries[-1].failed_count == 1
        assert summaries[-1].status == "partial_failure"

    asyncio.run(_run())


def test_loads_persisted_unreported_cleanup_summary(monkeypatch):
    async def _run() -> None:
        monkeypatch.setattr(
            fusion_sheets,
            "get_unreported_role_cleanup_summaries",
            AsyncMock(
                return_value=[
                    {
                        "fusion_id": "f-ended",
                        "fusion_name": "Old Fusion",
                        "role_id": 777,
                        "role_name": "Fusion Ping",
                        "members_found": 3,
                        "removed_count": 2,
                        "failed_count": 1,
                        "skipped_count": 0,
                        "status": "partial_failure",
                        "failure_reasons": ["member 1: permission/hierarchy/API failure"],
                        "_row_number": 12,
                    }
                ]
            ),
        )

        summaries = await role_cleanup.load_unreported_role_cleanup_summaries()

        assert len(summaries) == 1
        assert summaries[0].fusion_id == "f-ended"
        assert summaries[0].removed_count == 2
        assert summaries[0].failed_count == 1
        assert summaries[0].row_number == 12

    asyncio.run(_run())


def test_mark_reported_uses_persisted_rows_and_clears_runtime(monkeypatch):
    async def _run() -> None:
        marker = AsyncMock()
        monkeypatch.setattr(fusion_sheets, "mark_role_cleanup_summaries_reported", marker)
        role_cleanup.clear_recent_role_cleanup_summaries()
        runtime_summary = role_cleanup.FusionRoleCleanupSummary(
            fusion_id="f-ended",
            fusion_name="Old Fusion",
            role_id=777,
            row_number=12,
        )
        role_cleanup._record_summary(runtime_summary)

        await role_cleanup.mark_role_cleanup_summaries_reported([runtime_summary])

        marker.assert_awaited_once_with([12])
        assert role_cleanup.get_recent_role_cleanup_summaries() == []

    asyncio.run(_run())


def test_dedupe_does_not_recreate_summary_after_reported(monkeypatch):
    async def _run() -> None:
        role_cleanup.clear_recent_role_cleanup_summaries()
        member = _Member(1)
        role = _Role(777, [member])
        guild = _Guild(role)
        channel = SimpleNamespace(guild=guild)
        bot = SimpleNamespace(guilds=[guild])

        monkeypatch.setattr(fusion_sheets, "get_ended_fusions", AsyncMock(return_value=[_fusion_row()]))
        monkeypatch.setattr(fusion_sheets, "transition_fusion_to_ended", AsyncMock(return_value=False))
        monkeypatch.setattr(
            fusion_sheets,
            "get_sent_reminder_keys",
            AsyncMock(return_value={("__fusion_role_cleanup__", "ended")}),
        )
        upsert = AsyncMock()
        monkeypatch.setattr(fusion_sheets, "upsert_role_cleanup_summary", upsert)
        monkeypatch.setattr(fusion_sheets, "has_role_cleanup_summary", AsyncMock(return_value=True))
        monkeypatch.setattr(role_cleanup, "resolve_announcement_channel", AsyncMock(return_value=channel))

        await role_cleanup.process_ended_fusion_role_cleanup(bot)

        member.remove_roles.assert_not_awaited()
        upsert.assert_not_awaited()
        assert role_cleanup.get_recent_role_cleanup_summaries() == []

    asyncio.run(_run())
