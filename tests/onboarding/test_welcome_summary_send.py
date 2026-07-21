import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

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
    monkeypatch.setattr(
        welcome_controller, "_fallback_welcome_embed", lambda _author: fallback
    )
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
    assert thread.sent == [
        {"content": None, "embed": fallback, "allowed_mentions": None}
    ]


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
    def __init__(
        self,
        *,
        id: int,
        author_id: int = 111,
        embeds: list[discord.Embed] | None = None,
        content: str | None = None,
    ) -> None:
        super().__init__(
            id=id,
            author=SimpleNamespace(id=author_id),
            embeds=embeds or [],
            content=content or "",
        )
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
            embeds=(
                [kwargs["embed"]]
                if isinstance(kwargs.get("embed"), discord.Embed)
                else []
            ),
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
    return discord.Forbidden(
        SimpleNamespace(status=403, reason="Forbidden"), "forbidden"
    )


def _discord_http_error() -> discord.HTTPException:
    return discord.HTTPException(
        SimpleNamespace(status=500, reason="Server Error"), "boom"
    )


def _summary_embed(title: str = "C1C • Recruitment Summary") -> discord.Embed:
    return discord.Embed(title=title)


def _install_summary_fakes(
    monkeypatch: object, row: dict[str, object] | None = None
) -> tuple[dict[str, object], list[dict[str, object]]]:
    state = dict(row or {"thread_id": "456", "step_index": 15})
    updates: list[dict[str, object]] = []

    async def fake_get_by_thread_id(thread_id: int) -> dict[str, object]:
        assert thread_id == 456
        return dict(state)

    async def fake_update_existing(thread_id: int, payload: dict[str, object]) -> bool:
        assert thread_id == 456
        state.update(payload)
        updates.append(dict(payload))
        return True

    monkeypatch.setattr(
        welcome_controller.onboarding_sessions,
        "aget_by_thread_id",
        fake_get_by_thread_id,
    )
    monkeypatch.setattr(
        welcome_controller.onboarding_sessions, "aupdate_existing", fake_update_existing
    )
    monkeypatch.setattr(
        welcome_controller.onboarding_sessions,
        "amissing_columns",
        AsyncMock(return_value=set()),
    )
    return state, updates


def _controller_and_thread(
    monkeypatch: object, *, row: dict[str, object] | None = None
) -> tuple[
    welcome_controller.WelcomeController,
    GuardedThread,
    dict[str, object],
    list[dict[str, object]],
    discord.Embed,
]:
    controller = welcome_controller.WelcomeController(
        SimpleNamespace(user=SimpleNamespace(id=111, name="The Woadkeeper"))
    )
    embed = _summary_embed()
    monkeypatch.setattr(welcome_controller, "build_summary_embed", lambda **_: embed)
    thread = GuardedThread()
    state, updates = _install_summary_fakes(monkeypatch, row)
    return controller, thread, state, updates, embed


async def _send_summary(
    controller: welcome_controller.WelcomeController, thread: GuardedThread
) -> bool:
    return await controller._send_welcome_summary_safe(
        thread=thread,
        answers={"player_id": "97909413"},
        author=None,
        schema_hash="hash",
        visibility=None,
    )


def test_summary_id_exists_fetch_succeeds_edits_without_send(
    monkeypatch: object,
) -> None:
    controller, thread, state, updates, embed = _controller_and_thread(
        monkeypatch,
        row={
            "thread_id": "456",
            "step_index": 15,
            "recruiter_summary_message_id": "777",
        },
    )
    existing = GuardedMessage(id=777, author_id=111, embeds=[_summary_embed()])
    thread.messages[777] = existing

    success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert [item.get("content") for item in thread.sent] == [
        welcome_controller.SUMMARY_SCREENSHOT_PROMPT
    ]
    assert existing.edits[-1]["embed"] is embed
    assert any(
        update.get("recruiter_summary_message_id") == "777" for update in updates
    )
    assert state["recruiter_summary_player_id"] == "97909413"


def test_summary_id_not_found_discovers_existing_and_persists(
    monkeypatch: object,
) -> None:
    controller, thread, state, updates, embed = _controller_and_thread(
        monkeypatch,
        row={
            "thread_id": "456",
            "step_index": 15,
            "recruiter_summary_message_id": "777",
        },
    )
    discovered = GuardedMessage(id=778, author_id=111, embeds=[_summary_embed()])
    thread.history_messages.append(discovered)

    success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert [item.get("content") for item in thread.sent] == [
        welcome_controller.SUMMARY_SCREENSHOT_PROMPT
    ]
    assert discovered.edits[-1]["embed"] is embed
    assert any(
        update.get("recruiter_summary_message_id") == "778" for update in updates
    )
    assert state["recruiter_summary_message_id"] == "778"


def test_summary_id_not_found_sends_once_when_thread_has_none(
    monkeypatch: object,
) -> None:
    controller, thread, state, updates, _embed = _controller_and_thread(
        monkeypatch,
        row={
            "thread_id": "456",
            "step_index": 15,
            "recruiter_summary_message_id": "777",
        },
    )

    success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert len(thread.sent) == 2
    assert thread.sent[1]["content"] == welcome_controller.SUMMARY_SCREENSHOT_PROMPT
    assert updates[-2]["recruiter_summary_message_id"] == "900"
    assert state["recruiter_summary_message_id"] == "900"


