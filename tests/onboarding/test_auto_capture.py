import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import modules.onboarding.controllers.welcome_controller as welcome_controller_module
from modules.onboarding.controllers.welcome_controller import WelcomeController
from modules.onboarding.session_store import store


def test_handle_thread_message_captures_answer() -> None:
    async def runner() -> None:
        loop = asyncio.get_running_loop()
        controller = WelcomeController(SimpleNamespace(loop=loop, logger=None))

        thread_id = 1024
        question = SimpleNamespace(
            qid="w_ign",
            label="IGN",
            type="short",
            required=True,
            validate="",
            help="",
            options=(),
        )
        controller._questions[thread_id] = [question]
        controller._threads[thread_id] = SimpleNamespace(id=thread_id)

        session = store.ensure(thread_id, flow=controller.flow, schema_hash="hash")
        session.pending_step = {"kind": "inline", "index": 0}
        session.status = "in_progress"
        session.respondent_id = 555
        session.thread_id = thread_id
        session.answers = {}

        message = SimpleNamespace(
            channel=SimpleNamespace(id=thread_id),
            author=SimpleNamespace(id=555, bot=False),
            content="  Answer  ",
            id=99,
            add_reaction=AsyncMock(),
            delete=AsyncMock(),
        )

        controller._react_to_message = AsyncMock()
        controller._refresh_inline_message = AsyncMock()

        handled = await controller.handle_thread_message(message)

        assert handled is True
        stored = controller.answers_by_thread.get(thread_id, {})
        assert stored.get("w_ign") == "Answer"
        controller._react_to_message.assert_any_call(message, "✅")
        controller._refresh_inline_message.assert_awaited_with(thread_id, index=0)
        message.delete.assert_awaited()

        store.end(thread_id)

    asyncio.run(runner())


def test_handle_thread_message_normalizes_sheet_declared_score() -> None:
    async def runner() -> None:
        loop = asyncio.get_running_loop()
        controller = WelcomeController(SimpleNamespace(loop=loop, logger=None))
        thread_id = 1025
        question = SimpleNamespace(
            qid="sheet_score",
            label="Score",
            type="number",
            required=True,
            validate="score",
            help="",
            options=(),
        )
        controller._questions[thread_id] = [question]
        controller._threads[thread_id] = SimpleNamespace(id=thread_id)
        session = store.ensure(thread_id, flow=controller.flow, schema_hash="hash")
        session.pending_step = {"kind": "inline", "index": 0}
        session.status = "in_progress"
        session.respondent_id = 555
        session.thread_id = thread_id
        session.answers = {}
        message = SimpleNamespace(
            channel=SimpleNamespace(id=thread_id),
            author=SimpleNamespace(id=555, bot=False),
            content="Over 40b",
            id=100,
            add_reaction=AsyncMock(),
            delete=AsyncMock(),
        )
        controller._react_to_message = AsyncMock()
        controller._refresh_inline_message = AsyncMock()

        assert await controller.handle_thread_message(message) is True
        assert controller.answers_by_thread[thread_id]["sheet_score"] == "40B"
        controller._react_to_message.assert_called_with(message, "✅")
        controller._refresh_inline_message.assert_awaited_with(thread_id, index=0)
        message.delete.assert_awaited_once()
        store.end(thread_id)

    asyncio.run(runner())


