from __future__ import annotations

from types import SimpleNamespace

from modules.community.leagues.cog import LeaguesCog
from modules.community.leagues.config import LeagueBundle, LeagueSpec


def _bundle(slug: str, name: str) -> LeagueBundle:
    spec = LeagueSpec(key=f"{slug}_h", slug=slug, kind="header", index=None, sheet_name="Tab", cell_range="A1:B2")
    return LeagueBundle(slug=slug, display_name=name, header=spec, boards=[spec])


def test_build_announcement_uses_plain_title_and_bold_league_names() -> None:
    cog = LeaguesCog(SimpleNamespace())
    bundles = [
        _bundle("legendary", "Legendary League"),
        _bundle("rising", "Rising Stars League"),
        _bundle("storm", "Stormforged League"),
    ]
    text = cog._build_announcement(
        bundles,
        {
            "legendary": "https://discord.test/legendary",
            "rising": "https://discord.test/rising",
            "storm": "https://discord.test/storm",
        },
    )

    assert "# Shifting Echoes from the C1CLeague …" in text
    assert "@C1CLeague" not in text
    assert "🦅 **Legendary League**" in text
    assert "🌟 **Rising Stars League**" in text
    assert "⚡ **Stormforged League**" in text
    assert "🔹 **Legendary League** – [Jump to this week’s update](https://discord.test/legendary)" in text


def test_league_role_mention_prefers_role_id(monkeypatch) -> None:
    cog = LeaguesCog(SimpleNamespace())
    monkeypatch.setenv("C1C_LEAGUE_ROLE_ID", "12345")
    assert cog._league_role_mention() == "<@&12345>"


def _approval_row(status: str = "pending", *, posted_at_utc: str = "", approved_by_user_ids: str = ""):
    values = {
        "season_key": "2026",
        "week_key": "26",
        "prompt_message_id": "1234",
        "prompt_channel_id": "5678",
        "status": status,
        "required_reactions": "1",
        "approved_by_user_ids": approved_by_user_ids,
        "posted_at_utc": posted_at_utc,
        "created_at_utc": "2026-06-24T00:00:00+00:00",
        "updated_at_utc": "2026-06-24T00:00:00+00:00",
        "last_error": "",
    }
    return {"values": values, "row_number": 2, "header_map": {key: index for index, key in enumerate(values)}}


class _LeagueApprovalBot:
    user = SimpleNamespace(id=9999)

    def get_guild(self, _guild_id):
        return None

    def get_channel(self, _channel_id):
        return None


class _LeagueApprovalPayload:
    guild_id = 42
    channel_id = 5678
    message_id = 1234
    user_id = 1111
    emoji = "👍"
    member = SimpleNamespace(id=1111)


async def _run_approval(
    monkeypatch,
    *,
    is_admin: bool,
    job_result: bool = True,
    job_error: Exception | None = None,
    status: str = "pending",
    posted_at_utc: str = "",
    approved_by_user_ids: str = "",
):
    cog = LeaguesCog(_LeagueApprovalBot())
    row = _approval_row(status, posted_at_utc=posted_at_utc, approved_by_user_ids=approved_by_user_ids)
    updates = []
    calls = {"job": 0}

    monkeypatch.delenv("LEAGUE_ADMIN_IDS", raising=False)
    monkeypatch.setattr("modules.community.leagues.cog.is_admin_member", lambda member: is_admin)

    async def _find(channel_id, message_id, *, include_terminal=False):
        assert channel_id == 5678
        assert message_id == 1234
        return row

    async def _update(target, changes):
        updates.append(dict(changes))
        target["values"].update({key: str(value) for key, value in changes.items()})

    async def _run_job(*, trigger, status_channel):
        calls["job"] += 1
        assert trigger == "reaction_approval"
        assert row["values"]["status"] == "posting"
        if job_error is not None:
            raise job_error
        return job_result

    monkeypatch.setattr(cog, "_find_approval_row", _find)
    monkeypatch.setattr(cog, "_update_approval_row", _update)
    monkeypatch.setattr(cog, "run_leagues_job", _run_job)

    await cog.on_raw_reaction_add(_LeagueApprovalPayload())
    return row, updates, calls


def test_league_approval_allows_project_admin_without_league_admin_ids(monkeypatch) -> None:
    import asyncio

    row, updates, calls = asyncio.run(_run_approval(monkeypatch, is_admin=True))

    assert calls["job"] == 1
    assert row["values"]["approved_by_user_ids"] == "1111"
    assert any(update.get("status") == "posting" for update in updates)
    assert row["values"]["status"] == "posted"


