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
    {"active": "TRUE", "clan_tag": "C1C-A", "canonical_clan_name": "Cambion", "source_alias": "Cambion"},
    {"active": "TRUE", "clan_tag": "C1C-A", "canonical_clan_name": "Cambion", "source_alias": "C1C Cambion"},
    {"active": "TRUE", "clan_tag": "C1C-B", "canonical_clan_name": "Eff-it", "source_alias": "Eff-it"},
    {"active": "TRUE", "clan_tag": "C1C-B", "canonical_clan_name": "Eff-it", "source_alias": "Eff it"},
    {"active": "FALSE", "clan_tag": "OLD", "canonical_clan_name": "Former", "source_alias": "Old Clan"},
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
    record_tabs = {"Config": CONFIG, "Capture Specs": specs, "Clan Registry": CLANS if clans is None else clans}

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
        sources={"Live Input": [["C1C Cambion", "x", 125], ["Old Clan", "x", 999], ["Unknown!", "x", 8]]},
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
    sheets = install(monkeypatch, specs=[weekly_spec()], sources={"Live Input": [["Cambion", "", score]]})
    run_capture()
    alpha = appended_dicts(sheets["Archive"])[0]
    assert alpha["score"] == ""
    assert alpha["evaluation_status"] == "missing"


def test_alias_collision_is_rejected(monkeypatch):
    clans = CLANS + [{"active": "TRUE", "clan_tag": "C1C-X", "canonical_clan_name": "Other", "source_alias": "Cambion"}]
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
        "Siege Input": [["Cambion", 12, "C1C Cambion", 10], ["Eff-it", 7, "Eff it", 7]]
    })
    summary = run_capture()
    rows = appended_dicts(sheets["Archive"])
    assert [(row["score"], row["result"]) for row in rows] == [(2, "win"), (0, "loss")]
    assert all(row["score_unit"] == "wins" for row in rows)
    assert summary.result_only_rows == 2


def test_negative_delta_aborts_without_append(monkeypatch):
    sheets = install(monkeypatch, specs=[delta_spec()], sources={"Siege Input": [["Cambion", 9, "Cambion", 10]]})
    with pytest.raises(history.HistoryCaptureError, match="negative cumulative delta"):
        run_capture()
    assert sheets["Archive"].appended == []


def test_identical_retry_dedupes_and_conflict_never_overwrites(monkeypatch):
    sheets = install(monkeypatch, specs=[weekly_spec()], sources={"Live Input": [["Cambion", "", 5]]})
    run_capture(captured_at=history.dt.datetime(2026, 8, 3, tzinfo=history.dt.timezone.utc))
    rows = sheets["Archive"].appended[0][0]
    archive = [HEADERS, *rows]

    sheets = install(monkeypatch, specs=[weekly_spec()], sources={"Live Input": [["Cambion", "", 5]]}, archive=archive)
    summary = run_capture(captured_at=history.dt.datetime(2026, 8, 4, tzinfo=history.dt.timezone.utc))
    assert summary.identical_rows == 2 and summary.appended_rows == 0
    assert sheets["Archive"].appended == []

    sheets = install(monkeypatch, specs=[weekly_spec()], sources={"Live Input": [["Cambion", "", 6]]}, archive=archive)
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

    from shared import config as shared_config

    monkeypatch.setitem(shared_config._CONFIG, "LEAGUES_SHEET_ID", "sheet")
    for key, value in {
        "LEAGUES_LEGENDARY_THREAD_ID": 1, "LEAGUES_RISING_THREAD_ID": 2,
        "LEAGUES_STORMFORGED_THREAD_ID": 3, "ANNOUNCEMENT_CHANNEL_ID": 4,
    }.items():
        monkeypatch.setitem(shared_config._CONFIG, key, value)
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

    assert asyncio.run(cog._run_leagues_job(
        trigger="command", status_channel=channel, week_key="2026-W31"
    )) is False
    assert events == ["capture", "status"]


def test_configured_columns_accept_both_a1_range_boundaries(monkeypatch):
    sheets = install(
        monkeypatch,
        specs=[weekly_spec(current_clan_column="B", score_column="D")],
        sources={"Live Input": [["Cambion", "ignored", 9]]},
    )
    summary = run_capture()
    assert summary.appended_rows == 2
    assert sheets["Live Input"].get_calls


@pytest.mark.parametrize(
    ("column", "message"),
    [("D7", "invalid source column"), ("A", "outside configured range"), ("E", "outside configured range")],
)
def test_configured_column_malformed_or_outside_a1_range_aborts_before_read(
    monkeypatch, column, message
):
    sheets = install(
        monkeypatch,
        specs=[weekly_spec(score_column=column)],
        sources={"Live Input": [["Cambion", "ignored", 9]]},
    )
    with pytest.raises(history.HistoryCaptureError, match=message):
        run_capture()
    assert sheets["Live Input"].get_calls == []
    assert sheets["Archive"].appended == []