def test_missing_summary_id_discovers_existing_and_does_not_duplicate(
    monkeypatch: object,
) -> None:
    controller, thread, state, updates, embed = _controller_and_thread(monkeypatch)
    discovered = GuardedMessage(id=801, author_id=111, embeds=[_summary_embed()])
    thread.history_messages.append(discovered)

    success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert [item.get("content") for item in thread.sent] == [
        welcome_controller.SUMMARY_SCREENSHOT_PROMPT
    ]
    assert discovered.edits[-1]["embed"] is embed
    assert any(
        update.get("recruiter_summary_message_id") == "801" for update in updates
    )
    assert state["recruiter_summary_message_id"] == "801"


def test_missing_summary_id_and_no_existing_summary_sends_and_persists(
    monkeypatch: object,
) -> None:
    controller, thread, state, updates, _embed = _controller_and_thread(monkeypatch)

    success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert len(thread.sent) == 2
    assert thread.sent[1]["content"] == welcome_controller.SUMMARY_SCREENSHOT_PROMPT
    assert updates[-2]["recruiter_summary_message_id"] == "900"
    assert state["recruiter_summary_message_id"] == "900"


def test_recorded_fetch_forbidden_blocks_duplicate(monkeypatch: object, caplog) -> None:
    controller, thread, _state, updates, _embed = _controller_and_thread(
        monkeypatch,
        row={
            "thread_id": "456",
            "step_index": 15,
            "recruiter_summary_message_id": "777",
        },
    )
    thread.raise_on_fetch = _discord_forbidden()

    with caplog.at_level("WARNING"):
        success = asyncio.run(_send_summary(controller, thread))

    assert success is False
    assert thread.sent == []
    assert updates == []
    assert "recorded_edit_failed" in caplog.text