def test_league_approval_rejects_non_admin_when_not_in_league_admin_ids(monkeypatch) -> None:
    import asyncio

    row, updates, calls = asyncio.run(_run_approval(monkeypatch, is_admin=False))

    assert calls["job"] == 0
    assert updates == []
    assert row["values"]["status"] == "pending"


def test_league_approval_marks_posting_before_job_and_posted_on_success(monkeypatch) -> None:
    import asyncio

    row, updates, calls = asyncio.run(_run_approval(monkeypatch, is_admin=True, job_result=True))

    statuses = [update.get("status") for update in updates if "status" in update]
    assert calls["job"] == 1
    assert statuses == ["posting", "posted"]
    assert row["values"]["posted_at_utc"]


def test_league_approval_marks_failed_and_last_error_on_post_failure(monkeypatch) -> None:
    import asyncio

    row, updates, calls = asyncio.run(
        _run_approval(monkeypatch, is_admin=True, job_error=RuntimeError("boom"))
    )

    statuses = [update.get("status") for update in updates if "status" in update]
    assert calls["job"] == 1
    assert statuses == ["posting", "failed"]
    assert "RuntimeError: boom" in row["values"]["last_error"]


def test_league_approval_retries_failed_unposted_row(monkeypatch) -> None:
    import asyncio

    row, updates, calls = asyncio.run(
        _run_approval(
            monkeypatch,
            is_admin=True,
            job_result=True,
            status="failed",
            approved_by_user_ids="1111",
        )
    )

    statuses = [update.get("status") for update in updates if "status" in update]
    assert calls["job"] == 1
    assert statuses == ["posting", "posted"]
    assert row["values"]["posted_at_utc"]
    assert row["values"]["last_error"] == ""


def test_league_approval_ignores_failed_row_with_posted_at(monkeypatch) -> None:
    import asyncio

    row, updates, calls = asyncio.run(
        _run_approval(
            monkeypatch,
            is_admin=True,
            status="failed",
            posted_at_utc="2026-06-24T00:00:00+00:00",
            approved_by_user_ids="1111",
        )
    )

    assert calls["job"] == 0
    assert updates == []
    assert row["values"]["status"] == "failed"
    assert row["values"]["posted_at_utc"] == "2026-06-24T00:00:00+00:00"


def test_league_approval_ignores_posted_row(monkeypatch) -> None:
    import asyncio

    row, updates, calls = asyncio.run(
        _run_approval(monkeypatch, is_admin=True, status="posted", posted_at_utc="2026-06-24T00:00:00+00:00")
    )

    assert calls["job"] == 0
    assert updates == []
    assert row["values"]["status"] == "posted"


def test_league_approval_retry_failure_sets_failed_and_last_error(monkeypatch) -> None:
    import asyncio

    row, updates, calls = asyncio.run(
        _run_approval(
            monkeypatch,
            is_admin=True,
            status="failed",
            approved_by_user_ids="1111",
            job_error=RuntimeError("retry boom"),
        )
    )

    statuses = [update.get("status") for update in updates if "status" in update]
    assert calls["job"] == 1
    assert statuses == ["posting", "failed"]
    assert row["values"]["posted_at_utc"] == ""
    assert "RuntimeError: retry boom" in row["values"]["last_error"]


