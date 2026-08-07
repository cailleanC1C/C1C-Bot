from __future__ import annotations

import asyncio

from types import SimpleNamespace

import pytest

from cogs import live_arena as cog
from modules.community.live_arena import service
from shared import config as shared_config


def _config_rows(**overrides: str) -> list[list[object]]:
    values = {
        "ACTIVE_TOURNAMENT_ID": "LA-2026-TRIAL-01",
        "TOURNAMENTS_TAB": "TOURNAMENTS",
        "ELIGIBLE_CLANS_TAB": "ELIGIBLE_CLANS",
        "AVAILABILITY_SLOTS_TAB": "AVAILABILITY_SLOTS",
        "ORGANIZER_ROLE_ID": "1535031755734777967",
        "PARTICIPANTS_TAB": "PARTICIPANTS",
        "PARTICIPANT_AVAILABILITY_TAB": "PARTICIPANT_AVAILABILITY",
        "AUDIT_LOG_TAB": "AUDIT_LOG",
        **overrides,
    }
    return [
        list(service.CONFIG_HEADERS),
        *[[key, value, ""] for key, value in values.items()],
    ]


def _tournament_rows(
    headers: tuple[str, ...] = service.TOURNAMENT_HEADERS,
) -> list[list[object]]:
    row = {
        "tournament_id": "LA-2026-TRIAL-01",
        "tournament_name": "C1C Live Arena Trial Cup",
        "status": "draft",
        "eligibility_scope": "selected_clans",
        "min_participants": "8",
        "max_participants": "16",
        "signup_opens_at_utc": "",
        "signup_closes_at_utc": "",
        "notes": "",
    }
    return [list(headers), *[[row[header] for header in headers]]]


def _clan_rows() -> list[list[object]]:
    return [
        list(service.ELIGIBLE_CLAN_HEADERS),
        ["LA-2026-TRIAL-01", "C1CM", "Martyrs", "706438713332465694", "TRUE", ""],
        ["OTHER", "C1CX", "Other", "1", "TRUE", ""],
        ["LA-2026-TRIAL-01", "C1CY", "Inactive", "2", "FALSE", ""],
    ]


def _slot_rows(count: int = 84) -> list[list[object]]:
    weekdays = tuple(service.WEEKDAYS)
    return [
        list(service.AVAILABILITY_SLOT_HEADERS),
        *[
            [
                f"slot-{index}",
                weekdays[index % 7],
                "00:00",
                "02:00",
                "TRUE",
                index,
                f"Slot {index}",
            ]
            for index in range(count)
        ],
    ]


def _matrices(
    config: list[list[object]] | None = None,
) -> dict[str, list[list[object]]]:
    return {
        "CONFIG": config or _config_rows(),
        "TOURNAMENTS": _tournament_rows(),
        "ELIGIBLE_CLANS": _clan_rows(),
        "AVAILABILITY_SLOTS": _slot_rows(),
    }


def _install_fetch(monkeypatch, matrices, seen=None):
    async def fetch(sheet_id, tab):
        assert sheet_id == "sheet-id"
        if seen is not None:
            seen.append(tab)
        return matrices[tab]

    monkeypatch.setattr(service, "afetch_values", fetch)


def test_missing_sheet_id_disables_live_arena_safely(monkeypatch, caplog):
    monkeypatch.setitem(shared_config._CONFIG, "LIVE_ARENA_TOURNAMENT_SHEET_ID", "")
    caplog.set_level("INFO", logger="c1c.community.live_arena")
    cog.LiveArenaCog(SimpleNamespace())
    assert "Live Arena — disabled" in caplog.text


def test_env_key_reaches_shared_config_snapshot(monkeypatch):
    monkeypatch.setenv("LIVE_ARENA_TOURNAMENT_SHEET_ID", "workbook-from-env")
    assert (
        shared_config._load_config_snapshot()["LIVE_ARENA_TOURNAMENT_SHEET_ID"]
        == "workbook-from-env"
    )


def test_config_is_read_by_actual_name(monkeypatch):
    seen = []
    _install_fetch(monkeypatch, _matrices(), seen)
    asyncio.run(service.load_config("sheet-id"))
    assert seen == ["CONFIG"]


def test_table_names_are_routed_from_config(monkeypatch):
    matrices = _matrices(
        _config_rows(
            TOURNAMENTS_TAB="EVENTS",
            ELIGIBLE_CLANS_TAB="CLANS",
            AVAILABILITY_SLOTS_TAB="SLOTS",
        )
    )
    matrices.update(
        EVENTS=matrices.pop("TOURNAMENTS"),
        CLANS=matrices.pop("ELIGIBLE_CLANS"),
        SLOTS=matrices.pop("AVAILABILITY_SLOTS"),
    )
    seen = []
    _install_fetch(monkeypatch, matrices, seen)
    asyncio.run(service.load_tournament_snapshot("sheet-id"))
    assert seen == ["CONFIG", "EVENTS", "CLANS", "SLOTS"]


