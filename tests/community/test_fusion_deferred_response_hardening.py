import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import discord

from modules.community.fusion import deferred_response_hardening
from modules.community.fusion import opt_in_view


def run(awaitable):
    return asyncio.run(awaitable)


class _Response:
    def __init__(self, *, done: bool) -> None:
        self._done = done
        self.send_message = AsyncMock()

    def is_done(self) -> bool:
        return self._done


def _interaction(*, done: bool):
    return SimpleNamespace(
        response=_Response(done=done),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def _duplicate_select_view() -> discord.ui.View:
    view = discord.ui.View()
    view.add_item(
        discord.ui.Select(
            custom_id="fusion:test:duplicate",
            options=[
                discord.SelectOption(label="Champion Training I", value="CT_1"),
                discord.SelectOption(label="Champion Training II", value="CT_1"),
            ],
        )
    )
    return view


def test_deferred_progress_response_edits_original_instead_of_followup():
    interaction = _interaction(done=True)
    embed = discord.Embed(title="Progress")
    view = discord.ui.View()

    run(
        deferred_response_hardening._send_or_edit_ephemeral(
            interaction,
            embed=embed,
            view=view,
        )
    )

    interaction.edit_original_response.assert_awaited_once_with(
        embed=embed,
        view=view,
    )
    interaction.followup.send.assert_not_awaited()
    interaction.response.send_message.assert_not_awaited()


def test_non_deferred_progress_response_still_uses_initial_response():
    interaction = _interaction(done=False)
    embed = discord.Embed(title="Progress")
    view = discord.ui.View()

    run(
        deferred_response_hardening._send_or_edit_ephemeral(
            interaction,
            embed=embed,
            view=view,
        )
    )

    interaction.response.send_message.assert_awaited_once_with(
        content=None,
        embed=embed,
        view=view,
        ephemeral=True,
    )
    interaction.edit_original_response.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()


def test_duplicate_select_values_are_detected():
    assert deferred_response_hardening._duplicate_select_option_values(
        _duplicate_select_view()
    ) == ("CT_1",)


def test_duplicate_select_payload_is_blocked_before_discord_edit(monkeypatch):
    interaction = _interaction(done=True)
    error_log = Mock()
    monkeypatch.setattr(deferred_response_hardening.log, "error", error_log)

    run(
        deferred_response_hardening._send_or_edit_ephemeral(
            interaction,
            embed=discord.Embed(title="Progress"),
            view=_duplicate_select_view(),
        )
    )

    interaction.edit_original_response.assert_awaited_once_with(
        content=deferred_response_hardening._DUPLICATE_EVENT_MESSAGE
    )
    interaction.followup.send.assert_not_awaited()
    interaction.response.send_message.assert_not_awaited()
    extra = error_log.call_args.kwargs["extra"]
    assert extra["duplicate_select_option_values"] == ("CT_1",)


def test_duplicate_select_payload_is_blocked_before_initial_response():
    interaction = _interaction(done=False)

    run(
        deferred_response_hardening._send_or_edit_ephemeral(
            interaction,
            embed=discord.Embed(title="Progress"),
            view=_duplicate_select_view(),
        )
    )

    interaction.response.send_message.assert_awaited_once_with(
        content=deferred_response_hardening._DUPLICATE_EVENT_MESSAGE,
        embed=None,
        view=None,
        ephemeral=True,
    )
    interaction.edit_original_response.assert_not_awaited()
    interaction.followup.send.assert_not_awaited()


def test_install_replaces_fusion_response_boundary(monkeypatch):
    original = opt_in_view._send_or_followup_ephemeral
    monkeypatch.setattr(deferred_response_hardening, "_installed", False)
    try:
        deferred_response_hardening.install()
        assert opt_in_view._send_or_followup_ephemeral is deferred_response_hardening._send_or_edit_ephemeral
    finally:
        opt_in_view._send_or_followup_ephemeral = original
        deferred_response_hardening._installed = False


def test_50035_edit_failure_logs_full_discord_error_text(monkeypatch):
    class _InvalidFormBody(Exception):
        code = 50035

    interaction = _interaction(done=True)
    interaction.edit_original_response.side_effect = _InvalidFormBody(
        "400 Bad Request (error code: 50035): Invalid Form Body In components.0"
    )
    error_log = Mock()
    monkeypatch.setattr(deferred_response_hardening.log, "error", error_log)

    try:
        run(
            deferred_response_hardening._send_or_edit_ephemeral(
                interaction,
                embed=discord.Embed(title="Progress"),
                view=discord.ui.View(),
            )
        )
    except _InvalidFormBody:
        pass
    else:
        raise AssertionError("expected Invalid Form Body to propagate")

    extra = error_log.call_args.kwargs["extra"]
    assert extra["discord_error_code"] == 50035
    assert "Invalid Form Body" in extra["discord_error_text"]