def test_handle_thread_message_rejects_ambiguous_bare_score() -> None:
    async def runner() -> None:
        loop = asyncio.get_running_loop()
        controller = WelcomeController(SimpleNamespace(loop=loop, logger=None))
        thread_id = 1026
        question = SimpleNamespace(
            qid="sheet_score",
            label="Score",
            type="number",
            required=True,
            validate="score",
            help="",
            options=(),
        )
        controller._questions[thread_id] = [question]
        session = store.ensure(thread_id, flow=controller.flow, schema_hash="hash")
        session.pending_step = {"kind": "inline", "index": 0}
        session.status = "in_progress"
        session.respondent_id = 555
        session.thread_id = thread_id
        session.answers = {}
        message = SimpleNamespace(
            channel=SimpleNamespace(id=thread_id),
            author=SimpleNamespace(id=555, bot=False),
            content="40",
            id=101,
            add_reaction=AsyncMock(),
            delete=AsyncMock(),
        )
        controller._react_to_message = AsyncMock()
        controller._refresh_inline_message = AsyncMock()

        assert await controller.handle_thread_message(message) is True
        assert controller.answers_by_thread.get(thread_id) in (None, {})
        controller._react_to_message.assert_called_with(message, "❌")
        controller._refresh_inline_message.assert_not_awaited()
        message.delete.assert_not_awaited()
        store.end(thread_id)

    asyncio.run(runner())


def test_handle_thread_message_ignores_other_users() -> None:
    async def runner() -> None:
        loop = asyncio.get_running_loop()
        controller = WelcomeController(SimpleNamespace(loop=loop, logger=None))

        thread_id = 2048
        question = SimpleNamespace(
            qid="w_note",
            label="Note",
            type="short",
            required=True,
            validate="",
            help="",
            options=(),
        )
        controller._questions[thread_id] = [question]
        session = store.ensure(thread_id, flow=controller.flow, schema_hash="hash")
        session.pending_step = {"kind": "inline", "index": 0}
        session.status = "in_progress"
        session.respondent_id = 111
        session.thread_id = thread_id

        message = SimpleNamespace(
            channel=SimpleNamespace(id=thread_id),
            author=SimpleNamespace(id=222, bot=False),
            content="Should be ignored",
            id=100,
            add_reaction=AsyncMock(),
            delete=AsyncMock(),
        )

        controller._react_to_message = AsyncMock()
        controller._refresh_inline_message = AsyncMock()

        handled = await controller.handle_thread_message(message)

        assert handled is False
        controller._react_to_message.assert_not_called()
        controller._refresh_inline_message.assert_not_called()
        assert controller.answers_by_thread.get(thread_id) is None
        message.delete.assert_not_awaited()

        store.end(thread_id)

    asyncio.run(runner())


def test_handle_thread_message_requires_bound_respondent() -> None:
    async def runner() -> None:
        loop = asyncio.get_running_loop()
        controller = WelcomeController(SimpleNamespace(loop=loop, logger=None))

        thread_id = 5120
        question = SimpleNamespace(
            qid="w_require",
            label="Required",
            type="short",
            required=True,
            validate="",
            help="",
            options=(),
        )
        controller._questions[thread_id] = [question]
        session = store.ensure(thread_id, flow=controller.flow, schema_hash="hash")
        session.pending_step = {"kind": "inline", "index": 0}
        session.status = "in_progress"
        session.respondent_id = None
        session.thread_id = thread_id

        message = SimpleNamespace(
            channel=SimpleNamespace(id=thread_id),
            author=SimpleNamespace(id=999, bot=False),
            content="Should not capture",
            id=200,
            add_reaction=AsyncMock(),
            delete=AsyncMock(),
        )

        controller._react_to_message = AsyncMock()
        controller._refresh_inline_message = AsyncMock()

        handled = await controller.handle_thread_message(message)

        assert handled is True
        controller._react_to_message.assert_any_call(message, "✅")
        controller._refresh_inline_message.assert_awaited_with(thread_id, index=0)
        stored = controller.answers_by_thread.get(thread_id, {})
        assert stored.get("w_require") == "Should not capture"
        refreshed = store.get(thread_id)
        assert refreshed is not None
        assert refreshed.respondent_id == 999
        message.delete.assert_awaited()

        store.end(thread_id)

    asyncio.run(runner())


