import asyncio
import datetime as dt
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from modules.community.fusion import opt_in_view
from shared.sheets import fusion as fusion_sheets


def _fusion_row(*, opt_in_role_id: int | None, fusion_type: str = "fragment") -> fusion_sheets.FusionRow:
    return fusion_sheets.FusionRow(
        fusion_id="f-1",
        fusion_name="Mavara",
        champion="Mavara",
        champion_image_url="",
        fusion_type=fusion_type,
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
        self.edit_message = AsyncMock()
        self.defer = AsyncMock(side_effect=self._defer)
        self._is_done = False

    def is_done(self) -> bool:
        return self._is_done

    async def _defer(self, **_kwargs) -> None:
        self._is_done = True


class _Member:
    def __init__(self, role):
        self.id = 10
        self.display_name = "Test User"
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
        client=SimpleNamespace(),
        response=_Response(),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def _event_row(
    event_id: str,
    *,
    event_name: str | None = None,
    start_at_utc: dt.datetime | None = None,
    end_at_utc: dt.datetime | None = None,
    sort_order: int = 1,
    reward_type: str = "fragments",
    reward_amount: float = 5.0,
) -> fusion_sheets.FusionEventRow:
    return fusion_sheets.FusionEventRow(
        fusion_id="f-1",
        event_id=event_id,
        event_name=event_name or f"Event {event_id}",
        event_type="dungeon",
        category="Tournaments",
        start_at_utc=start_at_utc or dt.datetime(2026, 4, 8, tzinfo=dt.timezone.utc),
        end_at_utc=end_at_utc or dt.datetime(2026, 4, 9, tzinfo=dt.timezone.utc),
        reward_amount=reward_amount,
        bonus=None,
        reward_type=reward_type,
        points_needed=None,
        is_estimated=False,
        sort_order=sort_order,
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


def test_my_progress_first_time_user_opens_panel(monkeypatch):
    async def _run() -> None:
        member = _Member(role=None)
        guild = _Guild(role=None, member=member)
        interaction = _interaction(guild, member)
        events = [_event_row("e1"), _event_row("e2")]
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row(opt_in_role_id=777)))
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=events))
        monkeypatch.setattr(fusion_sheets, "get_user_event_progress", AsyncMock(return_value={}))

        await opt_in_view._handle_my_progress(interaction)

        interaction.response.send_message.assert_awaited_once()
        sent_kwargs = interaction.response.send_message.await_args.kwargs
        view = sent_kwargs["view"]
        assert isinstance(view, opt_in_view.FusionProgressPanelView)
        assert view.progress_by_event == {}

    asyncio.run(_run())


def test_my_progress_prefills_saved_event_states(monkeypatch):
    async def _run() -> None:
        member = _Member(role=None)
        guild = _Guild(role=None, member=member)
        interaction = _interaction(guild, member)
        events = [_event_row("e1"), _event_row("e2")]
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row(opt_in_role_id=777)))
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=events))
        monkeypatch.setattr(
            fusion_sheets,
            "get_user_event_progress",
            AsyncMock(return_value={"progress": {"e1": "done", "e2": "in_progress", "missing": "done"}}),
        )

        await opt_in_view._handle_my_progress(interaction)

        sent_kwargs = interaction.response.send_message.await_args.kwargs
        view = sent_kwargs["view"]
        assert isinstance(view, opt_in_view.FusionProgressPanelView)
        assert view.progress_by_event == {"e1": "done", "e2": "in_progress"}

    asyncio.run(_run())


def test_my_progress_load_failure_still_opens_panel(monkeypatch):
    async def _run() -> None:
        member = _Member(role=None)
        guild = _Guild(role=None, member=member)
        interaction = _interaction(guild, member)
        events = [_event_row("e1")]
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row(opt_in_role_id=777)))
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=events))
        monkeypatch.setattr(fusion_sheets, "get_user_event_progress", AsyncMock(side_effect=RuntimeError("boom")))

        await opt_in_view._handle_my_progress(interaction)

        interaction.response.send_message.assert_awaited_once()
        sent_kwargs = interaction.response.send_message.await_args.kwargs
        view = sent_kwargs["view"]
        assert isinstance(view, opt_in_view.FusionProgressPanelView)
        assert view.progress_by_event == {}

    asyncio.run(_run())