def test_reaction_approval_runtime_uses_async_league_config_loader(monkeypatch):
    import asyncio

    from modules.community.leagues import cog as leagues_cog
    from shared.sheets import core as sheets_core

    row = _approval_row()
    updates = []
    calls = {"async_loader": 0}

    monkeypatch.setenv("LEAGUES_SHEET_ID", "leagues-sheet")
    monkeypatch.setenv("LEAGUES_LEGENDARY_THREAD_ID", "1")
    monkeypatch.setenv("LEAGUES_RISING_THREAD_ID", "2")
    monkeypatch.setenv("LEAGUES_STORMFORGED_THREAD_ID", "3")
    monkeypatch.setenv("ANNOUNCEMENT_CHANNEL_ID", "4")
    monkeypatch.delenv("LEAGUE_ADMIN_IDS", raising=False)
    monkeypatch.setattr(leagues_cog, "is_admin_member", lambda member: True)

    def _sync_retry_forbidden(*_args, **_kwargs):
        raise AssertionError("sync Sheets retry must not be used by reaction_approval runtime")

    async def _async_loader(sheet_id, *, config_tab="Config"):
        calls["async_loader"] += 1
        assert sheet_id == "leagues-sheet"
        assert config_tab == "Config"
        return [_bundle("legendary", "Legendary League"), _bundle("rising", "Rising Stars League"), _bundle("storm", "Stormforged League")]

    class _Channel:
        def __init__(self, channel_id):
            self.id = channel_id
            self.sent = []

        async def send(self, *args, **kwargs):
            message = SimpleNamespace(id=len(self.sent) + 10, jump_url=f"https://discord.test/{self.id}/{len(self.sent)}")
            self.sent.append((args, kwargs, message))
            return message

    channels = {channel_id: _Channel(channel_id) for channel_id in (1, 2, 3, 4, 5678)}

    async def _find(channel_id, message_id, *, include_terminal=False):
        return row

    async def _update(target, changes):
        updates.append(dict(changes))
        target["values"].update({key: str(value) for key, value in changes.items()})

    async def _resolve(channel_id):
        return channels.get(channel_id)

    async def _export_header(_loop, _sheet_id, bundle):
        return SimpleNamespace(filename=f"{bundle.slug}_header.png")

    async def _export_boards(_loop, _sheet_id, bundle):
        return [SimpleNamespace(filename=f"{bundle.slug}_1.png")]

    monkeypatch.setattr(sheets_core, "_retry_with_backoff", _sync_retry_forbidden)
    monkeypatch.setattr(leagues_cog, "aload_league_bundles", _async_loader)

    bot = _LeagueApprovalBot()
    bot.get_cog = lambda _name: None
    cog = LeaguesCog(bot)
    monkeypatch.setattr(cog, "_find_approval_row", _find)
    monkeypatch.setattr(cog, "_update_approval_row", _update)
    monkeypatch.setattr(cog, "_resolve_channel", _resolve)
    monkeypatch.setattr(cog, "_export_header_image", _export_header)
    monkeypatch.setattr(cog, "_export_board_images", _export_boards)

    asyncio.run(cog.on_raw_reaction_add(_LeagueApprovalPayload()))

    assert calls["async_loader"] == 1
    assert row["values"]["status"] == "posted"
    assert [update.get("status") for update in updates if "status" in update] == ["posting", "posted"]


def test_approval_state_tab_is_loaded_from_leagues_config(monkeypatch):
    import asyncio
    from modules.community.leagues import cog as leagues_cog

    seen = {}

    async def _records(sheet_id, tab):
        seen["sheet_id"] = sheet_id
        seen["tab"] = tab
        return [{"spec_key": "league_approval_state_tab", "sheet_name": "LeagueApprovalState"}]

    monkeypatch.setenv("LEAGUES_CONFIG_TAB", "Config")
    monkeypatch.setattr(leagues_cog, "afetch_records", _records)
    cog = LeaguesCog(SimpleNamespace())

    assert asyncio.run(cog._approval_state_tab("leagues-sheet")) == "LeagueApprovalState"
    assert seen == {"sheet_id": "leagues-sheet", "tab": "Config"}


def test_create_approval_prompt_state_appends_pending_row(monkeypatch):
    import asyncio

    appended = []
    worksheet = SimpleNamespace(append_row=object())
    header_map = {key: index for index, key in enumerate(
        [
            "season_key",
            "week_key",
            "prompt_message_id",
            "prompt_channel_id",
            "status",
            "required_reactions",
            "approved_by_user_ids",
            "posted_at_utc",
            "created_at_utc",
            "updated_at_utc",
            "last_error",
        ]
    )}
    matrix = [list(header_map)]
    message = SimpleNamespace(id=2468, channel=SimpleNamespace(id=1357))

    async def _acall(func, *args, **kwargs):
        appended.append((func, args, kwargs))

    monkeypatch.setattr("modules.community.leagues.cog.acall_with_backoff", _acall)
    cog = LeaguesCog(SimpleNamespace())
    monkeypatch.setattr(cog, "_approval_keys", lambda: ("2026", "27"))

    asyncio.run(cog._create_approval_prompt_state(message, ("LeagueApprovalState", worksheet, header_map, matrix)))

    assert len(appended) == 1
    assert appended[0][0] == worksheet.append_row
    row = appended[0][1][0]
    assert row[:6] == ["2026", "27", "2468", "1357", "pending", "1"]
    assert row[8]
    assert row[9]