def test_handle_thread_message_flags_invalid_answer() -> None:
    async def runner() -> None:
        loop = asyncio.get_running_loop()
        controller = WelcomeController(SimpleNamespace(loop=loop, logger=None))

        thread_id = 4096
        question = SimpleNamespace(
            qid="w_id",
            label="ID",
            type="short",
            required=True,
            validate="",
            help="",
            options=(),
        )
        controller._questions[thread_id] = [question]
        session = store.ensure(thread_id, flow=controller.flow, schema_hash="hash")
        session.pending_step = {"kind": "inline", "index": 0}
        session.status = "in_progress"
        session.respondent_id = 777
        session.thread_id = thread_id

        message = SimpleNamespace(
            channel=SimpleNamespace(id=thread_id),
            author=SimpleNamespace(id=777, bot=False),
            content="bad",
            id=150,
            add_reaction=AsyncMock(),
            delete=AsyncMock(),
        )

        controller.validate_answer = lambda meta, raw: (False, None, "nope")
        controller._react_to_message = AsyncMock()
        controller._refresh_inline_message = AsyncMock()

        handled = await controller.handle_thread_message(message)

        assert handled is True
        controller._react_to_message.assert_called_with(message, "❌")
        controller._refresh_inline_message.assert_not_called()
        assert controller.answers_by_thread.get(thread_id) is None
        message.delete.assert_not_awaited()

        store.end(thread_id)

    asyncio.run(runner())


def test_handle_thread_message_falls_back_to_current_index() -> None:
    async def runner() -> None:
        loop = asyncio.get_running_loop()
        controller = WelcomeController(SimpleNamespace(loop=loop, logger=None))

        thread_id = 7000
        question = SimpleNamespace(
            qid="w_power",
            label="Power",
            type="short",
            required=True,
            validate="",
            help="",
            options=(),
        )
        controller._questions[thread_id] = [question]
        controller._threads[thread_id] = SimpleNamespace(id=thread_id)

        session = store.ensure(thread_id, flow=controller.flow, schema_hash="hash")
        session.pending_step = None
        session.current_question_index = 0
        session.status = "in_progress"
        session.respondent_id = 123
        session.thread_id = thread_id
        session.answers = {}

        message = SimpleNamespace(
            channel=SimpleNamespace(id=thread_id),
            author=SimpleNamespace(id=123, bot=False),
            content="99",
            id=42,
            add_reaction=AsyncMock(),
            delete=AsyncMock(),
        )

        controller._react_to_message = AsyncMock()
        controller._refresh_inline_message = AsyncMock()

        handled = await controller.handle_thread_message(message)

        assert handled is True
        stored = controller.answers_by_thread.get(thread_id, {})
        assert stored.get("w_power") == "99"
        controller._react_to_message.assert_called_with(message, "✅")
        controller._refresh_inline_message.assert_awaited_with(thread_id, index=0)
        message.delete.assert_awaited()

        store.end(thread_id)

    asyncio.run(runner())


def test_handle_thread_message_rehydrates_non_inline_pending() -> None:
    async def runner() -> None:
        loop = asyncio.get_running_loop()
        controller = WelcomeController(SimpleNamespace(loop=loop, logger=None))

        thread_id = 7100
        question = SimpleNamespace(
            qid="w_alias",
            label="Alias",
            type="short",
            required=True,
            validate="",
            help="",
            options=(),
        )
        controller._questions[thread_id] = [question]
        controller._threads[thread_id] = SimpleNamespace(id=thread_id)

        session = store.ensure(thread_id, flow=controller.flow, schema_hash="hash")
        session.pending_step = {"kind": "select", "index": 0}
        session.current_question_index = 0
        session.status = "in_progress"
        session.respondent_id = 456
        session.thread_id = thread_id
        session.answers = {}

        message = SimpleNamespace(
            channel=SimpleNamespace(id=thread_id),
            author=SimpleNamespace(id=456, bot=False),
            content="AliasName",
            id=314,
            add_reaction=AsyncMock(),
            delete=AsyncMock(),
        )

        controller._react_to_message = AsyncMock()
        controller._refresh_inline_message = AsyncMock()

        handled = await controller.handle_thread_message(message)

        assert handled is True
        stored = controller.answers_by_thread.get(thread_id, {})
        assert stored.get("w_alias") == "AliasName"
        controller._react_to_message.assert_called_with(message, "✅")
        controller._refresh_inline_message.assert_awaited_with(thread_id, index=0)

        refreshed = store.get(thread_id)
        assert refreshed is not None
        assert refreshed.pending_step == {"kind": "inline", "index": 0}
        message.delete.assert_awaited()

        store.end(thread_id)

    asyncio.run(runner())


