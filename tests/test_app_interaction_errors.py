from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import app


class BlankInteractionError(Exception):
    def __str__(self) -> str:
        return ""


class FakeRuntime:
    def __init__(self) -> None:
        self.messages: list[str] = []

    async def send_log_message(self, message: str) -> None:
        self.messages.append(message)


def _raise(exc: Exception) -> None:
    raise exc


def _interaction(*, data: dict, command: object | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id=12345,
        type=SimpleNamespace(name="component"),
        user=SimpleNamespace(id=111),
        guild=SimpleNamespace(id=222),
        channel=SimpleNamespace(id=333),
        message=SimpleNamespace(id=444, content="do not log this message content"),
        data=data,
        command=command,
    )


def test_on_error_logs_traceback_metadata_for_interaction(monkeypatch, caplog):
    fake_runtime = FakeRuntime()
    monkeypatch.setattr(app, "runtime", fake_runtime, raising=False)
    caplog.set_level(logging.ERROR, logger="c1c.app")

    try:
        _raise(RuntimeError("boom"))
    except RuntimeError:
        asyncio.run(app.on_error("on_interaction", _interaction(data={"custom_id": "join_button"})))

    record = next(rec for rec in caplog.records if rec.message == "Unhandled exception in on_interaction")
    assert record.exception_type == "RuntimeError"
    assert record.exception_message == "boom"
    assert record.exception_origin_file.endswith("tests/test_app_interaction_errors.py")
    assert isinstance(record.exception_origin_line, int)
    assert record.exception_origin_function == "_raise"
    assert record.exception_trace_frames[-1]["function"] == "_raise"
    assert fake_runtime.messages[-1].startswith("⚠️ Interaction error")
    assert "origin=tests/test_app_interaction_errors.py:" in fake_runtime.messages[-1]


def test_on_interaction_logs_custom_id_for_component_failures(monkeypatch, caplog):
    monkeypatch.setattr(app, "runtime", FakeRuntime(), raising=False)
    caplog.set_level(logging.ERROR, logger="c1c.app")

    try:
        _raise(ValueError("component failed"))
    except ValueError:
        asyncio.run(app.on_error(
            "on_interaction",
            _interaction(data={"custom_id": "recruit:accept", "component_type": 2}),
        ))

    record = next(rec for rec in caplog.records if rec.message == "Unhandled exception in on_interaction")
    assert record.custom_id == "recruit:accept"
    assert record.component_type == 2
    assert record.interaction_user_id == 111
    assert record.guild_id == 222
    assert record.channel_id == 333
    assert record.message_id == 444


def test_on_interaction_logs_command_name_for_app_command_failures(monkeypatch, caplog):
    monkeypatch.setattr(app, "runtime", FakeRuntime(), raising=False)
    caplog.set_level(logging.ERROR, logger="c1c.app")
    command = SimpleNamespace(qualified_name="ops refresh", name="refresh")

    try:
        _raise(RuntimeError("slash failed"))
    except RuntimeError:
        asyncio.run(app.on_error(
            "on_interaction",
            _interaction(data={"name": "fallback"}, command=command),
        ))

    record = next(rec for rec in caplog.records if rec.message == "Unhandled exception in on_interaction")
    assert record.command_name == "ops refresh"


def test_on_interaction_does_not_log_message_content_or_modal_values(monkeypatch, caplog):
    fake_runtime = FakeRuntime()
    monkeypatch.setattr(app, "runtime", fake_runtime, raising=False)
    caplog.set_level(logging.ERROR, logger="c1c.app")
    secret_modal_value = "secret modal submitted value"
    message_content = "do not log this message content"

    try:
        _raise(RuntimeError("modal failed"))
    except RuntimeError:
        asyncio.run(app.on_error(
            "on_interaction",
            _interaction(
                data={
                    "custom_id": "modal:submit",
                    "components": [{"components": [{"custom_id": "field", "value": secret_modal_value}]}],
                }
            ),
        ))

    rendered_records = "\n".join(str(record.__dict__) for record in caplog.records)
    rendered_ops = "\n".join(fake_runtime.messages)
    assert secret_modal_value not in rendered_records
    assert message_content not in rendered_records
    assert secret_modal_value not in rendered_ops
    assert message_content not in rendered_ops


def test_blank_exception_messages_still_log_type_and_origin(monkeypatch, caplog):
    fake_runtime = FakeRuntime()
    monkeypatch.setattr(app, "runtime", fake_runtime, raising=False)
    caplog.set_level(logging.ERROR, logger="c1c.app")

    try:
        _raise(BlankInteractionError())
    except BlankInteractionError:
        asyncio.run(app.on_error("on_interaction", _interaction(data={"custom_id": "blank"})))

    record = next(rec for rec in caplog.records if rec.message == "Unhandled exception in on_interaction")
    assert record.exception_type == "BlankInteractionError"
    assert record.exception_origin_file.endswith("tests/test_app_interaction_errors.py")
    assert record.exception_origin_function == "_raise"
    assert "exception=BlankInteractionError" in fake_runtime.messages[-1]
