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
        self.members = members


class _Guild:
    def __init__(self, role):
        self.id = 1
        self._role = role

    def get_role(self, _role_id):
        return self._role


def test_ended_fusion_triggers_role_cleanup(monkeypatch):
    async def _run() -> None:
        members = [_Member(1), _Member(2)]
        role = _Role(777, members)
        guild = _Guild(role)
        channel = SimpleNamespace(guild=guild)
        bot = SimpleNamespace(guilds=[guild])

        monkeypatch.setattr(fusion_sheets, "get_ended_fusions", AsyncMock(return_value=[_fusion_row()]))
        monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
        monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
        monkeypatch.setattr(role_cleanup, "resolve_announcement_channel", AsyncMock(return_value=channel))
        monkeypatch.setattr(role_cleanup, "_resolve_cleanup_guild", AsyncMock(return_value=guild))

        await role_cleanup.process_ended_fusion_role_cleanup(bot)

        for member in members:
            member.remove_roles.assert_awaited_once_with(role, reason="Fusion ended: f-ended")
        fusion_sheets.mark_reminder_sent.assert_awaited_once()

    asyncio.run(_run())


def test_cleanup_is_one_shot_via_dedupe(monkeypatch):
    async def _run() -> None:
        member = _Member(1)
        role = _Role(777, [member])
        guild = _Guild(role)
        channel = SimpleNamespace(guild=guild)
        bot = SimpleNamespace(guilds=[guild])

        monkeypatch.setattr(fusion_sheets, "get_ended_fusions", AsyncMock(return_value=[_fusion_row()]))
        monkeypatch.setattr(
            fusion_sheets,
            "get_sent_reminder_keys",
            AsyncMock(return_value={("__fusion_role_cleanup__", "ended")}),
        )
        monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
        monkeypatch.setattr(role_cleanup, "resolve_announcement_channel", AsyncMock(return_value=channel))

        await role_cleanup.process_ended_fusion_role_cleanup(bot)

        member.remove_roles.assert_not_awaited()
        fusion_sheets.mark_reminder_sent.assert_not_awaited()

    asyncio.run(_run())


def test_missing_role_is_handled_safely(monkeypatch):
    async def _run() -> None:
        guild = _Guild(role=None)
        channel = SimpleNamespace(guild=guild)
        bot = SimpleNamespace(guilds=[guild])

        monkeypatch.setattr(fusion_sheets, "get_ended_fusions", AsyncMock(return_value=[_fusion_row()]))
        monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
        monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
        monkeypatch.setattr(role_cleanup, "resolve_announcement_channel", AsyncMock(return_value=channel))
        monkeypatch.setattr(role_cleanup, "_resolve_cleanup_guild", AsyncMock(return_value=guild))

        await role_cleanup.process_ended_fusion_role_cleanup(bot)

        fusion_sheets.mark_reminder_sent.assert_awaited_once()

    asyncio.run(_run())


def test_partial_member_failure_does_not_abort(monkeypatch):
    async def _run() -> None:
        members = [_Member(1, fail=True), _Member(2, fail=False)]
        role = _Role(777, members)
        guild = _Guild(role)
        channel = SimpleNamespace(guild=guild)
        bot = SimpleNamespace(guilds=[guild])

        monkeypatch.setattr(fusion_sheets, "get_ended_fusions", AsyncMock(return_value=[_fusion_row()]))
        monkeypatch.setattr(fusion_sheets, "get_sent_reminder_keys", AsyncMock(return_value=set()))
        monkeypatch.setattr(fusion_sheets, "mark_reminder_sent", AsyncMock())
        monkeypatch.setattr(role_cleanup, "resolve_announcement_channel", AsyncMock(return_value=channel))

        await role_cleanup.process_ended_fusion_role_cleanup(bot)

        assert members[0].remove_roles.await_count == 1
        assert members[1].remove_roles.await_count == 1
        fusion_sheets.mark_reminder_sent.assert_awaited_once()

    asyncio.run(_run())
