import asyncio
from types import SimpleNamespace

from modules.common import feature_flags
from modules.onboarding import thread_scopes, welcome_flow
from shared.sheets import onboarding as onboarding_sheets
from shared.sheets import onboarding_questions


class DummyThread(SimpleNamespace):
    def __init__(self, name: str = "R1234-user") -> None:
        super().__init__(id=999, name=name, parent=None)
        self.sent_messages: list[str | None] = []

    async def send(self, content=None, **_kwargs):
        self.sent_messages.append(content)


def test_resolve_onboarding_flow_welcome_scope(monkeypatch):
    monkeypatch.setattr(thread_scopes, "is_welcome_parent", lambda _thread: True)
    monkeypatch.setattr(thread_scopes, "is_promo_parent", lambda _thread: False)

    result = welcome_flow.resolve_onboarding_flow(DummyThread("W0001-user"))

    assert result.flow == "welcome"
    assert result.ticket_code is None


def test_resolve_onboarding_flow_maps_promo_prefix(monkeypatch):
    monkeypatch.setattr(thread_scopes, "is_welcome_parent", lambda _thread: False)
    monkeypatch.setattr(thread_scopes, "is_promo_parent", lambda _thread: True)

    result = welcome_flow.resolve_onboarding_flow(DummyThread("L9876-user"))

    assert result.flow == "promo.l"
    assert result.ticket_code == "L9876"


def test_resolve_onboarding_flow_handles_parse_failure(monkeypatch):
    monkeypatch.setattr(thread_scopes, "is_welcome_parent", lambda _thread: False)
    monkeypatch.setattr(thread_scopes, "is_promo_parent", lambda _thread: True)

    result = welcome_flow.resolve_onboarding_flow(DummyThread("no-ticket"))

    assert result.flow is None
    assert result.error == "promo_ticket_parse_failed"


def test_start_welcome_dialog_aborts_when_promo_disabled(monkeypatch):
    monkeypatch.setattr(thread_scopes, "is_welcome_parent", lambda _thread: False)
    monkeypatch.setattr(thread_scopes, "is_promo_parent", lambda _thread: True)

    thread = DummyThread("R2345-user")
    actor = SimpleNamespace(display_name="Recruit", bot=False)

    async def fake_locate(_thread):
        return SimpleNamespace()

    async def fake_send_log(level: str, **payload):
        recorded_logs.append(payload)

    recorded_logs: list[dict[str, object]] = []

    monkeypatch.setattr(welcome_flow, "locate_welcome_message", fake_locate)
    monkeypatch.setattr(welcome_flow, "extract_target_from_message", lambda _msg: (None, None))
    monkeypatch.setattr(welcome_flow.logs, "send_welcome_log", fake_send_log)
    monkeypatch.setattr(welcome_flow.logs, "send_welcome_exception", lambda *args, **kwargs: None)

    def fake_is_enabled(name: str) -> bool:
        return {"promo_enabled": False, "promo_dialog": True, "welcome_dialog": True}.get(name, True)

    monkeypatch.setattr(feature_flags, "is_enabled", fake_is_enabled)
    monkeypatch.setattr(onboarding_questions, "get_questions", lambda flow: (_ for _ in ()).throw(AssertionError("should not load questions")))

    asyncio.run(
        welcome_flow.start_welcome_dialog(
            thread,
            actor,
            source="ticket",
            bot=SimpleNamespace(),
        )
    )

    assert thread.sent_messages
    assert "promo dialogs are currently disabled" in (thread.sent_messages[0] or "").lower()
    assert recorded_logs and recorded_logs[0]["reason"] == "promo_enabled"