def test_start_session_from_button_seeds_respondent() -> None:
    async def runner() -> None:
        loop = asyncio.get_running_loop()
        controller = WelcomeController(SimpleNamespace(loop=loop, logger=None))

        thread_id = 6000
        controller._questions[thread_id] = []
        interaction = SimpleNamespace(
            user=SimpleNamespace(id=321, bot=False),
            channel=SimpleNamespace(id=thread_id),
        )

        controller.check_interaction = AsyncMock(return_value=(True, None))
        controller.render_inline_step = AsyncMock()
        controller._sources[thread_id] = "panel"

        original_schema_hash = welcome_controller_module.schema_hash
        welcome_controller_module.schema_hash = lambda flow: "hash"

        try:
            await controller.start_session_from_button(
                thread_id,
                actor_id=None,
                channel=None,
                guild=None,
                interaction=interaction,
            )
        finally:
            welcome_controller_module.schema_hash = original_schema_hash

        session = store.get(thread_id)
        assert session is not None
        assert session.respondent_id == 321

        controller.render_inline_step.assert_awaited()
        store.end(thread_id)

    asyncio.run(runner())


def test_session_status_tracks_answers() -> None:
    async def runner() -> None:
        loop = asyncio.get_running_loop()
        controller = WelcomeController(SimpleNamespace(loop=loop, logger=None))

        thread_id = 7000
        question = SimpleNamespace(
            qid="w_inline",
            label="IGN",
            type="short",
            required=True,
            validate="",
            help="",
            options=(),
        )
        controller._questions[thread_id] = [question]

        session = store.ensure(thread_id, flow=controller.flow, schema_hash="hash")
        session.status = "in_progress"

        controller.answers_by_thread[thread_id] = {}
        assert controller.session_status(thread_id, question) == "in_progress"

        controller.answers_by_thread[thread_id]["w_inline"] = "Name"
        assert controller.session_status(thread_id, question) == "answered"

        store.end(thread_id)

    asyncio.run(runner())


def test_refresh_inline_message_reuses_existing_message(monkeypatch) -> None:
    async def runner() -> None:
        loop = asyncio.get_running_loop()
        controller = WelcomeController(SimpleNamespace(loop=loop, logger=None))

        thread_id = 8000
        question = SimpleNamespace(
            qid="w_clean",
            label="IGN",
            type="short",
            required=True,
            validate="",
            help="",
            options=(),
        )
        controller._questions[thread_id] = [question]
        controller.answers_by_thread[thread_id] = {}

        session = store.ensure(thread_id, flow=controller.flow, schema_hash="hash")
        session.pending_step = {"kind": "inline", "index": 0}
        session.current_question_index = 0
        session.status = "in_progress"

        message = SimpleNamespace(id=999, edit=AsyncMock())
        controller._inline_messages[thread_id] = message

        thread = SimpleNamespace(
            id=thread_id, send=AsyncMock(), fetch_message=AsyncMock()
        )
        controller._threads[thread_id] = thread

        await controller._refresh_inline_message(thread_id, index=0)

        message.edit.assert_awaited()
        thread.send.assert_not_awaited()

        store.end(thread_id)

    asyncio.run(runner())
