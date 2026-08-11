from __future__ import annotations

import asyncio
from copy import deepcopy
from unittest.mock import AsyncMock

import pytest

from modules.community.live_arena.registration import RegistrationError
from modules.community.live_arena import swiss_refresh
from modules.community.live_arena.swiss_refresh import regenerate_current_preview

TID = "T1"


def run(awaitable):
    return asyncio.run(awaitable)


class Repo:
    def __init__(self, fingerprint="old"):
        self.r = [
            {
                "tournament_id": TID,
                "round_id": f"{TID}-Q2",
                "round_number": "2",
                "status": "approved",
                "notes": f"swiss_source_fingerprint={fingerprint}",
                "approved_at_utc": "2026-08-11T10:00:00Z",
                "approved_by_discord_user_id": "42",
            }
        ]
        self.m = [
            {
                "tournament_id": TID,
                "round_id": f"{TID}-Q1",
                "match_id": "Q1-M1",
                "status": "finalized",
                "final_result_type": "played",
                "final_score_a": "2",
                "final_score_b": "0",
                "final_winner_discord_user_id": "1",
            }
        ]

    async def rounds(self):
        return deepcopy(self.r)

    async def matches(self):
        return deepcopy(self.m)

    async def persist_rounds(self, rounds, *, previous_rounds):
        self.r = deepcopy(rounds)


class Service:
    def __init__(self, repo):
        self.sheet_id = "sheet"
        self.repository = repo
        self.calls = []

    async def generate_preview(self, actor_id, round_number, *, regenerate=False):
        self.calls.append((actor_id, round_number, regenerate))
        return "regenerated"


def test_stale_approved_draw_is_demoted_then_regenerated(monkeypatch):
    repo = Repo("definitely-stale")
    service = Service(repo)
    monkeypatch.setattr(
        swiss_refresh,
        "load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": TID}),
    )

    result = run(regenerate_current_preview(service, "7", 2))

    assert result == "regenerated"
    assert repo.r[0]["status"] == "preview"
    assert repo.r[0]["approved_at_utc"] == ""
    assert repo.r[0]["approved_by_discord_user_id"] == ""
    assert service.calls == [("7", 2, True)]


def test_current_approved_draw_cannot_be_regenerated_by_preference(monkeypatch):
    repo = Repo("placeholder")
    service = Service(repo)
    monkeypatch.setattr(
        swiss_refresh,
        "load_config",
        AsyncMock(return_value={"ACTIVE_TOURNAMENT_ID": TID}),
    )
    current = swiss_refresh.source_fingerprint(repo.m, TID, before_round=2)
    repo.r[0]["notes"] = f"swiss_source_fingerprint={current}"

    with pytest.raises(RegistrationError, match="cannot be reshuffled"):
        run(regenerate_current_preview(service, "7", 2))

    assert repo.r[0]["status"] == "approved"
    assert service.calls == []
