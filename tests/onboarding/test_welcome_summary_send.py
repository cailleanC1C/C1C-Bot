import asyncio
from types import SimpleNamespace

import discord

from modules.onboarding.controllers import welcome_controller


class DummyThread:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.raise_on_send: Exception | None = None

    async def send(self, **kwargs: object) -> object:
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sent.append(kwargs)
        return SimpleNamespace(**kwargs)


def test_send_welcome_summary_safe_success(monkeypatch: object) -> None:
    controller = welcome_controller.WelcomeController(SimpleNamespace())
    embed = discord.Embed(title="ok")
    monkeypatch.setattr(welcome_controller, "build_summary_embed", lambda **_: embed)
    thread = DummyThread()

    success = asyncio.run(
        controller._send_welcome_summary_safe(
            thread=thread,
            answers={"foo": "bar"},
            author=None,
            schema_hash="hash",
            visibility=None,
            content="hi",
        )
    )

    assert success is True
    assert thread.sent == [{"content": "hi", "embed": embed, "allowed_mentions": None}]


def test_send_welcome_summary_safe_build_failure(monkeypatch: object) -> None:
    controller = welcome_controller.WelcomeController(SimpleNamespace())
    fallback = discord.Embed(title="fallback")
    monkeypatch.setattr(
        welcome_controller,
        "build_summary_embed",
        lambda **_: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(welcome_controller, "_fallback_welcome_embed", lambda _author: fallback)
    thread = DummyThread()

    success = asyncio.run(
        controller._send_welcome_summary_safe(
            thread=thread,
            answers={},
            author=None,
            schema_hash="hash",
            visibility=None,
        )
    )

    assert success is True
    assert thread.sent == [{"content": None, "embed": fallback, "allowed_mentions": None}]


def test_send_welcome_summary_safe_send_failure(monkeypatch: object) -> None:
    controller = welcome_controller.WelcomeController(SimpleNamespace())
    embed = discord.Embed(title="ok")
    monkeypatch.setattr(welcome_controller, "build_summary_embed", lambda **_: embed)
    thread = DummyThread()
    thread.raise_on_send = RuntimeError("boom")

    success = asyncio.run(
        controller._send_welcome_summary_safe(
            thread=thread,
            answers={},
            author=None,
            schema_hash="hash",
            visibility=None,
        )
    )

    assert success is False
    assert thread.sent == []


class GuardedMessage(SimpleNamespace):
    def __init__(self, *, id: int, author_id: int = 111, embeds: list[discord.Embed] | None = None, content: str | None = None) -> None:
        super().__init__(id=id, author=SimpleNamespace(id=author_id), embeds=embeds or [], content=content or "")
        self.edits: list[dict[str, object]] = []
        self.raise_on_edit: Exception | None = None

    async def edit(self, **kwargs: object) -> None:
        if self.raise_on_edit is not None:
            raise self.raise_on_edit
        self.edits.append(kwargs)


class GuardedThread(DummyThread):
    def __init__(self) -> None:
        super().__init__()
        self.id = 456
        self.messages: dict[int, GuardedMessage] = {}
        self.next_id = 900
        self.raise_on_fetch: Exception | None = None
        self.history_messages: list[GuardedMessage] = []

    async def send(self, **kwargs: object) -> object:
        if self.raise_on_send is not None:
            raise self.raise_on_send
        self.sent.append(kwargs)
        message_id = self.next_id
        self.next_id += 1
        message = GuardedMessage(
            id=message_id,
            author_id=111,
            embeds=[kwargs["embed"]] if isinstance(kwargs.get("embed"), discord.Embed) else [],
            content=str(kwargs.get("content") or ""),
        )
        self.messages[message_id] = message
        self.history_messages.insert(0, message)
        return message

    async def fetch_message(self, message_id: int) -> object:
        if self.raise_on_fetch is not None:
            raise self.raise_on_fetch
        try:
            return self.messages[int(message_id)]
        except KeyError as exc:
            raise _discord_not_found() from exc

    def history(self, *, limit: int = 50):
        async def iterator():
            for message in self.history_messages[:limit]:
                yield message

        return iterator()


def _discord_not_found() -> discord.NotFound:
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "missing")


def _discord_forbidden() -> discord.Forbidden:
    return discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "forbidden")


def _discord_http_error() -> discord.HTTPException:
    return discord.HTTPException(SimpleNamespace(status=500, reason="Server Error"), "boom")


def _summary_embed(title: str = "C1C • Recruitment Summary") -> discord.Embed:
    return discord.Embed(title=title)


def _install_summary_fakes(monkeypatch: object, row: dict[str, object] | None = None) -> tuple[dict[str, object], list[dict[str, object]]]:
    state = dict(row or {"thread_id": "456", "step_index": 15})
    updates: list[dict[str, object]] = []

    def fake_get_by_thread_id(thread_id: int) -> dict[str, object]:
        assert thread_id == 456
        return dict(state)

    def fake_update_existing(thread_id: int, payload: dict[str, object]) -> bool:
        assert thread_id == 456
        state.update(payload)
        updates.append(dict(payload))
        return True

    monkeypatch.setattr(welcome_controller.onboarding_sessions, "get_by_thread_id", fake_get_by_thread_id)
    monkeypatch.setattr(welcome_controller.onboarding_sessions, "update_existing", fake_update_existing)
    monkeypatch.setattr(welcome_controller.onboarding_sessions, "missing_columns", lambda columns: set())
    return state, updates