def test_wednesday_reminder_skips_existing_week_row(monkeypatch):
    import asyncio

    sent = {"count": 0}
    headers = [
        "season_key",
        "week_key",
        "prompt_message_id",
        "prompt_channel_id",
        "status",
        "required_reactions",
        "approved_by_user_ids",
        "posted_at_utc",
        "created_at_utc",
        "updated_at_utc",
        "last_error",
    ]
    matrix = [headers, ["2026", "27", "123", "456", "pending", "1", "", "", "c", "u", ""]]

    async def _approval_sheet():
        return ("LeagueApprovalState", SimpleNamespace(), {name: i for i, name in enumerate(headers)}, matrix)

    async def _send(*_args, **_kwargs):
        sent["count"] += 1
        return SimpleNamespace(id=999, channel=SimpleNamespace(id=456), add_reaction=lambda *_a, **_k: None)

    async def _resolve(_channel_id):
        return SimpleNamespace(send=_send)

    cog = LeaguesCog(SimpleNamespace())
    monkeypatch.setattr(cog, "_approval_keys", lambda: ("2026", "27"))
    monkeypatch.setattr(cog, "_approval_sheet", _approval_sheet)
    monkeypatch.setattr(cog, "_resolve_channel", _resolve)

    asyncio.run(cog.send_wednesday_reminder())

    assert sent["count"] == 0


def test_wednesday_reminder_allows_recovery_for_failed_unposted_row(monkeypatch):
    import asyncio

    calls = {"send": 0, "append": 0}
    headers = [
        "season_key",
        "week_key",
        "prompt_message_id",
        "prompt_channel_id",
        "status",
        "required_reactions",
        "approved_by_user_ids",
        "posted_at_utc",
        "created_at_utc",
        "updated_at_utc",
        "last_error",
    ]
    matrix = [headers, ["2026", "27", "123", "456", "failed", "1", "111", "", "c", "u", "boom"]]
    worksheet = SimpleNamespace(append_row=object())

    async def _approval_sheet():
        return ("LeagueApprovalState", worksheet, {name: i for i, name in enumerate(headers)}, matrix)

    async def _send(*_args, **_kwargs):
        calls["send"] += 1

        async def _add_reaction(*_a, **_k):
            return None

        return SimpleNamespace(id=999, channel=SimpleNamespace(id=456), add_reaction=_add_reaction)

    async def _resolve(_channel_id):
        return SimpleNamespace(id=456, send=_send)

    async def _acall(func, *args, **kwargs):
        if func == worksheet.append_row:
            calls["append"] += 1

    cog = LeaguesCog(SimpleNamespace())
    monkeypatch.setattr(cog, "_approval_keys", lambda: ("2026", "27"))
    monkeypatch.setattr(cog, "_approval_sheet", _approval_sheet)
    monkeypatch.setattr(cog, "_resolve_channel", _resolve)
    monkeypatch.setattr("modules.community.leagues.cog.acall_with_backoff", _acall)

    asyncio.run(cog.send_wednesday_reminder())

    assert calls == {"send": 1, "append": 1}


def test_wednesday_reminder_allows_recovery_for_deleted_pending_prompt(monkeypatch):
    import asyncio

    calls = {"send": 0, "append": 0}
    headers = [
        "season_key",
        "week_key",
        "prompt_message_id",
        "prompt_channel_id",
        "status",
        "required_reactions",
        "approved_by_user_ids",
        "posted_at_utc",
        "created_at_utc",
        "updated_at_utc",
        "last_error",
    ]
    matrix = [headers, ["2026", "27", "123", "456", "pending", "1", "", "", "c", "u", ""]]
    worksheet = SimpleNamespace(append_row=object())

    async def _approval_sheet():
        return ("LeagueApprovalState", worksheet, {name: i for i, name in enumerate(headers)}, matrix)

    async def _send(*_args, **_kwargs):
        calls["send"] += 1

        async def _add_reaction(*_a, **_k):
            return None

        return SimpleNamespace(id=999, channel=SimpleNamespace(id=456), add_reaction=_add_reaction)

    async def _resolve(_channel_id):
        return SimpleNamespace(id=456, send=_send)

    async def _message_exists(_row):
        return False

    async def _acall(func, *args, **kwargs):
        if func == worksheet.append_row:
            calls["append"] += 1

    cog = LeaguesCog(SimpleNamespace())
    monkeypatch.setattr(cog, "_approval_keys", lambda: ("2026", "27"))
    monkeypatch.setattr(cog, "_approval_sheet", _approval_sheet)
    monkeypatch.setattr(cog, "_resolve_channel", _resolve)
    monkeypatch.setattr(cog, "_approval_prompt_message_exists", _message_exists)
    monkeypatch.setattr("modules.community.leagues.cog.acall_with_backoff", _acall)

    asyncio.run(cog.send_wednesday_reminder())

    assert calls == {"send": 1, "append": 1}