def test_active_tournament_comes_from_config(monkeypatch):
    matrices = _matrices(_config_rows(ACTIVE_TOURNAMENT_ID="missing-id"))
    _install_fetch(monkeypatch, matrices)
    with pytest.raises(
        service.LiveArenaConfigError, match="active tournament not found: missing-id"
    ):
        asyncio.run(service.load_tournament_snapshot("sheet-id"))


def test_exact_tournament_headers_pass(monkeypatch):
    _install_fetch(monkeypatch, _matrices())
    assert (
        asyncio.run(service.load_tournament_snapshot("sheet-id"))
    ).tournament_id == "LA-2026-TRIAL-01"


def test_missing_tournament_header_has_useful_error(monkeypatch):
    matrices = _matrices()
    matrices["TOURNAMENTS"] = _tournament_rows(
        tuple(h for h in service.TOURNAMENT_HEADERS if h != "status")
    )
    _install_fetch(monkeypatch, matrices)
    with pytest.raises(
        service.LiveArenaConfigError,
        match="TOURNAMENTS: required header missing: status",
    ):
        asyncio.run(service.load_tournament_snapshot("sheet-id"))


def test_exact_eligible_clan_headers_pass(monkeypatch):
    _install_fetch(monkeypatch, _matrices())
    assert (
        asyncio.run(service.load_tournament_snapshot("sheet-id"))
    ).active_eligible_clans == 1


def test_exact_availability_headers_pass_without_tournament_id(monkeypatch):
    assert "tournament_id" not in service.AVAILABILITY_SLOT_HEADERS
    _install_fetch(monkeypatch, _matrices())
    assert (
        asyncio.run(service.load_tournament_snapshot("sheet-id"))
    ).enabled_availability_windows == 84


def test_availability_uses_enabled_never_active():
    assert "enabled" in service.AVAILABILITY_SLOT_HEADERS
    assert "active" not in service.AVAILABILITY_SLOT_HEADERS


def test_weekday_names_are_accepted(monkeypatch):
    matrices = _matrices()
    matrices["AVAILABILITY_SLOTS"][1][1] = "Monday"
    _install_fetch(monkeypatch, matrices)
    assert (
        asyncio.run(service.load_tournament_snapshot("sheet-id"))
    ).enabled_availability_windows == 84


def test_current_fixture_has_84_enabled_windows(monkeypatch):
    _install_fetch(monkeypatch, _matrices())
    assert (
        asyncio.run(service.load_tournament_snapshot("sheet-id"))
    ).enabled_availability_windows == 84


def test_blank_signup_timestamps_are_valid_for_draft(monkeypatch):
    _install_fetch(monkeypatch, _matrices())
    snapshot = asyncio.run(service.load_tournament_snapshot("sheet-id"))
    assert (
        snapshot.status,
        snapshot.signup_opens_at_utc,
        snapshot.signup_closes_at_utc,
    ) == ("draft", "", "")


class _Context:
    def __init__(self, role_ids):
        self.author = SimpleNamespace(
            roles=[SimpleNamespace(id=value) for value in role_ids]
        )
        self.messages = []

    async def send(self, content=None, *, embed=None):
        self.messages.append((content, embed))


def test_check_rejects_user_without_configured_organizer_role(monkeypatch):
    monkeypatch.setitem(
        shared_config._CONFIG, "LIVE_ARENA_TOURNAMENT_SHEET_ID", "sheet-id"
    )
    _install_fetch(monkeypatch, _matrices())
    ctx = _Context([])
    instance = cog.LiveArenaCog(SimpleNamespace())
    asyncio.run(cog.LiveArenaCog.check.callback(instance, ctx))
    assert ctx.messages[0][0] is None
    assert "organizer role" in ctx.messages[0][1].description


def test_bare_command_uses_embed(monkeypatch):
    monkeypatch.setitem(
        shared_config._CONFIG, "LIVE_ARENA_TOURNAMENT_SHEET_ID", "sheet-id"
    )
    ctx = _Context([])
    instance = cog.LiveArenaCog(SimpleNamespace())
    asyncio.run(cog.LiveArenaCog.latournament.callback(instance, ctx))
    assert ctx.messages[0][0] is None
    assert "latournament check" in ctx.messages[0][1].description


def test_healthy_check_embed_has_expected_values(monkeypatch):
    monkeypatch.setitem(
        shared_config._CONFIG, "LIVE_ARENA_TOURNAMENT_SHEET_ID", "sheet-id"
    )
    _install_fetch(monkeypatch, _matrices())
    ctx = _Context([1535031755734777967])
    instance = cog.LiveArenaCog(SimpleNamespace())
    asyncio.run(cog.LiveArenaCog.check.callback(instance, ctx))
    fields = {field.name: field.value for field in ctx.messages[0][1].fields}
    assert fields == {
        "Tournament": "C1C Live Arena Trial Cup",
        "Tournament ID": "LA-2026-TRIAL-01",
        "Status": "draft",
        "Eligibility scope": "selected_clans",
        "Active eligible clans": "1",
        "Enabled availability windows": "84",
        "Participants": "8–16",
        "Signup opens": "Not configured",
        "Signup closes": "Not configured",
        "Configuration": "OK",
    }