def _controller_and_thread(monkeypatch: object, *, row: dict[str, object] | None = None) -> tuple[welcome_controller.WelcomeController, GuardedThread, dict[str, object], list[dict[str, object]], discord.Embed]:
    controller = welcome_controller.WelcomeController(SimpleNamespace(user=SimpleNamespace(id=111, name="The Woadkeeper")))
    embed = _summary_embed()
    monkeypatch.setattr(welcome_controller, "build_summary_embed", lambda **_: embed)
    thread = GuardedThread()
    state, updates = _install_summary_fakes(monkeypatch, row)
    return controller, thread, state, updates, embed


async def _send_summary(controller: welcome_controller.WelcomeController, thread: GuardedThread) -> bool:
    return await controller._send_welcome_summary_safe(
        thread=thread,
        answers={"player_id": "97909413"},
        author=None,
        schema_hash="hash",
        visibility=None,
    )


def test_summary_id_exists_fetch_succeeds_edits_without_send(monkeypatch: object) -> None:
    controller, thread, state, updates, embed = _controller_and_thread(
        monkeypatch,
        row={"thread_id": "456", "step_index": 15, "recruiter_summary_message_id": "777"},
    )
    existing = GuardedMessage(id=777, author_id=111, embeds=[_summary_embed()])
    thread.messages[777] = existing

    success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert thread.sent == []
    assert existing.edits[-1]["embed"] is embed
    assert updates[-1]["recruiter_summary_message_id"] == "777"
    assert state["recruiter_summary_player_id"] == "97909413"


def test_summary_id_not_found_discovers_existing_and_persists(monkeypatch: object) -> None:
    controller, thread, state, updates, embed = _controller_and_thread(
        monkeypatch,
        row={"thread_id": "456", "step_index": 15, "recruiter_summary_message_id": "777"},
    )
    discovered = GuardedMessage(id=778, author_id=111, embeds=[_summary_embed()])
    thread.history_messages.append(discovered)

    success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert thread.sent == []
    assert discovered.edits[-1]["embed"] is embed
    assert updates[-1]["recruiter_summary_message_id"] == "778"
    assert state["recruiter_summary_message_id"] == "778"


def test_summary_id_not_found_sends_once_when_thread_has_none(monkeypatch: object) -> None:
    controller, thread, state, updates, _embed = _controller_and_thread(
        monkeypatch,
        row={"thread_id": "456", "step_index": 15, "recruiter_summary_message_id": "777"},
    )

    success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert len(thread.sent) == 1
    assert updates[-1]["recruiter_summary_message_id"] == "900"
    assert state["recruiter_summary_message_id"] == "900"


def test_missing_summary_id_discovers_existing_and_does_not_duplicate(monkeypatch: object) -> None:
    controller, thread, state, updates, embed = _controller_and_thread(monkeypatch)
    discovered = GuardedMessage(id=801, author_id=111, embeds=[_summary_embed()])
    thread.history_messages.append(discovered)

    success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert thread.sent == []
    assert discovered.edits[-1]["embed"] is embed
    assert updates[-1]["recruiter_summary_message_id"] == "801"
    assert state["recruiter_summary_message_id"] == "801"


def test_missing_summary_id_and_no_existing_summary_sends_and_persists(monkeypatch: object) -> None:
    controller, thread, state, updates, _embed = _controller_and_thread(monkeypatch)

    success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert len(thread.sent) == 1
    assert updates[-1]["recruiter_summary_message_id"] == "900"
    assert state["recruiter_summary_message_id"] == "900"


def test_recorded_fetch_forbidden_blocks_duplicate(monkeypatch: object, caplog) -> None:
    controller, thread, _state, updates, _embed = _controller_and_thread(
        monkeypatch,
        row={"thread_id": "456", "step_index": 15, "recruiter_summary_message_id": "777"},
    )
    thread.raise_on_fetch = _discord_forbidden()

    with caplog.at_level("WARNING"):
        success = asyncio.run(_send_summary(controller, thread))

    assert success is False
    assert thread.sent == []
    assert updates == []
    assert "recorded_edit_failed" in caplog.text


def test_recorded_fetch_http_exception_blocks_duplicate(monkeypatch: object, caplog) -> None:
    controller, thread, _state, updates, _embed = _controller_and_thread(
        monkeypatch,
        row={"thread_id": "456", "step_index": 15, "recruiter_summary_message_id": "777"},
    )
    thread.raise_on_fetch = _discord_http_error()

    with caplog.at_level("WARNING"):
        success = asyncio.run(_send_summary(controller, thread))

    assert success is False
    assert thread.sent == []
    assert updates == []
    assert "recorded_edit_failed" in caplog.text


def test_concurrent_summary_tasks_post_only_one_message(monkeypatch: object) -> None:
    controller, thread, state, updates, _embed = _controller_and_thread(monkeypatch)

    async def runner() -> list[bool]:
        return await asyncio.gather(_send_summary(controller, thread), _send_summary(controller, thread))

    results = asyncio.run(runner())

    assert results == [True, True]
    assert len(thread.sent) == 1
    assert state["recruiter_summary_message_id"] == "900"
    assert updates[-1]["recruiter_summary_message_id"] == "900"


def test_missing_metadata_headers_logs_warning_without_claiming_persist(monkeypatch: object, caplog) -> None:
    controller, thread, state, updates, _embed = _controller_and_thread(monkeypatch)
    monkeypatch.setattr(
        welcome_controller.onboarding_sessions,
        "missing_columns",
        lambda columns: {"recruiter_summary_message_id"},
    )

    with caplog.at_level("WARNING"):
        success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert len(thread.sent) == 1
    assert updates == []
    assert "recruiter_summary_message_id" not in state
    assert "metadata_columns_missing" in caplog.text
