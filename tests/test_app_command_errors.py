from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from discord.ext import commands

import app
from shared.sheets import help_commands


def _row(*, bot_key: str, command: str = "responder") -> help_commands.HelpCommandRow:
    return help_commands.HelpCommandRow(
        bot_key=bot_key,
        command_key=command,
        command=f"!{command}",
        usage=f"!{command}",
        category="Test",
        access_level="user",
        summary="Test command",
        details="",
        sort_order=1,
        source_order=0,
    )


def _ctx(invoked_with: str = "responder") -> SimpleNamespace:
    return SimpleNamespace(
        command=None,
        invoked_with=invoked_with,
        author=SimpleNamespace(id=123),
        guild=None,
    )


@pytest.mark.parametrize(
    "owners",
    [
        ("reminder",),
        ("achievements",),
        ("reminder", "achievements"),
    ],
)
async def test_known_external_command_is_ignored(monkeypatch, owners) -> None:
    monkeypatch.setattr(
        app.help_commands,
        "get_rows",
        AsyncMock(return_value=tuple(_row(bot_key=owner) for owner in owners)),
    )
    send_log_message = AsyncMock()
    monkeypatch.setattr(app.runtime, "send_log_message", send_log_message)

    await app.on_command_error(_ctx(), commands.CommandNotFound("not registered"))

    send_log_message.assert_not_awaited()


@pytest.mark.parametrize(
    "rows",
    [
        (_row(bot_key="woadkeeper"),),
        (_row(bot_key="reminder"), _row(bot_key="woadkeeper")),
        (_row(bot_key=""),),
        (_row(bot_key="reminder", command="different"),),
        (),
        None,
    ],
)
async def test_command_not_found_is_reported_when_ownership_is_not_external_only(
    monkeypatch, rows
) -> None:
    monkeypatch.setattr(
        app.help_commands,
        "get_rows",
        AsyncMock(return_value=rows),
    )
    send_log_message = AsyncMock()
    monkeypatch.setattr(app.runtime, "send_log_message", send_log_message)

    await app.on_command_error(_ctx(), commands.CommandNotFound("not registered"))

    send_log_message.assert_awaited_once()


async def test_command_not_found_is_reported_when_help_lookup_fails(monkeypatch) -> None:
    monkeypatch.setattr(
        app.help_commands,
        "get_rows",
        AsyncMock(side_effect=RuntimeError("sheet unavailable")),
    )
    send_log_message = AsyncMock()
    monkeypatch.setattr(app.runtime, "send_log_message", send_log_message)

    await app.on_command_error(_ctx(), commands.CommandNotFound("not registered"))

    send_log_message.assert_awaited_once()


async def test_non_command_not_found_errors_keep_existing_reporting(monkeypatch) -> None:
    get_rows = AsyncMock()
    monkeypatch.setattr(app.help_commands, "get_rows", get_rows)
    send_log_message = AsyncMock()
    monkeypatch.setattr(app.runtime, "send_log_message", send_log_message)
    ctx = _ctx()
    ctx.command = SimpleNamespace(name="responder")

    await app.on_command_error(ctx, RuntimeError("boom"))

    get_rows.assert_not_awaited()
    send_log_message.assert_awaited_once()