def test_start_welcome_dialog_uses_promo_subflow(monkeypatch):
    monkeypatch.setattr(thread_scopes, "is_welcome_parent", lambda _thread: False)
    monkeypatch.setattr(thread_scopes, "is_promo_parent", lambda _thread: True)

    thread = DummyThread("M6789-user")
    actor = SimpleNamespace(display_name="Recruit", bot=False)

    async def fake_locate(_thread):
        return SimpleNamespace()

    captured: dict[str, object] = {}

    async def fake_send_log(level: str, **payload):
        captured.setdefault("logs", []).append(payload)

    def fake_is_enabled(_name: str) -> bool:
        return True

    def fake_get_questions(flow: str):
        captured["flow"] = flow
        return [object()]

    class DummyController:
        def __init__(self, _bot, *, flow: str):
            self.flow = flow
            self._panel_messages = {}
            self._prefetched_panels = {}
            self._sources = {}

        async def run(self, *_args, **_kwargs):
            captured["controller_flow"] = self.flow
            return None

    monkeypatch.setattr(welcome_flow, "PromoController", DummyController)
    monkeypatch.setattr(welcome_flow, "WelcomeController", DummyController)
    monkeypatch.setattr(welcome_flow, "locate_welcome_message", fake_locate)
    monkeypatch.setattr(welcome_flow, "extract_target_from_message", lambda _msg: (None, None))
    monkeypatch.setattr(welcome_flow.logs, "send_welcome_log", fake_send_log)
    monkeypatch.setattr(welcome_flow.logs, "send_welcome_exception", lambda *args, **kwargs: None)
    async def fake_panel_log(**_kwargs):
        return None

    monkeypatch.setattr(welcome_flow.logs, "log_onboarding_panel_lifecycle", fake_panel_log)
    monkeypatch.setattr(feature_flags, "is_enabled", fake_is_enabled)
    monkeypatch.setattr(onboarding_questions, "get_questions", fake_get_questions)
    monkeypatch.setattr(onboarding_questions, "schema_hash", lambda flow: f"hash-{flow}")
    monkeypatch.setattr(welcome_flow, "_resolve_bot", lambda _thread: SimpleNamespace())
    monkeypatch.setattr(welcome_flow.panels, "register_panel_message", lambda *_args, **_kwargs: None)

    asyncio.run(
        welcome_flow.start_welcome_dialog(
            thread,
            actor,
            source="ticket",
            bot=SimpleNamespace(),
        )
    )

    assert captured.get("flow") == "promo.m"
    assert captured.get("controller_flow") == "promo.m"
    assert not thread.sent_messages


def test_no_onboarding_logs_tab_writer_exists():
    assert not hasattr(onboarding_sheets, "append_onboarding_event_log_row")
    assert not hasattr(onboarding_sheets, "_resolve_onboarding_and_log_tab")


def test_promo_ticket_parse_failure_writes_sheet_before_public_error(monkeypatch):
    monkeypatch.setattr(thread_scopes, "is_welcome_parent", lambda _thread: False)
    monkeypatch.setattr(thread_scopes, "is_promo_parent", lambda _thread: True)
    thread = DummyThread("not-a-promo-ticket")
    actor = SimpleNamespace(id=42, display_name="Recruit", bot=False)
    events: list[str] = []
    updates: list[dict[str, object]] = []

    async def fake_locate(_thread):
        return SimpleNamespace(id=555)

    async def fake_send(content=None, **_kwargs):
        events.append("send")
        thread.sent_messages.append(content)

    def fake_update(*_args, **kwargs):
        events.append("sheet")
        updates.append(kwargs)
        return "updated"

    async def fake_log(*_args, **_kwargs):
        return None

    thread.send = fake_send
    monkeypatch.setattr(welcome_flow, "locate_welcome_message", fake_locate)
    monkeypatch.setattr(welcome_flow, "extract_target_from_message", lambda _msg: (123, 555))
    monkeypatch.setattr(welcome_flow.logs, "send_welcome_log", fake_log)
    monkeypatch.setattr(welcome_flow.logs, "send_welcome_exception", fake_log)
    monkeypatch.setattr(welcome_flow.onboarding_sheets, "update_ticket_finalization_state", fake_update)

    asyncio.run(welcome_flow.start_welcome_dialog(thread, actor, source="button", bot=SimpleNamespace()))

    assert events == ["sheet", "send"]
    assert updates[0]["thread_id"] == thread.id
    assert updates[0]["finalization_status"] == "pending_review"
    assert updates[0]["finalization_note"] == "promo_ticket_parse_failed before dialog start"


