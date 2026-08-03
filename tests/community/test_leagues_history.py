from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from modules.community.leagues import history


CONFIG = [
    {"KEY": "cluster_capture_config_tab", "VALUE": "Capture Specs"},
    {"KEY": "cluster_clan_map_tab", "VALUE": "Clan Registry"},
    {"KEY": "cluster_event_history_tab", "VALUE": "Archive"},
    {"KEY": "cluster_evaluation_tab", "VALUE": "Ratings"},
]
CLANS = [
    {"active": "TRUE", "clan_tag": "C1C-A", "clan_name": "Alpha Clan", "aliases": "Alpha; A-Team"},
    {"active": "TRUE", "clan_tag": "C1C-B", "clan_name": "Beta Clan", "aliases": "Beta"},
    {"active": "FALSE", "clan_tag": "OLD", "clan_name": "Former", "aliases": "Old Clan"},
]
HEADERS = list(history.HISTORY_HEADERS)


class Worksheet:
    def __init__(self, matrix=None):
        self.matrix = matrix or []
        self.appended = []
        self.get_calls = []

    def get(self, cell_range, **kwargs):
        self.get_calls.append((cell_range, kwargs))
        return self.matrix

    def append_rows(self, rows, **kwargs):
        self.appended.append((rows, kwargs))