def test_coerce_status_for_save_accepts_canonical_and_index_values():
    assert opt_in_view._coerce_status_for_save("not_started") == "not_started"
    assert opt_in_view._coerce_status_for_save("in_progress") == "in_progress"
    assert opt_in_view._coerce_status_for_save("2") == "done"
    assert opt_in_view._coerce_status_for_save("3") == "done_bonus"
    assert opt_in_view._coerce_status_for_save(4) == "skipped"
    assert opt_in_view._coerce_status_for_save("999") is None


def test_my_progress_panel_keeps_selected_event_in_sync_across_second_save(monkeypatch):
    async def _run() -> None:
        events = [_event_row("e1"), _event_row("e2")]
        view = opt_in_view.FusionProgressPanelView(
            user_id=10,
            target=_fusion_row(opt_in_role_id=777),
            events=events,
            progress_by_event={},
        )
        upsert_mock = AsyncMock()
        monkeypatch.setattr(fusion_sheets, "upsert_user_event_progress", upsert_mock)

        interaction = _interaction(guild=None, member=SimpleNamespace(id=10))

        event_select = next(item for item in view.children if item.custom_id == "fusion:progress:event")
        assert event_select.options[0].default is True
        assert event_select.options[1].default is False

        event_select._values = ["e1"]
        await event_select.callback(interaction)
        status_select = next(item for item in view.children if item.custom_id == "fusion:progress:status")
        status_select._values = ["done"]
        await status_select.callback(interaction)

        event_select = next(item for item in view.children if item.custom_id == "fusion:progress:event")
        event_select._values = ["e2"]
        await event_select.callback(interaction)

        assert view.selected_event_id == "e2"
        event_select = next(item for item in view.children if item.custom_id == "fusion:progress:event")
        defaults = {option.value: option.default for option in event_select.options}
        assert defaults == {"e1": False, "e2": True}

        status_select = next(item for item in view.children if item.custom_id == "fusion:progress:status")
        status_select._values = ["in_progress"]
        await status_select.callback(interaction)

        assert upsert_mock.await_count == 2
        first_call = upsert_mock.await_args_list[0].args
        second_call = upsert_mock.await_args_list[1].args
        assert first_call[2] == "e1"
        assert second_call[2] == "e2"

        embed = view.build_embed()
        selected_field = next(field for field in embed.fields if field.name == "Selected Event")
        assert "Event e2" in selected_field.value
        assert view.progress_by_event["e2"] == "in_progress"

    asyncio.run(_run())


def test_event_dropdown_uses_effective_status_icons_and_sort_order():
    now = dt.datetime(2026, 4, 21, tzinfo=dt.timezone.utc)
    future_start = dt.datetime(2026, 4, 25, tzinfo=dt.timezone.utc)
    future_end = dt.datetime(2026, 4, 26, tzinfo=dt.timezone.utc)
    past_start = dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc)
    past_end = dt.datetime(2026, 4, 2, tzinfo=dt.timezone.utc)
    events = [
        _event_row("e_done_ended", event_name="Done Ended", start_at_utc=past_start, end_at_utc=past_end),
        _event_row("e_not_started", event_name="Not Started", start_at_utc=future_start, end_at_utc=future_end),
        _event_row("e_in_progress", event_name="In Progress", start_at_utc=future_start, end_at_utc=future_end),
        _event_row("e_missed", event_name="Missed", start_at_utc=past_start, end_at_utc=past_end),
        _event_row("e_skipped", event_name="Skipped", start_at_utc=future_start, end_at_utc=future_end),
    ]
    progress_by_event = {
        "e_done_ended": "done",
        "e_in_progress": "in_progress",
        "e_skipped": "skipped",
    }

    select = opt_in_view._FusionProgressEventSelect(events, selected_event_id=None, progress_by_event=progress_by_event)
    labels = [option.label for option in select.options]
    assert "✅ Done Ended" in labels
    assert any(label.endswith("Not Started") for label in labels)
    assert any(label.endswith("In Progress") for label in labels)
    assert any(label.endswith("Missed") for label in labels)
    assert any(label.endswith("Skipped") for label in labels)

    assert (
        opt_in_view._effective_display_status(event=events[3], progress_by_event=progress_by_event, now=now) == "missed"
    )
    assert (
        opt_in_view._effective_display_status(event=events[0], progress_by_event=progress_by_event, now=now) == "done"
    )


