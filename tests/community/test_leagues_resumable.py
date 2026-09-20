import asyncio
from types import SimpleNamespace

import discord

from modules.community.leagues.cog import LeaguesCog
from modules.community.leagues.config import LeagueSpec
from shared.sheets.export_utils import ImageExportError


def test_progress_text_reports_component_state() -> None:
    cog = LeaguesCog(SimpleNamespace())
    row = {
        "values": {
            "prepare_status": "ready",
            "legendary_status": "posted",
            "rising_status": "partial",
            "storm_status": "pending",
            "announcement_status": "pending",
        }
    }

    text = cog._progress_text(
        row,
        "2026-W38",
        state="failed",
        detail="**Error:** Rising Stars send failed",
    )

    assert "**Status:** ❌ Failed" in text
    assert "📸 Images — ✅ ready" in text
    assert "🦅 Legendary League — ✅ posted" in text
    assert "🌟 Rising Stars League — ⚠️ partial" in text
    assert "⚡ Stormforged League — ⏸️ waiting" in text
    assert "**Error:** Rising Stars send failed" in text


def test_export_spec_retries_transient_export_failure(monkeypatch) -> None:
    from modules.community.leagues import cog as leagues_cog

    calls = {"count": 0}

    async def _export(*_args, **kwargs):
        calls["count"] += 1
        assert kwargs["raise_on_failure"] is True
        if calls["count"] < 3:
            raise ImageExportError("pdf_export_status_503")
        return b"png-data"

    sleeps = []

    async def _sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(leagues_cog, "export_pdf_as_png", _export)
    monkeypatch.setattr(leagues_cog.asyncio, "sleep", _sleep)
    monkeypatch.setattr(leagues_cog, "get_tab_gid", lambda *_args: "123")

    spec = LeagueSpec(
        key="LEAGUE_RISING_HEADER",
        slug="rising",
        kind="header",
        index=None,
        sheet_name="RisingStars",
        cell_range="A1:M15",
    )
    cog = LeaguesCog(SimpleNamespace())

    async def _run():
        return await cog._export_spec(
            asyncio.get_running_loop(),
            "sheet",
            "rising",
            spec,
            filename="rising_header.png",
        )

    result = asyncio.run(_run())

    assert isinstance(result, discord.File)
    assert calls["count"] == 3
    assert sleeps == [30.0, 60.0, 30.0]


def test_export_spec_reports_terminal_reason_after_three_attempts(monkeypatch) -> None:
    from modules.community.leagues import cog as leagues_cog

    calls = {"count": 0}

    async def _export(*_args, **_kwargs):
        calls["count"] += 1
        raise ImageExportError("empty_pdf_response")

    async def _sleep(_seconds):
        return None

    monkeypatch.setattr(leagues_cog, "export_pdf_as_png", _export)
    monkeypatch.setattr(leagues_cog.asyncio, "sleep", _sleep)
    monkeypatch.setattr(leagues_cog, "get_tab_gid", lambda *_args: "123")

    spec = LeagueSpec(
        key="LEAGUE_RISING_HEADER",
        slug="rising",
        kind="header",
        index=None,
        sheet_name="RisingStars",
        cell_range="A1:M15",
    )
    cog = LeaguesCog(SimpleNamespace())

    async def _run():
        return await cog._export_spec(
            asyncio.get_running_loop(),
            "sheet",
            "rising",
            spec,
            filename="rising_header.png",
        )

    result = asyncio.run(_run())

    assert calls["count"] == 3
    assert isinstance(result, str)
    assert "LEAGUE_RISING_HEADER export failed after 3 attempts" in result
    assert "empty_pdf_response" in result


def test_export_spec_uses_conservative_429_backoff_and_retry_after(monkeypatch) -> None:
    from modules.community.leagues import cog as leagues_cog

    calls = {"count": 0}
    sleeps = []

    async def _export(*_args, **_kwargs):
        calls["count"] += 1
        if calls["count"] == 1:
            raise ImageExportError("pdf_export_status_429")
        if calls["count"] == 2:
            raise ImageExportError("pdf_export_status_429", retry_after_seconds=180.0)
        return b"png-data"

    async def _sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(leagues_cog, "export_pdf_as_png", _export)
    monkeypatch.setattr(leagues_cog.asyncio, "sleep", _sleep)
    monkeypatch.setattr(leagues_cog, "get_tab_gid", lambda *_args: "123")

    spec = LeagueSpec(
        key="LEAGUE_LEGENDARY_6",
        slug="legendary",
        kind="board",
        index=6,
        sheet_name="Legendary",
        cell_range="A185:M212",
    )
    cog = LeaguesCog(SimpleNamespace())

    async def _run():
        return await cog._export_spec(
            asyncio.get_running_loop(),
            "sheet",
            "legendary",
            spec,
            filename="legendary_6.png",
        )

    result = asyncio.run(_run())

    assert isinstance(result, discord.File)
    assert calls["count"] == 3
    # First 429 uses our 60s floor. The second honors Google's longer
    # Retry-After instead of our 120s floor. A successful export is then
    # followed by the normal 30s pacing delay.
    assert sleeps == [60.0, 180.0, 30.0]


def test_publish_rows_keeps_each_message_id_in_its_own_row(monkeypatch) -> None:
    cog = LeaguesCog(SimpleNamespace())
    matrix = [
        [
            "season_key",
            "week_key",
            "component",
            "message_type",
            "message_id",
            "status",
            "created_at_utc",
            "updated_at_utc",
        ],
        ["2026", "38", "legendary", "header", "111", "posted", "c", "u"],
        ["2026", "38", "legendary", "board", "222", "posted", "c", "u"],
        ["2026", "38", "rising", "header", "333", "posted", "c", "u"],
    ]
    header_map = {name: idx for idx, name in enumerate(matrix[0])}

    async def _state(_sheet_id):
        return SimpleNamespace(), header_map, matrix

    monkeypatch.setattr(cog, "_publish_state_sheet", _state)

    rows = asyncio.run(cog._publish_rows("sheet", "2026-W38", "legendary"))

    assert [row["values"]["message_id"] for row in rows] == ["111", "222"]
    assert all("," not in row["values"]["message_id"] for row in rows)
