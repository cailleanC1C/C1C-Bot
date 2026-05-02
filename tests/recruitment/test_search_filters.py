from modules.recruitment import search
from shared.sheets.recruitment import RecruitmentClanRecord


def _record(
    cb: str,
    hydra: str,
    chimera: str,
    open_spots: int = 1,
    playstyle: str = "",
) -> RecruitmentClanRecord:
    row = [""] * 24
    row[15] = cb
    row[16] = hydra
    row[17] = chimera
    row[18] = ""
    row[19] = ""
    row[20] = playstyle
    return RecruitmentClanRecord(
        row=tuple(row),
        open_spots=open_spots,
        inactives=0,
        reserved=0,
        roster="active",
    )


def test_combined_difficulty_filters_normalize_labels(monkeypatch):
    monkeypatch.setattr(
        search.sheet_recruitment,
        "get_clan_header_map",
        lambda: {"cb": 15, "hydra": 16, "chimera": 17, "cb_range": 21, "hydra_range": 22, "chimera_range": 23},
    )
    records = [_record("UNM", "NM", "", open_spots=2), _record("BTL", "HRD", "", open_spots=2)]

    matches, diagnostics = search.filter_records_with_diagnostics(
        records,
        cb="Ultra Nightmare",
        hydra="Nightmare",
        chimera="—",
        cvc="—",
        siege="",
        playstyle=None,
        roster_mode="open",
    )

    assert len(matches) == 1
    assert matches[0].row[15] == "UNM"
    assert diagnostics["initial_clans"] == 2
    assert diagnostics["after_open_spots_filter"] == 2
    assert diagnostics["after_cb_filter"] == 1
    assert diagnostics["after_hydra_filter"] == 1


def test_blank_filters_do_not_participate(monkeypatch):
    monkeypatch.setattr(
        search.sheet_recruitment,
        "get_clan_header_map",
        lambda: {"cb": 15, "hydra": 16, "chimera": 17, "cb_range": 21, "hydra_range": 22, "chimera_range": 23},
    )
    records = [_record("UNM", "NM", "", open_spots=0), _record("UNM", "NM", "", open_spots=3)]

    matches, diagnostics = search.filter_records_with_diagnostics(
        records,
        cb="UNM",
        hydra="-",
        chimera="None",
        cvc="—",
        siege="blank",
        playstyle="",
        roster_mode="open",
    )

    assert len(matches) == 1
    assert matches[0].open_spots == 3
    assert diagnostics["after_open_spots_filter"] == 1


def test_cvc_siege_filters_support_normalized_unset_values(monkeypatch):
    monkeypatch.setattr(
        search.sheet_recruitment,
        "get_clan_header_map",
        lambda: {"cb": 15, "hydra": 16, "chimera": 17, "cb_range": 21, "hydra_range": 22, "chimera_range": 23},
    )
    open_row = [""] * 24
    open_row[15] = "UNM"
    open_row[16] = "NM"
    open_row[18] = "1"
    open_row[19] = "0"
    record = RecruitmentClanRecord(row=tuple(open_row), open_spots=3, inactives=0, reserved=0, roster="active")

    matches, _ = search.filter_records_with_diagnostics(
        [record],
        cb="UNM",
        hydra="NM",
        chimera=None,
        cvc="1",
        siege="0",
        playstyle=None,
        roster_mode="open",
    )
    assert len(matches) == 1

    matches_blank, _ = search.filter_records_with_diagnostics(
        [record],
        cb="UNM",
        hydra="NM",
        chimera=None,
        cvc="—",
        siege="-",
        playstyle=None,
        roster_mode="open",
    )
    assert len(matches_blank) == 1


def test_blank_playstyle_does_not_filter(monkeypatch):
    monkeypatch.setattr(
        search.sheet_recruitment,
        "get_clan_header_map",
        lambda: {"cb": 15, "hydra": 16, "chimera": 17, "cb_range": 21, "hydra_range": 22, "chimera_range": 23},
    )
    records = [_record("UNM", "NM", "", open_spots=2, playstyle="Casual")]
    for blank_value in ("", "—", "-", "Any", "None", "Blank", "Unset", None):
        matches, _ = search.filter_records_with_diagnostics(
            records,
            cb="UNM",
            hydra="NM",
            chimera=None,
            cvc=None,
            siege=None,
            playstyle=blank_value,
            roster_mode="open",
        )
        assert len(matches) == 1


def test_playstyle_value_passes_through_unchanged(monkeypatch):
    monkeypatch.setattr(
        search.sheet_recruitment,
        "get_clan_header_map",
        lambda: {"cb": 15, "hydra": 16, "chimera": 17, "cb_range": 21, "hydra_range": 22, "chimera_range": 23},
    )
    records = [
        _record("UNM", "NM", "", open_spots=2, playstyle="Casual"),
        _record("UNM", "NM", "", open_spots=2, playstyle="Competitive"),
    ]

    matches, _ = search.filter_records_with_diagnostics(
        records,
        cb="UNM",
        hydra="Nightmare",
        chimera=None,
        cvc=None,
        siege=None,
        playstyle="Casual",
        roster_mode="open",
    )
    assert len(matches) == 1
    assert matches[0].row[20] == "Casual"