@pytest.mark.parametrize(
    ("first_trigger", "retry_trigger"),
    [("reaction_approval", "command"), ("command", "reaction_approval")],
)
def test_cross_trigger_retry_uses_semantic_data_and_preserves_original_audit(
    monkeypatch, first_trigger, retry_trigger
):
    sheets = install(monkeypatch, specs=[weekly_spec()], sources={"Live Input": [["Cambion", "", 5]]})
    first = asyncio.run(history.capture_weekly_history(
        "sheet", config_tab="Config", week_key="2026-W31", trigger=first_trigger,
        captured_at=history.dt.datetime(2026, 8, 3, tzinfo=history.dt.timezone.utc),
    ))
    assert first.appended_rows == 2
    original = [list(row) for row in sheets["Archive"].appended[0][0]]
    archive = [HEADERS, *original]

    moved_spec = weekly_spec(source_range="B6:D10")
    sheets = install(monkeypatch, specs=[moved_spec], sources={"Live Input": [["Cambion", "", 5]]}, archive=archive)
    retry = asyncio.run(history.capture_weekly_history(
        "sheet", config_tab="Config", week_key="2026-W31", trigger=retry_trigger,
        captured_at=history.dt.datetime(2026, 8, 4, tzinfo=history.dt.timezone.utc),
    ))

    assert retry.identical_rows == 2 and retry.appended_rows == 0
    assert sheets["Archive"].appended == []
    # Append-only history retains the original trigger, timestamp, range, and row.
    assert archive[1:] == original
    alpha = dict(zip(HEADERS, archive[1]))
    assert alpha["source_trigger"] == first_trigger
    assert alpha["source_range"] == "Live Input!B5:D9"
    assert alpha["source_row"] == 5
    assert alpha["captured_at_utc"].startswith("2026-08-03")


def test_semantic_score_conflict_still_aborts_without_overwrite(monkeypatch):
    sheets = install(monkeypatch, specs=[weekly_spec()], sources={"Live Input": [["Cambion", "", 5]]})
    run_capture()
    original = [list(row) for row in sheets["Archive"].appended[0][0]]
    archive = [HEADERS, *original]
    sheets = install(monkeypatch, specs=[weekly_spec()], sources={"Live Input": [["Cambion", "", 6]]}, archive=archive)

    with pytest.raises(history.HistoryCaptureError, match="history-conflict"):
        asyncio.run(history.capture_weekly_history(
            "sheet", config_tab="Config", week_key="2026-W31", trigger="command"
        ))
    assert sheets["Archive"].appended == []
    assert archive[1:] == original


def test_semantic_result_conflict_still_aborts(monkeypatch):
    sheets = install(monkeypatch, specs=[delta_spec()], sources={
        "Siege Input": [["Cambion", 12, "Cambion", 10], ["Eff-it", 7, "Eff-it", 7]]
    })
    run_capture()
    original = [list(row) for row in sheets["Archive"].appended[0][0]]
    archive = [HEADERS, *original]
    result_index = HEADERS.index("result")
    archive[1][result_index] = "loss"
    sheets = install(monkeypatch, specs=[delta_spec()], sources={
        "Siege Input": [["Cambion", 12, "Cambion", 10], ["Eff-it", 7, "Eff-it", 7]]
    }, archive=archive)

    with pytest.raises(history.HistoryCaptureError, match="history-conflict"):
        run_capture()
    assert sheets["Archive"].appended == []


def test_concurrent_manual_and_reaction_jobs_keep_explicit_week_keys():
    from modules.community.leagues.cog import LeaguesCog

    cog = LeaguesCog(SimpleNamespace())
    seen = []

    async def job(*, trigger, status_channel, week_key):
        await asyncio.sleep(0)
        seen.append((trigger, week_key))
        return True

    cog._run_leagues_job = job

    async def run_both():
        return await asyncio.gather(
            cog.run_leagues_job(
                trigger="command", status_channel=None, week_key="2026-W32"
            ),
            cog.run_leagues_job(
                trigger="reaction_approval", status_channel=None, week_key="2025-W52"
            ),
        )

    assert asyncio.run(run_both()) == [True, True]
    assert seen == [("command", "2026-W32"), ("reaction_approval", "2025-W52")]


def test_active_map_with_live_headers_supports_duplicate_alias_rows():
    clans, aliases = history.build_active_clan_map(CLANS)
    assert set(clans) == {"C1C-A", "C1C-B"}
    assert clans["C1C-A"] == ("C1C-A", "Cambion")
    assert aliases[history.normalize_alias("Cambion")] == "C1C-A"
    assert aliases[history.normalize_alias("C1C Cambion")] == "C1C-A"
    assert aliases[history.normalize_alias("Eff-it")] == "C1C-B"
    assert aliases[history.normalize_alias("Eff it")] == "C1C-B"


def test_zero_active_clans_aborts_without_source_read_or_append(monkeypatch):
    sheets = install(
        monkeypatch,
        specs=[weekly_spec()],
        sources={"Live Input": [["Cambion", "", 5]]},
        clans=[
            {
                "active": "FALSE",
                "clan_tag": "C1C-A",
                "canonical_clan_name": "Cambion",
                "source_alias": "Cambion",
            }
        ],
    )
    with pytest.raises(history.HistoryCaptureError, match="zero active clans"):
        run_capture()
    assert sheets["Live Input"].get_calls == []
    assert sheets["Archive"].appended == []


def test_populated_source_with_zero_active_matches_aborts_without_append(monkeypatch):
    sheets = install(
        monkeypatch,
        specs=[weekly_spec()],
        sources={"Live Input": [["Former clan", "", 5], ["Unknown clan", "", 7]]},
    )
    with pytest.raises(history.HistoryCaptureError, match="zero clans match"):
        run_capture()
    assert sheets["Live Input"].get_calls
    assert sheets["Archive"].appended == []
