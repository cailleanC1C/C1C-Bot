import asyncio

import asyncio

import pytest

from shared.sheets import onboarding


def _reset_cache() -> None:
    onboarding._CLAN_TAGS = []
    onboarding._CLAN_TAG_TS = 0.0


def test_load_clan_tags_reads_column_b(monkeypatch) -> None:
    sample_values = [
        ["Elders", "C1CE", "Elite End"],
        ["Island Sacred", "C1C1", "Elite End"],
        ["", "", ""],
        ["no placement", "NONE", ""],
    ]

    def fake_fetch(sheet_id: str, tab: str):  # type: ignore[no-untyped-def]
        assert sheet_id == "sheet-id"
        assert tab == "ClanList"
        return sample_values

    monkeypatch.setattr(onboarding, "_sheet_id", lambda: "sheet-id", raising=False)
    monkeypatch.setattr(onboarding, "_clanlist_tab", lambda: "ClanList", raising=False)
    monkeypatch.setattr(onboarding.core, "fetch_values", fake_fetch, raising=True)
    _reset_cache()

    tags = onboarding.load_clan_tags(force=True)

    assert tags == ["C1CE", "C1C1", "NONE"]


def test_load_clan_tags_async_reads_column_b(monkeypatch) -> None:
    sample_values = [
        ["Elders", "C1CE"],
        ["Island Sacred", "C1C1"],
        ["no placement", "NONE"],
    ]

    async def fake_afetch(sheet_id: str, tab: str):  # type: ignore[no-untyped-def]
        assert sheet_id == "sheet-id"
        assert tab == "ClanList"
        return sample_values

    async def runner() -> None:
        monkeypatch.setattr(onboarding, "_sheet_id", lambda: "sheet-id", raising=False)

        async def fake_aclanlist_tab() -> str:
            return "ClanList"

        monkeypatch.setattr(
            onboarding, "_aclanlist_tab", fake_aclanlist_tab, raising=True
        )
        monkeypatch.setattr(onboarding, "afetch_values", fake_afetch, raising=True)

        tags = await onboarding._load_clan_tags_async()

        assert tags == ["C1CE", "C1C1", "NONE"]

    asyncio.run(runner())