def test_promo_parser_accepts_context_rich_names(monkeypatch):
    monkeypatch.setattr(thread_scopes, "is_welcome_parent", lambda _thread: False)
    monkeypatch.setattr(thread_scopes, "is_promo_parent", lambda _thread: True)

    result = welcome_flow.resolve_onboarding_flow(DummyThread("L0061-C1C WarWalker / C1C Cholula"))

    assert result.flow == "promo.l"
    assert result.ticket_code == "L0061"


def test_promo_parser_accepts_ticket_code_with_optional_free_text():
    examples = {
        "L0061-C1C WarWalker / C1C Cholula": ("L", "0061", "L0061", "promo.l", "C1C WarWalker / C1C Cholula"),
        "L0061 C1C WarWalker / C1C Cholula": ("L", "0061", "L0061", "promo.l", "C1C WarWalker / C1C Cholula"),
        "L0061_C1C WarWalker / C1C Cholula": ("L", "0061", "L0061", "promo.l", "C1C WarWalker / C1C Cholula"),
        "M0042-player name with spaces": ("M", "0042", "M0042", "promo.m", "player name with spaces"),
        "R0188-Player.Name": ("R", "0188", "R0188", "promo.r", "Player.Name"),
        "R0188-player/name": ("R", "0188", "R0188", "promo.r", "player/name"),
        "M0042-player 😈": ("M", "0042", "M0042", "promo.m", "player 😈"),
        "L0061": ("L", "0061", "L0061", "promo.l", ""),
    }

    for name, expected in examples.items():
        result = welcome_flow.parse_promo_thread_name(name)

        assert result is not None, name
        assert (result.prefix, result.digits, result.ticket_code, result.promo_flow, result.display_text) == expected


def test_context_rich_promo_thread_starts_dialog(monkeypatch):
    monkeypatch.setattr(thread_scopes, "is_welcome_parent", lambda _thread: False)
    monkeypatch.setattr(thread_scopes, "is_promo_parent", lambda _thread: True)

    thread = DummyThread("L0061-C1C WarWalker / C1C Cholula")
    actor = SimpleNamespace(id=42, display_name="Recruit", bot=False)
    captured: dict[str, object] = {}

    async def fake_locate(_thread):
        return SimpleNamespace(id=555)

    async def fake_log(*_args, **_kwargs):
        captured.setdefault("logs", []).append(_kwargs)

    class DummyController:
        def __init__(self, _bot, *, flow: str):
            self.flow = flow
            self._panel_messages = {}
            self._prefetched_panels = {}
            self._sources = {}

        async def run(self, *_args, **_kwargs):
            captured["controller_flow"] = self.flow

    monkeypatch.setattr(welcome_flow, "PromoController", DummyController)
    monkeypatch.setattr(welcome_flow, "locate_welcome_message", fake_locate)
    monkeypatch.setattr(welcome_flow, "extract_target_from_message", lambda _msg: (123, 555))
    monkeypatch.setattr(welcome_flow.logs, "send_welcome_log", fake_log)
    monkeypatch.setattr(welcome_flow.logs, "send_welcome_exception", fake_log)
    monkeypatch.setattr(welcome_flow.logs, "log_onboarding_panel_lifecycle", fake_log)
    monkeypatch.setattr(feature_flags, "is_enabled", lambda _name: True)
    monkeypatch.setattr(onboarding_questions, "get_questions", lambda _flow: [object()])
    monkeypatch.setattr(onboarding_questions, "schema_hash", lambda _flow: "schema")

    asyncio.run(welcome_flow.start_welcome_dialog(thread, actor, source="ticket", bot=SimpleNamespace()))

    assert captured["controller_flow"] == "promo.l"
    assert not thread.sent_messages
    assert all(log.get("reason") != "promo_ticket_parse_failed" for log in captured.get("logs", []))
