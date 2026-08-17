import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from modules.housekeeping import staff_thread_guard as guard


def _row(**overrides):
    row = {
        "enabled": "TRUE",
        "guard_id": "captains_table",
        "thread_id": "12345",
        "action": "delete",
        "redirect_target_id": "",
        "warning_delete_after_seconds": "10",
        "offense_window_minutes": "1440",
        "timeout_after_offenses": "3",
        "timeout_seconds": "30",
        "warning_text": "Avast, wrong deck.",
        "timeout_text": "Into the brig for {timeout_seconds} seconds.",
        "redirect_notice_text": "Hauled to {redirect_target}.",
        "redirect_header_text": "Hauled from {source_thread}",
        "redirect_body_text": "{user_name} from {source_thread}: {message_content}{attachment_links}",
        "failure_text": "Could not haul that message away.",
    }
    row.update(overrides)
    return row


def _rule(**overrides):
    values = {
        "guard_id": "captains_table",
        "thread_id": 12345,
        "action": "delete",
        "redirect_target_id": None,
        "warning_delete_after_seconds": 10,
        "offense_window_minutes": 60,
        "timeout_after_offenses": 3,
        "timeout_seconds": 30,
        "warning_text": "Avast.",
        "timeout_text": "Brig.",
        "redirect_notice_text": "Redirected.",
        "redirect_header_text": "Redirected from {source_thread}",
        "redirect_body_text": "{message_content}{attachment_links}",
        "failure_text": "Failed.",
    }
    values.update(overrides)
    return guard.GuardRule(**values)


def test_validate_rule_builds_delete_guard():
    parsed = guard._validate_rule(_row(), row_number=2)

    assert parsed is not None
    assert parsed.guard_id == "captains_table"
    assert parsed.thread_id == 12345
    assert parsed.action == "delete"
    assert parsed.timeout_after_offenses == 3
    assert parsed.timeout_seconds == 30


def test_validate_rule_requires_redirect_target():
    parsed = guard._validate_rule(
        _row(action="redirect", redirect_target_id=""),
        row_number=3,
    )

    assert parsed is None


def test_validate_rule_keeps_guard_but_disables_bad_timeout_config():
    parsed = guard._validate_rule(
        _row(offense_window_minutes="0", timeout_after_offenses="3"),
        row_number=4,
    )

    assert parsed is not None
    assert parsed.timeout_after_offenses == 0
    assert parsed.timeout_seconds == 0


def test_validate_redirect_rule_requires_configured_body_template():
    parsed = guard._validate_rule(
        _row(
            action="redirect",
            redirect_target_id="67890",
            redirect_body_text="",
        ),
        row_number=5,
    )

    assert parsed is None


def test_advance_offense_counts_only_inside_the_configured_window():
    rule = _rule(offense_window_minutes=60)
    first_at = datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc)
    state = guard._advance_offense(None, rule, user_id=777, now=first_at)

    state = guard._advance_offense(
        state,
        rule,
        user_id=777,
        now=first_at + timedelta(minutes=30),
    )
    assert state.offense_count == 2

    state = guard._advance_offense(
        state,
        rule,
        user_id=777,
        now=first_at + timedelta(minutes=61),
    )
    assert state.offense_count == 1
    assert state.window_started_at_utc == "2026-08-14T09:01:00Z"


def test_state_from_values_is_scoped_to_guard_and_user():
    values = [
        list(guard.OFFENSE_HEADERS),
        [
            "other_guard",
            "999",
            "777",
            "9",
            "2026-08-14T08:00:00Z",
            "2026-08-14T08:10:00Z",
            "",
            "delete",
            "1",
        ],
        [
            "captains_table",
            "12345",
            "777",
            "2",
            "2026-08-14T08:00:00Z",
            "2026-08-14T08:10:00Z",
            "",
            "delete",
            "2",
        ],
    ]

    state = guard._state_from_values(values, guard_id="captains_table", user_id=777)

    assert state is not None
    assert state.sheet_row == 3
    assert state.thread_id == 12345
    assert state.offense_count == 2


def test_install_wraps_on_message_once_and_stops_consumed_messages(monkeypatch):
    calls = []

    async def original(message):
        calls.append(("original", message))

    async def fake_handle(bot, message):
        calls.append(("guard", message))
        return True

    monkeypatch.setattr(guard, "handle_message", fake_handle)
    bot = SimpleNamespace(on_message=original)
    message = object()

    guard.install(bot)
    installed = bot.on_message
    guard.install(bot)

    assert bot.on_message is installed
    asyncio.run(bot.on_message(message))
    assert calls == [("guard", message)]


def test_install_falls_through_when_guard_does_not_consume(monkeypatch):
    calls = []

    async def original(message):
        calls.append("original")

    async def fake_handle(bot, message):
        calls.append("guard")
        return False

    monkeypatch.setattr(guard, "handle_message", fake_handle)
    bot = SimpleNamespace(on_message=original)

    guard.install(bot)
    asyncio.run(bot.on_message(object()))

    assert calls == ["guard", "original"]


