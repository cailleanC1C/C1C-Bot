import asyncio
import datetime as dt
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from modules.community.fusion import opt_in_view
from shared.sheets import fusion as fusion_sheets


class _Response:
    def __init__(self) -> None:
        self._is_done = False
        self.send_message = AsyncMock()
        self.edit_message = AsyncMock()
        self.defer = AsyncMock(side_effect=self._defer)

    def is_done(self) -> bool:
        return self._is_done

    async def _defer(self, **_kwargs) -> None:
        self._is_done = True


class _UnknownInteractionError(Exception):
    code = 10062


def _interaction(*, user_id: int = 10):
    return SimpleNamespace(
        guild=None,
        user=SimpleNamespace(id=user_id, display_name="Test User"),
        client=SimpleNamespace(),
        response=_Response(),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def _fusion_row(*, fusion_type: str = "fragment") -> fusion_sheets.FusionRow:
    return fusion_sheets.FusionRow(
        fusion_id="f-1",
        fusion_name="Mavara",
        champion="Mavara",
        champion_image_url="",
        fusion_type=fusion_type,
        fusion_structure="",
        reward_type="fragments",
        needed=100,
        available=115,
        start_at_utc=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
        end_at_utc=dt.datetime(2026, 8, 31, tzinfo=dt.timezone.utc),
        announcement_channel_id=123,
        opt_in_role_id=777,
        announcement_message_id=456,
        published_at=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
        last_announcement_refresh_at=None,
        last_announcement_status_hash="",
        status="active",
    )


def _event_row() -> fusion_sheets.FusionEventRow:
    return fusion_sheets.FusionEventRow(
        fusion_id="f-1",
        event_id="e1",
        event_name="Dungeon Dash",
        event_type="dungeon",
        category="Tournaments",
        start_at_utc=dt.datetime(2026, 8, 1, tzinfo=dt.timezone.utc),
        end_at_utc=dt.datetime(2026, 8, 2, tzinfo=dt.timezone.utc),
        reward_amount=5.0,
        bonus=None,
        reward_type="fragments",
        points_needed=None,
        is_estimated=False,
        sort_order=1,
    )


def _button(view: opt_in_view.FusionOptInView, custom_id: str):
    return next(child for child in view.children if child.custom_id == custom_id)


def test_my_progress_button_acknowledges_before_sheets_io(monkeypatch):
    async def _run() -> None:
        interaction = _interaction()
        events = [_event_row()]
        calls: list[str] = []

        original_defer = interaction.response.defer

        async def _defer(**kwargs) -> None:
            calls.append("defer")
            await interaction.response._defer(**kwargs)

        interaction.response.defer = AsyncMock(side_effect=_defer)

        async def _get_publishable_fusion():
            calls.append("sheet")
            assert interaction.response.is_done() is True
            return _fusion_row()

        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", _get_publishable_fusion)
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=events))
        monkeypatch.setattr(fusion_sheets, "get_user_event_progress", AsyncMock(return_value={}))

        view = opt_in_view.FusionOptInView()
        await _button(view, "fusion:my_progress").callback(interaction)

        assert calls[:2] == ["defer", "sheet"]
        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        interaction.response.send_message.assert_not_awaited()
        interaction.followup.send.assert_awaited_once()
        assert interaction.followup.send.await_args.kwargs["ephemeral"] is True
        assert isinstance(interaction.followup.send.await_args.kwargs["view"], opt_in_view.FusionProgressPanelView)
        assert original_defer is not interaction.response.defer

    asyncio.run(_run())


def test_my_progress_diagnostics_include_ack_latency(monkeypatch):
    async def _run() -> None:
        interaction = _interaction()
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row()))
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=[_event_row()]))
        monkeypatch.setattr(fusion_sheets, "get_user_event_progress", AsyncMock(return_value={}))
        info = Mock()
        monkeypatch.setattr(opt_in_view.log, "info", info)

        view = opt_in_view.FusionOptInView()
        await _button(view, "fusion:my_progress").callback(interaction)

        response_logs = [
            call.kwargs["extra"]
            for call in info.call_args_list
            if call.args and call.args[0] == "fusion my-progress response path selected"
        ]
        assert len(response_logs) == 1
        assert response_logs[0]["response_path"] == "followup_send_ephemeral"
        assert response_logs[0]["response_done_before_send"] is True
        assert response_logs[0]["ack_elapsed_ms"] >= 0

    asyncio.run(_run())


def test_opt_in_button_acknowledges_before_fusion_lookup(monkeypatch):
    async def _run() -> None:
        role = SimpleNamespace(id=777)
        member = SimpleNamespace(
            id=10,
            display_name="Test User",
            guild=SimpleNamespace(id=1),
            roles=[],
            add_roles=AsyncMock(),
            remove_roles=AsyncMock(),
        )
        guild = SimpleNamespace(id=1, get_member=lambda _user_id: member, get_role=lambda _role_id: role)
        interaction = _interaction(user_id=member.id)
        interaction.guild = guild
        interaction.user = member

        async def _get_publishable_fusion():
            assert interaction.response.is_done() is True
            return _fusion_row()

        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", _get_publishable_fusion)

        view = opt_in_view.FusionOptInView()
        await _button(view, "fusion:opt_in").callback(interaction)

        interaction.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        member.add_roles.assert_awaited_once_with(role, reason="Fusion role opt-in button")
        interaction.followup.send.assert_awaited_once_with("Opted in. You’ll get fusion pings.", ephemeral=True)

    asyncio.run(_run())


def test_unknown_interaction_error_does_not_attempt_second_response(monkeypatch):
    async def _run() -> None:
        interaction = _interaction()
        alert = AsyncMock()
        monkeypatch.setattr(opt_in_view.fusion_logs, "send_ops_alert", alert)

        view = opt_in_view.FusionOptInView()
        item = SimpleNamespace(custom_id="fusion:my_progress")
        await view.on_error(interaction, _UnknownInteractionError("expired"), item)

        alert.assert_awaited_once()
        interaction.response.send_message.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()

    asyncio.run(_run())


def test_error_notification_unknown_interaction_is_suppressed(monkeypatch):
    async def _run() -> None:
        interaction = _interaction()
        interaction.response.send_message.side_effect = _UnknownInteractionError("expired while notifying")
        monkeypatch.setattr(opt_in_view.fusion_logs, "send_ops_alert", AsyncMock())

        view = opt_in_view.FusionOptInView()
        item = SimpleNamespace(custom_id="fusion:my_progress")
        await view.on_error(interaction, RuntimeError("handler failed"), item)

        interaction.response.send_message.assert_awaited_once_with("Temporary issue. Try again shortly.", ephemeral=True)

    asyncio.run(_run())