def install(monkeypatch, *, specs, sources, archive=None, clans=None):
    archive = archive if archive is not None else [HEADERS]
    worksheets = {name: Worksheet(matrix) for name, matrix in sources.items()}
    worksheets["Archive"] = Worksheet()
    record_tabs = {"Config": CONFIG, "Capture Specs": specs, "Clan Registry": clans or CLANS}

    async def records(_sheet, tab):
        return record_tabs[tab]

    async def values(_sheet, tab):
        assert tab == "Archive"
        return archive

    async def worksheet(_sheet, tab):
        return worksheets[tab]

    async def call(func, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(history, "afetch_records", records)
    monkeypatch.setattr(history, "afetch_values", values)
    monkeypatch.setattr(history, "aget_worksheet", worksheet)
    monkeypatch.setattr(history, "acall_with_backoff", call)
    return worksheets


def weekly_spec(**updates):
    row = {
        "enabled": "TRUE", "event_type": "cvc", "capture_mode": "weekly_score",
        "source_worksheet": "Live Input", "source_range": "B5:D9",
        "current_clan_column": "B", "score_column": "D", "score_unit": "points",
    }
    row.update(updates)
    return row


def run_capture(**kwargs):
    return asyncio.run(history.capture_weekly_history(
        "sheet", config_tab="Config", week_key="2026-W31",
        trigger="reaction_approval", **kwargs
    ))


def appended_dicts(ws):
    rows = ws.appended[0][0]
    return [dict(zip(HEADERS, row)) for row in rows]


def test_config_drives_tabs_range_columns_aliases_missing_and_unmapped(monkeypatch):
    sheets = install(
        monkeypatch,
        specs=[weekly_spec()],
        sources={"Live Input": [["A.l-p h a", "x", 125], ["Old Clan", "x", 999], ["Unknown!", "x", 8]]},
    )
    summary = run_capture()
    rows = appended_dicts(sheets["Archive"])

    assert summary.active_clans == 2
    assert summary.candidate_rows == summary.appended_rows == 2
    assert summary.missing_rows == 1
    assert summary.ignored_source_clans == 2
    assert sheets["Live Input"].get_calls == [("B5:D9", {"value_render_option": "UNFORMATTED_VALUE"})]
    assert rows[0]["score"] == 125 and rows[0]["evaluation_status"] == "valid"
    assert rows[0]["source_row"] == 5 and rows[0]["source_range"] == "Live Input!B5:D9"
    assert rows[1]["score"] == "" and rows[1]["evaluation_status"] == "missing"
    assert all(row["clan_tag"] != "OLD" for row in rows)


@pytest.mark.parametrize("score", ["", 0, "not-a-number"])
def test_blank_invalid_and_zero_scores_are_missing_not_zero(monkeypatch, score):
    sheets = install(monkeypatch, specs=[weekly_spec()], sources={"Live Input": [["Alpha", "", score]]})
    run_capture()
    alpha = appended_dicts(sheets["Archive"])[0]
    assert alpha["score"] == ""
    assert alpha["evaluation_status"] == "missing"


def test_alias_collision_is_rejected(monkeypatch):
    clans = CLANS[:2] + [{"active": "TRUE", "clan_tag": "C1C-X", "clan_name": "Other", "aliases": "A Team"}]
    install(monkeypatch, specs=[weekly_spec()], sources={"Live Input": []}, clans=clans)
    with pytest.raises(history.HistoryCaptureError, match="alias collision"):
        run_capture()


def delta_spec():
    return {
        "enabled": "TRUE", "event_type": "siege", "capture_mode": "cumulative_win_delta",
        "source_worksheet": "Siege Input", "source_range": "A2:D8",
        "current_clan_column": "A", "current_total_column": "B",
        "previous_clan_column": "C", "previous_total_column": "D",
        "history_status": "result_only",
    }


def test_cumulative_delta_captures_win_loss_and_result_only(monkeypatch):
    sheets = install(monkeypatch, specs=[delta_spec()], sources={
        "Siege Input": [["Alpha", 12, "Alpha Clan", 10], ["Beta", 7, "Beta", 7]]
    })
    summary = run_capture()
    rows = appended_dicts(sheets["Archive"])
    assert [(row["score"], row["result"]) for row in rows] == [(2, "win"), (0, "loss")]
    assert all(row["score_unit"] == "wins" for row in rows)
    assert summary.result_only_rows == 2


def test_negative_delta_aborts_without_append(monkeypatch):
    sheets = install(monkeypatch, specs=[delta_spec()], sources={"Siege Input": [["Alpha", 9, "Alpha", 10]]})
    with pytest.raises(history.HistoryCaptureError, match="negative cumulative delta"):
        run_capture()
    assert sheets["Archive"].appended == []


def test_identical_retry_dedupes_and_conflict_never_overwrites(monkeypatch):
    sheets = install(monkeypatch, specs=[weekly_spec()], sources={"Live Input": [["Alpha", "", 5]]})
    run_capture(captured_at=history.dt.datetime(2026, 8, 3, tzinfo=history.dt.timezone.utc))
    rows = sheets["Archive"].appended[0][0]
    archive = [HEADERS, *rows]

    sheets = install(monkeypatch, specs=[weekly_spec()], sources={"Live Input": [["Alpha", "", 5]]}, archive=archive)
    summary = run_capture(captured_at=history.dt.datetime(2026, 8, 4, tzinfo=history.dt.timezone.utc))
    assert summary.identical_rows == 2 and summary.appended_rows == 0
    assert sheets["Archive"].appended == []

    sheets = install(monkeypatch, specs=[weekly_spec()], sources={"Live Input": [["Alpha", "", 6]]}, archive=archive)
    with pytest.raises(history.HistoryCaptureError, match="history-conflict"):
        run_capture()
    assert sheets["Archive"].appended == []


def test_week_key_format_is_stable_for_approval_and_manual():
    from modules.community.leagues.cog import LeaguesCog

    instant = history.dt.datetime(2026, 1, 1, tzinfo=history.dt.timezone.utc)
    assert LeaguesCog._format_week_key("2026", "1") == "2026-W01"
    assert LeaguesCog._current_week_key(instant) == "2026-W01"


def test_capture_failure_prevents_first_export_or_board_send(monkeypatch):
    from modules.community.leagues import cog as leagues_cog
    from modules.community.leagues.config import LeagueBundle, LeagueSpec

    monkeypatch.setenv("LEAGUES_SHEET_ID", "sheet")
    for key, value in {
        "LEAGUES_LEGENDARY_THREAD_ID": "1", "LEAGUES_RISING_THREAD_ID": "2",
        "LEAGUES_STORMFORGED_THREAD_ID": "3", "ANNOUNCEMENT_CHANNEL_ID": "4",
    }.items():
        monkeypatch.setenv(key, value)
    events = []
    channel = SimpleNamespace(send=lambda *_a, **_k: None)
    cog = leagues_cog.LeaguesCog(SimpleNamespace())

    async def resolve(_channel_id):
        return channel

    spec = LeagueSpec("key", "legendary", "header", None, "tab", "A1")
    bundles = [LeagueBundle(slug, slug, spec, [spec]) for slug in ("legendary", "rising", "storm")]

    async def load(*_a, **_k):
        return bundles

    async def capture(*_a, **_k):
        events.append("capture")
        raise history.HistoryCaptureError("bad snapshot")

    async def export(*_a, **_k):
        events.append("export")

    async def status(*_a, **_k):
        events.append("status")

    monkeypatch.setattr(cog, "_resolve_channel", resolve)
    monkeypatch.setattr(cog, "_post_status", status)
    monkeypatch.setattr(cog, "_export_header_image", export)
    monkeypatch.setattr(leagues_cog, "aload_league_bundles", load)
    monkeypatch.setattr(leagues_cog, "capture_weekly_history", capture)

    assert asyncio.run(cog._run_leagues_job(trigger="command", status_channel=channel)) is False
    assert events == ["capture", "status"]