def test_event_dropdown_keeps_selected_event_after_sorting_rebuild():
    past_start = dt.datetime(2026, 4, 1, tzinfo=dt.timezone.utc)
    past_end = dt.datetime(2026, 4, 2, tzinfo=dt.timezone.utc)
    future_start = dt.datetime(2026, 4, 25, tzinfo=dt.timezone.utc)
    future_end = dt.datetime(2026, 4, 26, tzinfo=dt.timezone.utc)
    events = [
        _event_row("e_done", event_name="Done", start_at_utc=past_start, end_at_utc=past_end),
        _event_row("e_missed", event_name="Missed", start_at_utc=past_start, end_at_utc=past_end),
        _event_row("e_not_started", event_name="Not Started", start_at_utc=future_start, end_at_utc=future_end),
    ]
    view = opt_in_view.FusionProgressPanelView(
        user_id=10,
        target=_fusion_row(opt_in_role_id=777),
        events=events,
        progress_by_event={"e_done": "done"},
    )
    view.selected_event_id = "e_missed"
    view.refresh_items()

    event_select = next(item for item in view.children if item.custom_id == "fusion:progress:event")
    defaults = {option.value: option.default for option in event_select.options}
    assert defaults["e_missed"] is True
    assert view.selected_event_id == "e_missed"


def test_reward_totals_and_selected_event_labels_are_data_driven():
    events = [
        _event_row("e_done", event_name="Base Done"),
        _event_row("e_done_bonus", event_name="Bonus Done"),
    ]
    events[1] = replace(events[1], reward_amount=25.0, bonus=50.0)
    progress_by_event = {"e_done": "done", "e_done_bonus": "done_bonus"}

    embed = opt_in_view._build_progress_summary_embed(
        target=_fusion_row(opt_in_role_id=777),
        events=events,
        progress_by_event=progress_by_event,
        selected_event_id="e_done_bonus",
    )

    progress_field = next(field for field in embed.fields if field.name == "\u200b")
    summary_field = next(field for field in embed.fields if field.name == "Summary")
    selected_field = next(field for field in embed.fields if field.name == "Selected Event")
    assert "80 acquired" in progress_field.value
    assert "✅ Done: 2" in summary_field.value
    assert "25 + 50 bonus fragments" in selected_field.value


def test_done_on_bonus_event_counts_base_only():
    event = replace(_event_row("e_bonus"), reward_amount=25.0, bonus=50.0)
    embed = opt_in_view._build_progress_summary_embed(
        target=_fusion_row(opt_in_role_id=777),
        events=[event],
        progress_by_event={"e_bonus": "done"},
        selected_event_id="e_bonus",
    )
    progress_field = next(field for field in embed.fields if field.name == "\u200b")
    assert "25 acquired" in progress_field.value


def test_my_progress_uses_tracker_reward_type_for_titan():
    event = replace(_event_row("e_points"), reward_amount=25.0, bonus=50.0, reward_type="points")
    titan = replace(_fusion_row(opt_in_role_id=777), reward_type="points", available=1750, fusion_type="titan")
    embed = opt_in_view._build_progress_summary_embed(
        target=titan,
        events=[event],
        progress_by_event={"e_points": "done_bonus"},
        selected_event_id="e_points",
    )

    points_field = next(field for field in embed.fields if field.name == "\u200b")
    selected_field = next(field for field in embed.fields if field.name == "Selected Event")
    assert "**Points Progress**" in points_field.value
    assert "75 acquired" in points_field.value
    assert "25 + 50 bonus points" in selected_field.value




def test_progress_summary_field_uses_inline_heading_and_needed_copy():
    embed = opt_in_view._build_progress_summary_embed(
        target=replace(_fusion_row(opt_in_role_id=777), needed=100, available=115, reward_type="fragments"),
        events=[replace(_event_row("e_done", event_name="Done"), reward_amount=35.0), replace(_event_row("e_skipped", event_name="Skipped"), reward_amount=5.0)],
        progress_by_event={"e_done": "done", "e_skipped": "skipped"},
    )

    progress_field = next(field for field in embed.fields if field.name == "\u200b")
    assert progress_field.value.startswith("**Fragment Progress**\n35 acquired\n5 skipped\n65 to go")
    assert "100 / 115 needed" in progress_field.value
    assert "still needed" not in progress_field.value
    assert "required" not in progress_field.value
