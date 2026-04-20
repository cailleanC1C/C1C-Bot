import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock

from modules.community.fusion import opt_in_view
from shared.sheets import fusion as fusion_sheets


def _fusion_row(*, opt_in_role_id: int | None) -> fusion_sheets.FusionRow:
    return fusion_sheets.FusionRow(
        fusion_id="f-1",
        fusion_name="Mavara",
        champion="Mavara",
        champion_image_url="",
        fusion_type="traditional",
        fusion_structure="",
        reward_type="fragments",
        needed=400,
        available=450,
        start_at_utc=dt.datetime(2026, 4, 8, tzinfo=dt.timezone.utc),
        end_at_utc=dt.datetime(2026, 4, 22, tzinfo=dt.timezone.utc),
        announcement_channel_id=123,
        opt_in_role_id=opt_in_role_id,
        announcement_message_id=456,
        published_at=dt.datetime(2026, 4, 7, tzinfo=dt.timezone.utc),
        last_announcement_refresh_at=None,
        last_announcement_status_hash="",
        status="active",
    )


class _Response:
    def __init__(self) -> None:
        self.send_message = AsyncMock()

    def is_done(self) -> bool:
        return False


class _Member:
    def __init__(self, role):
        self.id = 10
        self.guild = SimpleNamespace(id=1)
        self.roles = [] if role is None else [role]
        self.add_roles = AsyncMock(side_effect=self._add)
        self.remove_roles = AsyncMock(side_effect=self._remove)

    async def _add(self, role, reason=None):
        if role not in self.roles:
            self.roles.append(role)

    async def _remove(self, role, reason=None):
        self.roles = [r for r in self.roles if r != role]


class _Guild:
    def __init__(self, role, member):
        self.id = 1
        self._role = role
        self._member = member

    def get_role(self, _role_id):
        return self._role

    def get_member(self, _user_id):
        return self._member


def _interaction(guild, member):
    return SimpleNamespace(
        guild=guild,
        user=member,
        response=_Response(),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def test_opt_in_click_adds_role(monkeypatch):
    async def _run() -> None:
        role = SimpleNamespace(id=777)
        member = _Member(role=None)
        guild = _Guild(role=role, member=member)
        interaction = _interaction(guild, member)
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row(opt_in_role_id=777)))

        await opt_in_view._handle_opt_action(interaction, action="in")

        member.add_roles.assert_awaited_once_with(role, reason="Fusion role opt-in button")

    asyncio.run(_run())


def test_opt_in_click_is_harmless_when_already_opted_in(monkeypatch):
    async def _run() -> None:
        role = SimpleNamespace(id=777)
        member = _Member(role=role)
        guild = _Guild(role=role, member=member)
        interaction = _interaction(guild, member)
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row(opt_in_role_id=777)))

        await opt_in_view._handle_opt_action(interaction, action="in")

        member.add_roles.assert_not_awaited()

    asyncio.run(_run())


def test_opt_out_click_removes_role(monkeypatch):
    async def _run() -> None:
        role = SimpleNamespace(id=777)
        member = _Member(role=role)
        guild = _Guild(role=role, member=member)
        interaction = _interaction(guild, member)
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row(opt_in_role_id=777)))

        await opt_in_view._handle_opt_action(interaction, action="out")

        member.remove_roles.assert_awaited_once_with(role, reason="Fusion role opt-out button")

    asyncio.run(_run())


def test_opt_out_click_is_harmless_when_missing_role(monkeypatch):
    async def _run() -> None:
        role = SimpleNamespace(id=777)
        member = _Member(role=None)
        guild = _Guild(role=role, member=member)
        interaction = _interaction(guild, member)
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row(opt_in_role_id=777)))

        await opt_in_view._handle_opt_action(interaction, action="out")

        member.remove_roles.assert_not_awaited()

    asyncio.run(_run())


def test_missing_guild_role_is_handled_cleanly(monkeypatch):
    async def _run() -> None:
        member = _Member(role=None)
        guild = _Guild(role=None, member=member)
        interaction = _interaction(guild, member)
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row(opt_in_role_id=777)))

        await opt_in_view._handle_opt_action(interaction, action="in")

        interaction.response.send_message.assert_awaited_once_with("Fusion role is missing in this server.", ephemeral=True)

    asyncio.run(_run())


def test_permission_failure_is_handled_cleanly(monkeypatch):
    async def _run() -> None:
        role = SimpleNamespace(id=777)
        member = _Member(role=None)
        member.add_roles = AsyncMock(side_effect=RuntimeError("forbidden"))
        guild = _Guild(role=role, member=member)
        interaction = _interaction(guild, member)
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row(opt_in_role_id=777)))

        await opt_in_view._handle_opt_action(interaction, action="in")

        interaction.response.send_message.assert_awaited_once_with(
            "Couldn’t update your fusion role right now.", ephemeral=True
        )

    asyncio.run(_run())


def test_build_view_keeps_opt_buttons_when_role_configured():
    view = opt_in_view.build_fusion_opt_in_view(_fusion_row(opt_in_role_id=777))

    custom_ids = [item.custom_id for item in view.children]
    assert "fusion:opt_in" in custom_ids
    assert "fusion:opt_out" in custom_ids
    assert "fusion:my_progress" in custom_ids


def test_build_view_keeps_progress_button_without_opt_role():
    view = opt_in_view.build_fusion_opt_in_view(_fusion_row(opt_in_role_id=None))

    custom_ids = [item.custom_id for item in view.children]
    assert "fusion:opt_in" not in custom_ids
    assert "fusion:opt_out" not in custom_ids
    assert custom_ids == ["fusion:my_progress"]


def test_my_progress_accepts_nested_progress_payload(monkeypatch):
    async def _run() -> None:
        now = dt.datetime(2026, 4, 10, 12, 0, tzinfo=dt.timezone.utc)
        target = _fusion_row(opt_in_role_id=777)
        event = fusion_sheets.FusionEventRow(
            fusion_id=target.fusion_id,
            event_id="e-1",
            event_name="Event 1",
            event_type="tournament",
            category="arena",
            start_at_utc=now - dt.timedelta(hours=1),
            end_at_utc=now + dt.timedelta(hours=1),
            reward_amount=100.0,
            bonus=None,
            reward_type="fragments",
            points_needed=1000,
            is_estimated=False,
            sort_order=1,
        )
        member = _Member(role=None)
        guild = _Guild(role=SimpleNamespace(id=777), member=member)
        interaction = _interaction(guild, member)

        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=target))
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[event]))
        monkeypatch.setattr(
            fusion_sheets,
            "get_user_event_progress",
            AsyncMock(return_value={"progress": {"e-1": "done"}}),
        )

        await opt_in_view._handle_my_progress(interaction)

        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert kwargs["ephemeral"] is True
        assert kwargs["embed"].title.startswith("My Progress")

    asyncio.run(_run())