def test_load_rules_caches_sheet_rows(monkeypatch):
    guard.invalidate_cache()
    calls = []

    monkeypatch.setattr(guard, "_feature_enabled", lambda: True)

    async def fake_tabs():
        return "HousekeepingThreadGuard", "HousekeepingThreadGuardOffenses"

    async def fake_records(sheet_id, tab):
        calls.append((sheet_id, tab))
        return [_row()]

    monkeypatch.setattr(guard, "_tab_names", fake_tabs)
    monkeypatch.setattr(
        guard.recruitment, "get_recruitment_sheet_id", lambda: "sheet"
    )
    monkeypatch.setattr(guard.async_core, "afetch_records", fake_records)

    async def run():
        first = await guard.load_rules()
        second = await guard.load_rules()
        return first, second

    first, second = asyncio.run(run())

    assert first[12345].guard_id == "captains_table"
    assert second[12345].guard_id == "captains_table"
    assert calls == [("sheet", "HousekeepingThreadGuard")]


class _FakeThread:
    def __init__(self, thread_id=12345):
        self.id = thread_id
        self.name = "captains-table"
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _FakeRole:
    def __init__(self, role_id):
        self.id = role_id


class _FakeMember:
    def __init__(self, user_id, role_ids, *, bot=False, administrator=False):
        self.id = user_id
        self.roles = [_FakeRole(role_id) for role_id in role_ids]
        self.bot = bot
        self.display_name = f"user-{user_id}"
        self.name = self.display_name
        self.mention = f"<@{user_id}>"
        self.guild_permissions = SimpleNamespace(administrator=administrator)
        self.timeout_calls = []

    async def timeout(self, duration, reason=None):
        self.timeout_calls.append((duration, reason))


class _FakeMessage:
    def __init__(self, author, channel, *, message_id=98765):
        self.author = author
        self.channel = channel
        self.guild = SimpleNamespace(id=1)
        self.content = "hello"
        self.attachments = []
        self.id = message_id
        self.delete_calls = 0

    async def delete(self):
        self.delete_calls += 1


def test_handle_message_allows_clan_lead_without_recording_offense(monkeypatch):
    monkeypatch.setattr(guard, "_feature_enabled", lambda: True)
    monkeypatch.setattr(guard.discord, "Thread", _FakeThread)
    monkeypatch.setattr(guard.discord, "Member", _FakeMember)
    monkeypatch.setattr(guard, "get_clan_lead_ids", lambda: {222, 333})
    monkeypatch.setattr(guard, "is_admin_member", lambda author: False)

    async def fail_load_rules():
        raise AssertionError("load_rules should not be called for allowed clan leads")

    async def fail_save_state(state):
        raise AssertionError("allowed clan leads must not record offenses")

    monkeypatch.setattr(guard, "load_rules", fail_load_rules)
    monkeypatch.setattr(guard, "_save_state", fail_save_state)

    author = _FakeMember(77, [333])
    message = _FakeMessage(author, _FakeThread())

    handled = asyncio.run(guard.handle_message(SimpleNamespace(), message))

    assert handled is False
    assert message.delete_calls == 0


def test_handle_message_allows_admin_without_recording_offense(monkeypatch):
    monkeypatch.setattr(guard, "_feature_enabled", lambda: True)
    monkeypatch.setattr(guard.discord, "Thread", _FakeThread)
    monkeypatch.setattr(guard.discord, "Member", _FakeMember)
    monkeypatch.setattr(guard, "get_clan_lead_ids", lambda: set())
    monkeypatch.setattr(guard, "is_admin_member", lambda author: True)

    async def fail_load_rules():
        raise AssertionError("load_rules should not be called for admins")

    monkeypatch.setattr(guard, "load_rules", fail_load_rules)

    author = _FakeMember(88, [], administrator=True)
    message = _FakeMessage(author, _FakeThread())

    handled = asyncio.run(guard.handle_message(SimpleNamespace(), message))

    assert handled is False
    assert message.delete_calls == 0


def test_handle_message_enforces_guard_for_staff_only_member(monkeypatch):
    monkeypatch.setattr(guard, "_feature_enabled", lambda: True)
    monkeypatch.setattr(guard.discord, "Thread", _FakeThread)
    monkeypatch.setattr(guard.discord, "Member", _FakeMember)
    monkeypatch.setattr(guard, "get_clan_lead_ids", lambda: {222})
    monkeypatch.setattr(guard, "is_admin_member", lambda author: False)

    saved_states = []

    async def fake_load_rules():
        return {12345: _rule()}

    async def fake_load_state(rule, message):
        return guard.OffenseState(
            guard_id=rule.guard_id,
            thread_id=rule.thread_id,
            user_id=message.author.id,
            offense_count=1,
            window_started_at_utc="2026-08-17T22:42:00Z",
            last_offense_at_utc="2026-08-17T22:42:00Z",
        )

    async def fake_save_state(state):
        saved_states.append(state)

    async def fake_send_notice(*args, **kwargs):
        return None

    monkeypatch.setattr(guard, "load_rules", fake_load_rules)
    monkeypatch.setattr(guard, "_load_and_advance_state", fake_load_state)
    monkeypatch.setattr(guard, "_save_state", fake_save_state)
    monkeypatch.setattr(guard, "_send_notice", fake_send_notice)

    staff_role_id = 999
    author = _FakeMember(99, [staff_role_id])
    message = _FakeMessage(author, _FakeThread())

    handled = asyncio.run(guard.handle_message(SimpleNamespace(), message))

    assert handled is True
    assert message.delete_calls == 1
    assert len(saved_states) == 1
    assert saved_states[0].user_id == 99
    assert saved_states[0].last_action == "delete"
    assert saved_states[0].offense_count == 1