def test_status_options_include_done_bonus_only_when_event_has_bonus():
    bonus_event = replace(_event_row("e_bonus"), bonus=10.0)
    plain_event = _event_row("e_plain")

    bonus_select = opt_in_view._FusionProgressStatusSelect("done_bonus", selected_event=bonus_event)
    plain_select = opt_in_view._FusionProgressStatusSelect("done", selected_event=plain_event)

    assert [option.value for option in bonus_select.options] == [
        "not_started",
        "in_progress",
        "done",
        "done_bonus",
        "skipped",
    ]
    assert [option.value for option in plain_select.options] == [
        "not_started",
        "in_progress",
        "done",
        "skipped",
    ]


def test_my_progress_share_button_opens_share_mode_panel():
    async def _run() -> None:
        events = [_event_row("e1")]
        view = opt_in_view.FusionProgressPanelView(
            user_id=10,
            target=_fusion_row(opt_in_role_id=777),
            events=events,
            progress_by_event={},
        )
        interaction = _interaction(guild=None, member=SimpleNamespace(id=10, display_name="Tester"))
        share_button = next(item for item in view.children if item.custom_id == "fusion:progress:share")

        await share_button.callback(interaction)

        interaction.response.send_message.assert_awaited_once()
        kwargs = interaction.response.send_message.await_args.kwargs
        assert isinstance(kwargs["view"], opt_in_view.FusionProgressShareModeView)
        assert kwargs["ephemeral"] is True

    asyncio.run(_run())


def test_share_mode_summary_posts_to_fusion_announcement_channel(monkeypatch):
    async def _run() -> None:
        events = [_event_row("e1", event_name="Dungeon Dash")]
        channel = SimpleNamespace(send=AsyncMock())
        member = SimpleNamespace(id=10, display_name="Tester")
        interaction = _interaction(guild=None, member=member)

        view = opt_in_view.FusionProgressShareModeView(
            user_id=10,
            target=_fusion_row(opt_in_role_id=777),
            events=events,
            progress_by_event={"e1": "done"},
        )
        monkeypatch.setattr(opt_in_view.fusion_announcements, "resolve_announcement_channel", AsyncMock(return_value=channel))
        summary_button = next(item for item in view.children if item.custom_id == "fusion:progress:share:summary")

        await summary_button.callback(interaction)

        channel.send.assert_awaited_once()
        embed = channel.send.await_args.kwargs["embed"]
        assert embed.title == "Progress Share: Mavara"
        summary_field = next(field for field in embed.fields if field.name == "Summary")
        assert "✅ Done: 1" in summary_field.value

    asyncio.run(_run())


def test_mark_all_button_hidden_for_non_milestone_event():
    events = [_event_row("e1")]
    view = opt_in_view.FusionProgressPanelView(
        user_id=10,
        target=_fusion_row(opt_in_role_id=777),
        events=events,
        progress_by_event={},
    )
    custom_ids = [item.custom_id for item in view.children]
    assert "fusion:progress:mark_all" not in custom_ids


def test_mark_all_button_shown_for_milestone_event():
    milestone = fusion_sheets.FusionEventMilestone(points_needed=2050, reward_amount=5.0)
    events = [replace(_event_row("e1"), milestones=(milestone,))]
    view = opt_in_view.FusionProgressPanelView(
        user_id=10,
        target=_fusion_row(opt_in_role_id=777),
        events=events,
        progress_by_event={},
    )
    custom_ids = [item.custom_id for item in view.children]
    assert "fusion:progress:mark_all" in custom_ids


def test_my_progress_direct_send_logs_mobile_diagnostics(monkeypatch):
    async def _run() -> None:
        member = _Member(role=None)
        guild = _Guild(role=None, member=member)
        interaction = _interaction(guild, member)
        events = [_event_row("e1"), _event_row("e2")]
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row(opt_in_role_id=777)))
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=events))
        monkeypatch.setattr(fusion_sheets, "get_user_event_progress", AsyncMock(return_value={}))
        info_mock = Mock()
        monkeypatch.setattr(opt_in_view.log, "info", info_mock)

        await opt_in_view._handle_my_progress(interaction)

        interaction.response.send_message.assert_awaited_once()
        assert interaction.response.send_message.await_args.kwargs["ephemeral"] is True
        interaction.followup.send.assert_not_awaited()
        info_mock.assert_called()
        diagnostics = info_mock.call_args.kwargs["extra"]
        assert diagnostics["response_path"] == "direct_send_ephemeral"
        assert diagnostics["event_count"] == 2
        assert diagnostics["event_options_visible"] == 2
        assert diagnostics["component_count"] == 3

    asyncio.run(_run())