def test_recorded_fetch_http_exception_blocks_duplicate(
    monkeypatch: object, caplog
) -> None:
    controller, thread, _state, updates, _embed = _controller_and_thread(
        monkeypatch,
        row={
            "thread_id": "456",
            "step_index": 15,
            "recruiter_summary_message_id": "777",
        },
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
        return await asyncio.gather(
            _send_summary(controller, thread), _send_summary(controller, thread)
        )

    results = asyncio.run(runner())

    assert results == [True, True]
    assert len(thread.sent) == 2
    assert thread.sent[1]["content"] == welcome_controller.SUMMARY_SCREENSHOT_PROMPT
    assert state["recruiter_summary_message_id"] == "900"
    assert any(
        update.get("recruiter_summary_message_id") == "900" for update in updates
    )


def test_missing_metadata_headers_logs_warning_without_claiming_persist(
    monkeypatch: object, caplog
) -> None:
    controller, thread, state, updates, _embed = _controller_and_thread(monkeypatch)
    monkeypatch.setattr(
        welcome_controller.onboarding_sessions,
        "amissing_columns",
        AsyncMock(return_value={"recruiter_summary_message_id"}),
    )

    with caplog.at_level("WARNING"):
        success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert len(thread.sent) == 1
    assert updates == []
    assert "recruiter_summary_message_id" not in state
    assert "metadata_columns_missing" in caplog.text
    assert "screenshot_prompt_skipped_missing_metadata_columns" in caplog.text


def test_welcome_summary_posts_screenshot_prompt_after_summary(
    monkeypatch: object,
) -> None:
    controller, thread, state, updates, _embed = _controller_and_thread(monkeypatch)

    success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert thread.sent[0].get("embed") is not None
    assert thread.sent[1] == {"content": welcome_controller.SUMMARY_SCREENSHOT_PROMPT}
    assert state["summary_screenshot_prompt_summary_message_id"] == "900"


def test_promo_summary_posts_screenshot_prompt_after_summary(
    monkeypatch: object,
) -> None:
    controller = welcome_controller.BaseWelcomeController(
        SimpleNamespace(user=SimpleNamespace(id=111, name="The Woadkeeper")),
        flow="promo.r",
    )
    embed = _summary_embed()
    monkeypatch.setattr(welcome_controller, "build_summary_embed", lambda **_: embed)
    thread = GuardedThread()
    state, _updates = _install_summary_fakes(monkeypatch)

    success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert thread.sent[0].get("embed") is embed
    assert thread.sent[1] == {"content": welcome_controller.SUMMARY_SCREENSHOT_PROMPT}
    assert state["summary_screenshot_prompt_summary_message_id"] == "900"


def test_screenshot_prompt_not_sent_when_summary_not_posted(
    monkeypatch: object,
) -> None:
    controller, thread, _state, _updates, _embed = _controller_and_thread(monkeypatch)
    thread.raise_on_send = RuntimeError("summary failed")

    success = asyncio.run(_send_summary(controller, thread))

    assert success is False
    assert thread.sent == []


def test_screenshot_prompt_failure_does_not_break_summary(
    monkeypatch: object, caplog
) -> None:
    controller, thread, state, updates, _embed = _controller_and_thread(monkeypatch)
    original_send = thread.send
    send_count = 0

    async def send_once_then_fail(**kwargs: object) -> object:
        nonlocal send_count
        send_count += 1
        if send_count == 2:
            raise RuntimeError("prompt failed")
        return await original_send(**kwargs)

    thread.send = send_once_then_fail  # type: ignore[method-assign]

    with caplog.at_level("WARNING"):
        success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert len(thread.sent) == 1
    assert state["recruiter_summary_message_id"] == "900"
    assert any(
        update.get("recruiter_summary_message_id") == "900" for update in updates
    )
    assert "screenshot_prompt_send_failed" in caplog.text


def test_duplicate_screenshot_prompts_not_sent_for_same_summary_event(
    monkeypatch: object,
) -> None:
    controller, thread, _state, updates, _embed = _controller_and_thread(
        monkeypatch,
        row={
            "thread_id": "456",
            "step_index": 15,
            "recruiter_summary_message_id": "777",
            "summary_screenshot_prompt_summary_message_id": "777",
            "summary_screenshot_prompt_message_id": "778",
        },
    )
    existing = GuardedMessage(id=777, author_id=111, embeds=[_summary_embed()])
    thread.messages[777] = existing

    success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert thread.sent == []
    assert existing.edits
    assert not any(
        "summary_screenshot_prompt_summary_message_id" in update for update in updates
    )


def test_screenshot_prompt_skipped_when_dedupe_metadata_columns_missing(
    monkeypatch: object, caplog
) -> None:
    controller, thread, _state, _updates, _embed = _controller_and_thread(monkeypatch)
    monkeypatch.setattr(
        welcome_controller.onboarding_sessions,
        "amissing_columns",
        AsyncMock(
            return_value={
                "summary_screenshot_prompt_message_id",
                "summary_screenshot_prompt_summary_message_id",
            }
        ),
    )

    with caplog.at_level("WARNING"):
        success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert len(thread.sent) == 1
    assert thread.sent[0].get("embed") is not None
    assert "screenshot_prompt_skipped_missing_metadata_columns" in caplog.text
    prompt_record = next(
        record
        for record in caplog.records
        if record.message
        == "onboarding.summary.screenshot_prompt_skipped_missing_metadata_columns"
    )
    missing_columns = getattr(prompt_record, "missing_columns")
    assert "summary_screenshot_prompt_message_id" in missing_columns
    assert "summary_screenshot_prompt_summary_message_id" in missing_columns


def test_screenshot_prompt_skipped_when_dedupe_metadata_read_fails(
    monkeypatch: object, caplog
) -> None:
    controller, thread, _state, _updates, _embed = _controller_and_thread(monkeypatch)
    original_get = welcome_controller.onboarding_sessions.aget_by_thread_id
    calls = 0

    async def get_by_thread_id(thread_id: int) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls >= 3:
            raise RuntimeError("sheet read failed")
        return await original_get(thread_id)

    monkeypatch.setattr(
        welcome_controller.onboarding_sessions, "aget_by_thread_id", get_by_thread_id
    )

    with caplog.at_level("WARNING"):
        success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert len(thread.sent) == 1
    assert "screenshot_prompt_skipped_metadata_read_failed" in caplog.text


def test_screenshot_prompt_metadata_persistence_failure_does_not_break_summary(
    monkeypatch: object, caplog
) -> None:
    controller, thread, state, updates, _embed = _controller_and_thread(monkeypatch)
    original_update = welcome_controller.onboarding_sessions.aupdate_existing
    calls = 0

    async def update_existing(thread_id: int, payload: dict[str, object]) -> bool:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("sheet write failed")
        return await original_update(thread_id, payload)

    monkeypatch.setattr(
        welcome_controller.onboarding_sessions, "aupdate_existing", update_existing
    )

    with caplog.at_level("WARNING"):
        success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert len(thread.sent) == 2
    assert thread.sent[1]["content"] == welcome_controller.SUMMARY_SCREENSHOT_PROMPT
    assert state["recruiter_summary_message_id"] == "900"
    assert any(
        update.get("recruiter_summary_message_id") == "900" for update in updates
    )
    assert "summary_screenshot_prompt_summary_message_id" not in state
    assert "screenshot_prompt_metadata_persist_failed" in caplog.text


def test_screenshot_prompt_history_scan_skips_existing_unrecorded_prompt(
    monkeypatch: object, caplog
) -> None:
    controller, thread, _state, updates, _embed = _controller_and_thread(monkeypatch)
    existing_prompt = GuardedMessage(
        id=888,
        author_id=111,
        content=welcome_controller.SUMMARY_SCREENSHOT_PROMPT,
    )
    thread.history_messages.append(existing_prompt)

    with caplog.at_level("WARNING"):
        success = asyncio.run(_send_summary(controller, thread))

    assert success is True
    assert len(thread.sent) == 1
    assert thread.sent[0].get("embed") is not None
    assert not any(
        "summary_screenshot_prompt_summary_message_id" in update for update in updates
    )
    assert "screenshot_prompt_skipped_existing_prompt_found" in caplog.text
