import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from modules.community.live_arena import messages, next_tournament, service, tournament_motto


def _tournament_matrix(*, motto_header=True):
    headers = list(service.TOURNAMENT_HEADERS)
    if motto_header:
        headers.append("tournament_motto")
    values = {header: "" for header in headers}
    values.update(
        tournament_id="OLD",
        tournament_name="Old Cup",
        status="archived",
        eligibility_scope="selected_clans",
        min_participants="8",
        max_participants="16",
        signup_opens_at_utc="2026-08-01T10:00:00Z",
        signup_closes_at_utc="2026-08-05T10:00:00Z",
        tournament_short_name="Old",
        timezone="UTC",
    )
    if motto_header:
        values["tournament_motto"] = "Old motto"
    return [headers, [values.get(header, "") for header in headers]]


def test_tournaments_accept_only_the_optional_motto_header():
    matrix = _tournament_matrix(motto_header=True)
    rows = service._rows(matrix, service.TOURNAMENT_HEADERS, "TOURNAMENTS")
    assert rows[0]["tournament_motto"] == "Old motto"

    bad = [matrix[0] + ["surprise"], matrix[1] + ["nope"]]
    with pytest.raises(service.LiveArenaConfigError, match="unexpected header: surprise"):
        service._rows(bad, service.TOURNAMENT_HEADERS, "TOURNAMENTS")


def test_next_tournament_draft_carries_optional_motto_through_replace():
    draft = next_tournament.NextTournamentDraft(tournament_name="Cutlass & Chaos Cup")
    assert draft.tournament_motto == ""
    themed = replace(
        draft,
        tournament_motto="No maps. No mercy. Just glorious bad decisions.",
    )
    assert themed.tournament_motto.startswith("No maps")


def test_public_signup_embed_places_motto_beneath_first_description_line():
    template = messages.MessageTemplate(
        "signup_open",
        "C1C Tournament signups are open",
        (
            "# {tournament_name} is open for signups.\n"
            "Submit by {signup_deadline}.\n"
            "Confirmed: {confirmed_count}/{max_participants}."
        ),
        0x1A73E8,
    )
    token = tournament_motto._public_motto.set(
        "No maps. No mercy. Just glorious bad decisions."
    )
    try:
        embed = template.embed(
            tournament_name="Cutlass & Chaos Cup",
            signup_deadline="soon",
            confirmed_count=0,
            max_participants=16,
        )
    finally:
        tournament_motto._public_motto.reset(token)

    lines = embed.description.splitlines()
    assert lines[0] == "# Cutlass & Chaos Cup is open for signups."
    assert lines[1] == "*No maps. No mercy. Just glorious bad decisions.*"