def test_my_progress_event_select_is_paginated_for_mobile():
    events = [_event_row(f"e{i:02d}", event_name=f"Event {i:02d}") for i in range(12)]
    view = opt_in_view.FusionProgressPanelView(
        user_id=10,
        target=_fusion_row(opt_in_role_id=777),
        events=events,
        progress_by_event={},
    )

    event_select = next(item for item in view.children if item.custom_id == "fusion:progress:event")
    custom_ids = [item.custom_id for item in view.children]
    assert len(event_select.options) == 10
    assert event_select.placeholder == "Choose event (page 1/2)"
    assert "fusion:progress:page:previous" in custom_ids
    assert "fusion:progress:page:next" in custom_ids


def test_my_progress_next_page_rebuilds_smaller_event_options():
    async def _run() -> None:
        events = [_event_row(f"e{i:02d}", event_name=f"Event {i:02d}") for i in range(12)]
        view = opt_in_view.FusionProgressPanelView(
            user_id=10,
            target=_fusion_row(opt_in_role_id=777),
            events=events,
            progress_by_event={},
        )
        interaction = _interaction(guild=None, member=SimpleNamespace(id=10))
        next_button = next(item for item in view.children if item.custom_id == "fusion:progress:page:next")

        await next_button.callback(interaction)

        assert view.event_page_index == 1
        assert view.selected_event_id == "e10"
        event_select = next(item for item in view.children if item.custom_id == "fusion:progress:event")
        assert [option.value for option in event_select.options] == ["e10", "e11"]
        assert event_select.placeholder == "Choose event (page 2/2)"

    asyncio.run(_run())


def test_traditional_my_progress_opens_choice_view(monkeypatch):
    async def _run() -> None:
        member = _Member(role=None)
        guild = _Guild(role=None, member=member)
        interaction = _interaction(guild, member)
        events = [_event_row("e1")]
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row(opt_in_role_id=777, fusion_type="traditional")))
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=events))
        monkeypatch.setattr(fusion_sheets, "get_user_event_progress", AsyncMock(return_value={"progress": {"e1": "done"}}))

        await opt_in_view._handle_my_progress(interaction)

        sent_kwargs = interaction.response.send_message.await_args.kwargs
        assert isinstance(sent_kwargs["view"], opt_in_view.TraditionalProgressChoiceView)
        assert "Event/Tournament Progress" in sent_kwargs["embed"].fields[0].value
        assert "Champion Preparation" in sent_kwargs["embed"].fields[0].value

    asyncio.run(_run())


def test_traditional_my_progress_uses_followup_when_interaction_already_acknowledged(monkeypatch):
    async def _run() -> None:
        member = _Member(role=None)
        guild = _Guild(role=None, member=member)
        interaction = _interaction(guild, member)
        interaction.response._is_done = True
        events = [_event_row("e1")]
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row(opt_in_role_id=777, fusion_type="traditional")))
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=events))
        monkeypatch.setattr(fusion_sheets, "get_user_event_progress", AsyncMock(return_value={"progress": {"e1": "done"}}))

        await opt_in_view._handle_my_progress(interaction)

        interaction.response.send_message.assert_not_awaited()
        interaction.response.edit_message.assert_not_awaited()
        interaction.response.defer.assert_not_awaited()
        interaction.followup.send.assert_awaited_once()
        sent_kwargs = interaction.followup.send.await_args.kwargs
        assert sent_kwargs["ephemeral"] is True
        assert isinstance(sent_kwargs["view"], opt_in_view.TraditionalProgressChoiceView)
        assert "Event/Tournament Progress" in sent_kwargs["embed"].fields[0].value
        assert "Champion Preparation" in sent_kwargs["embed"].fields[0].value

    asyncio.run(_run())


