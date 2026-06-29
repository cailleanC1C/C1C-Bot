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


def _approval_row(status: str = "pending"):
    values = {
        "season_key": "2026",
        "week_key": "26",
        "prompt_message_id": "1234",
        "prompt_channel_id": "5678",
        "status": status,
        "required_reactions": "1",
        "approved_by_user_ids": "",
        "posted_at_utc": "",
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


async def _run_approval(monkeypatch, *, is_admin: bool, job_result: bool = True, job_error: Exception | None = None):
    cog = LeaguesCog(_LeagueApprovalBot())
    row = _approval_row()
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