def test_create_next_tournament_writes_motto_in_live_header_order(monkeypatch):
    now = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)
    tournaments = _tournament_matrix(motto_header=True)
    clans = [
        list(next_tournament.ELIGIBLE_CLAN_HEADERS),
        ["OLD", "C1C", "Pirate Crew", "42", "TRUE", ""],
    ]
    config_matrix = [
        list(next_tournament.CONFIG_HEADERS),
        ["ACTIVE_TOURNAMENT_ID", "OLD", ""],
        ["PUBLIC_PANEL_MESSAGE_ID", "123", ""],
        ["ORGANIZER_PANEL_MESSAGE_ID", "456", ""],
    ]

    async def fake_snapshot(_sheet_id):
        return SimpleNamespace(status="archived", tournament_id="OLD")

    async def fake_config(_sheet_id):
        return {
            "TOURNAMENTS_TAB": "TOURNAMENTS",
            "ELIGIBLE_CLANS_TAB": "ELIGIBLE_CLANS",
        }

    async def fake_fetch(_sheet_id, tab):
        return {
            "TOURNAMENTS": tournaments,
            "ELIGIBLE_CLANS": clans,
            next_tournament.CONFIG_TAB: config_matrix,
        }[tab]

    retired = []
    audits = []

    class FakeRepository:
        def __init__(self, _sheet_id):
            pass

        async def initialize(self):
            pass

        async def retire_discord_resources(self, tournament_id, *, updated_at_utc):
            retired.append((tournament_id, updated_at_utc))

        async def append_audit(self, row):
            audits.append(row)

    captured = []

    async def fake_get_worksheet(_sheet_id, _tab):
        return SimpleNamespace(
            spreadsheet=SimpleNamespace(values_batch_update=object())
        )

    async def fake_acall(_callable, *args, **kwargs):
        captured.append(kwargs["body"])

    monkeypatch.setattr(next_tournament, "load_tournament_snapshot", fake_snapshot)
    monkeypatch.setattr(next_tournament, "load_config", fake_config)
    monkeypatch.setattr(next_tournament, "afetch_values", fake_fetch)
    monkeypatch.setattr(next_tournament, "LiveArenaRepository", FakeRepository)
    monkeypatch.setattr(next_tournament, "aget_worksheet", fake_get_worksheet)
    monkeypatch.setattr(next_tournament, "acall_with_backoff", fake_acall)

    draft = next_tournament.NextTournamentDraft(
        tournament_name="Cutlass & Chaos Cup",
        short_name="Cutlass & Chaos",
        min_participants=8,
        max_participants=16,
        timezone="UTC",
        signup_opens_at_utc="2026-08-20T10:00:00Z",
        signup_closes_at_utc="2026-08-25T10:00:00Z",
        eligible_role_ids=("42",),
        tournament_motto="No maps. No mercy. Just glorious bad decisions.",
    )
    creator = next_tournament.NextTournamentService("sheet", clock=lambda: now)
    new_id = asyncio.run(creator.create("99", draft))

    assert new_id == "LA-20260819-100000"
    assert retired and retired[0][0] == "OLD"
    write = captured[0]
    tournament_write = write["data"][0]
    assert tournament_write["range"] == "'TOURNAMENTS'!A3:O3"
    assert tournament_write["values"][0][-1] == draft.tournament_motto
    assert audits and "tournament_motto" in audits[0]["details"]


def test_create_with_motto_refuses_missing_header_before_retiring_history(monkeypatch):
    now = datetime(2026, 8, 19, 10, 0, tzinfo=UTC)
    tournaments = _tournament_matrix(motto_header=False)
    clans = [
        list(next_tournament.ELIGIBLE_CLAN_HEADERS),
        ["OLD", "C1C", "Pirate Crew", "42", "TRUE", ""],
    ]
    config_matrix = [
        list(next_tournament.CONFIG_HEADERS),
        ["ACTIVE_TOURNAMENT_ID", "OLD", ""],
        ["PUBLIC_PANEL_MESSAGE_ID", "123", ""],
        ["ORGANIZER_PANEL_MESSAGE_ID", "456", ""],
    ]

    async def fake_snapshot(_sheet_id):
        return SimpleNamespace(status="archived", tournament_id="OLD")

    async def fake_config(_sheet_id):
        return {
            "TOURNAMENTS_TAB": "TOURNAMENTS",
            "ELIGIBLE_CLANS_TAB": "ELIGIBLE_CLANS",
        }

    async def fake_fetch(_sheet_id, tab):
        return {
            "TOURNAMENTS": tournaments,
            "ELIGIBLE_CLANS": clans,
            next_tournament.CONFIG_TAB: config_matrix,
        }[tab]

    retired = []

    class FakeRepository:
        def __init__(self, _sheet_id):
            pass

        async def initialize(self):
            pass

        async def retire_discord_resources(self, tournament_id, *, updated_at_utc):
            retired.append(tournament_id)

    monkeypatch.setattr(next_tournament, "load_tournament_snapshot", fake_snapshot)
    monkeypatch.setattr(next_tournament, "load_config", fake_config)
    monkeypatch.setattr(next_tournament, "afetch_values", fake_fetch)
    monkeypatch.setattr(next_tournament, "LiveArenaRepository", FakeRepository)

    draft = next_tournament.NextTournamentDraft(
        tournament_name="Cutlass & Chaos Cup",
        short_name="Cutlass & Chaos",
        signup_opens_at_utc="2026-08-20T10:00:00Z",
        signup_closes_at_utc="2026-08-25T10:00:00Z",
        eligible_role_ids=("42",),
        tournament_motto="Chaos",
    )
    creator = next_tournament.NextTournamentService("sheet", clock=lambda: now)

    with pytest.raises(service.LiveArenaConfigError, match="tournament_motto header"):
        asyncio.run(creator.create("99", draft))
    assert retired == []