def test_non_traditional_my_progress_uses_followup_when_interaction_already_acknowledged(monkeypatch):
    async def _run() -> None:
        member = _Member(role=None)
        guild = _Guild(role=None, member=member)
        interaction = _interaction(guild, member)
        interaction.response._is_done = True
        events = [_event_row("e1")]
        monkeypatch.setattr(fusion_sheets, "get_publishable_fusion", AsyncMock(return_value=_fusion_row(opt_in_role_id=777)))
        monkeypatch.setattr(fusion_sheets, "get_fusion_events", AsyncMock(return_value=events))
        monkeypatch.setattr(fusion_sheets, "get_user_event_progress", AsyncMock(return_value={"progress": {"e1": "in_progress"}}))

        await opt_in_view._handle_my_progress(interaction)

        interaction.response.send_message.assert_not_awaited()
        interaction.response.edit_message.assert_not_awaited()
        interaction.response.defer.assert_not_awaited()
        interaction.followup.send.assert_awaited_once()
        sent_kwargs = interaction.followup.send.await_args.kwargs
        assert sent_kwargs["ephemeral"] is True
        assert isinstance(sent_kwargs["view"], opt_in_view.FusionProgressPanelView)
        assert sent_kwargs["view"].progress_by_event == {"e1": "in_progress"}

    asyncio.run(_run())


def test_traditional_prep_validation_blocks_impossible_counts():
    message = opt_in_view._validate_traditional_prep_counts(
        needed_total=16,
        rares_acquired=8,
        rares_level_40=8,
        rares_ascended=4,
        epics_fused=2,
        epics_level_50=0,
        epics_ascended=0,
    )

    assert message == "Epics fused cannot be higher than the number allowed by fully ascended Rares."



def test_fusion_my_progress_views_do_not_expire():
    target = _fusion_row(opt_in_role_id=777, fusion_type="traditional")
    events = [_event_row("e1")]
    progress_by_event = {"e1": "done"}
    prep = fusion_sheets.FusionTraditionalUserProgressRow(fusion_id="f-1", user_id="42")

    choice_view = opt_in_view.TraditionalProgressChoiceView(
        user_id=42,
        target=target,
        events=events,
        progress_by_event=progress_by_event,
        partial_by_event={},
    )
    prep_view = opt_in_view.TraditionalPrepPanelView(
        user_id=42,
        target=target,
        events=events,
        progress_by_event=progress_by_event,
        prep=prep,
    )
    event_view = opt_in_view.FusionProgressPanelView(
        user_id=42,
        target=target,
        events=events,
        progress_by_event=progress_by_event,
    )
    share_view = opt_in_view.FusionProgressShareModeView(
        user_id=42,
        target=target,
        events=events,
        progress_by_event=progress_by_event,
        partial_by_event={},
    )

    assert choice_view.timeout is None
    assert prep_view.timeout is None
    assert event_view.timeout is None
    assert share_view.timeout is None



def test_traditional_prep_modal_save_edits_original_panel(monkeypatch):
    async def _run() -> None:
        member = _Member(role=None)
        guild = _Guild(role=None, member=member)
        interaction = _interaction(guild, member)
        target = _fusion_row(opt_in_role_id=777, fusion_type="traditional")
        events = [_event_row("e1", reward_type="rare", reward_amount=16)]
        progress_by_event = {"e1": "done"}
        prep = fusion_sheets.FusionTraditionalUserProgressRow(fusion_id="f-1", user_id=str(member.id))
        panel = opt_in_view.TraditionalPrepPanelView(
            user_id=member.id,
            target=target,
            events=events,
            progress_by_event=progress_by_event,
            prep=prep,
        )
        modal = opt_in_view._TraditionalPrepModal(view=panel)
        for item, value in (
            (modal.rares_level_40, "16"),
            (modal.rares_ascended, "16"),
            (modal.epics_fused, "4"),
            (modal.epics_level_50, "4"),
            (modal.epics_ascended, "4"),
        ):
            item._value = value

        saved = fusion_sheets.FusionTraditionalUserProgressRow(
            fusion_id="f-1",
            user_id=str(member.id),
            rares_level_40=16,
            rares_ascended=16,
            epics_fused=4,
            epics_level_50=4,
            epics_ascended=4,
        )
        monkeypatch.setattr(fusion_sheets, "upsert_user_traditional_progress", AsyncMock(return_value=saved))

        await modal.on_submit(interaction)

        interaction.response.defer.assert_awaited_once_with(thinking=False)
        interaction.edit_original_response.assert_awaited_once()
        interaction.response.edit_message.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()
        assert panel.prep.epics_ascended == 4

    asyncio.run(_run())


def _button_by_label(view, label: str):
    for child in view.children:
        if getattr(child, "label", None) == label:
            return child
    raise AssertionError(f"button {label!r} not found")


def test_traditional_prep_back_edits_to_choice_panel():
    async def _run() -> None:
        member = _Member(role=None)
        guild = _Guild(role=None, member=member)
        interaction = _interaction(guild, member)
        target = _fusion_row(opt_in_role_id=777, fusion_type="traditional")
        events = [_event_row("e1")]
        progress_by_event = {"e1": "in_progress"}
        partial_by_event = {"e1": 2.0}
        prep = fusion_sheets.FusionTraditionalUserProgressRow(fusion_id="f-1", user_id=str(member.id))
        view = opt_in_view.TraditionalPrepPanelView(
            user_id=member.id,
            target=target,
            events=events,
            progress_by_event=progress_by_event,
            partial_by_event=partial_by_event,
            prep=prep,
        )

        assert [getattr(child, "label", None) for child in view.children] == ["Update Champion Prep", "Back"]

        await _button_by_label(view, "Back").callback(interaction)

        interaction.response.edit_message.assert_awaited_once()
        interaction.response.send_message.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert isinstance(kwargs["view"], opt_in_view.TraditionalProgressChoiceView)
        assert kwargs["view"].partial_by_event == partial_by_event
        assert kwargs["embed"].title == "My Progress: Mavara"

    asyncio.run(_run())


def test_traditional_event_progress_has_back_and_back_edits_to_choice_panel():
    async def _run() -> None:
        member = _Member(role=None)
        guild = _Guild(role=None, member=member)
        interaction = _interaction(guild, member)
        target = _fusion_row(opt_in_role_id=777, fusion_type="traditional")
        events = [_event_row("e1")]
        progress_by_event = {"e1": "in_progress"}
        partial_by_event = {"e1": 2.0}
        view = opt_in_view.FusionProgressPanelView(
            user_id=member.id,
            target=target,
            events=events,
            progress_by_event=progress_by_event,
            partial_by_event=partial_by_event,
            return_to_traditional_choice=True,
        )

        assert _button_by_label(view, "Back") is not None
        await _button_by_label(view, "Back").callback(interaction)

        interaction.response.edit_message.assert_awaited_once()
        interaction.response.send_message.assert_not_awaited()
        interaction.followup.send.assert_not_awaited()
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert isinstance(kwargs["view"], opt_in_view.TraditionalProgressChoiceView)
        assert kwargs["view"].partial_by_event == partial_by_event

    asyncio.run(_run())



def test_traditional_choice_event_progress_opens_back_enabled_panel():
    async def _run() -> None:
        member = _Member(role=None)
        guild = _Guild(role=None, member=member)
        interaction = _interaction(guild, member)
        target = _fusion_row(opt_in_role_id=777, fusion_type="traditional")
        partial_by_event = {"e1": 2.0}
        view = opt_in_view.TraditionalProgressChoiceView(
            user_id=member.id,
            target=target,
            events=[_event_row("e1")],
            progress_by_event={"e1": "in_progress"},
            partial_by_event=partial_by_event,
        )

        await _button_by_label(view, "Event/Tournament Progress").callback(interaction)

        interaction.response.edit_message.assert_awaited_once()
        kwargs = interaction.response.edit_message.await_args.kwargs
        assert isinstance(kwargs["view"], opt_in_view.FusionProgressPanelView)
        assert kwargs["view"].return_to_traditional_choice is True
        assert kwargs["view"].partial_by_event == partial_by_event
        assert _button_by_label(kwargs["view"], "Back") is not None

    asyncio.run(_run())


def test_non_traditional_event_progress_does_not_show_back():
    target = _fusion_row(opt_in_role_id=777)
    view = opt_in_view.FusionProgressPanelView(
        user_id=42,
        target=target,
        events=[_event_row("e1")],
        progress_by_event={},
    )

    assert all(getattr(child, "label", None) != "Back" for child in view.children)
