import asyncio

from shared.sheets import recruitment


def test_async_role_map_tab_name_uses_async_config(monkeypatch):
    calls = []

    async def fake_afetch_records(sheet_id, tab):
        calls.append((sheet_id, tab))
        return [{"Key": "rolemap_tab", "Value": "ConfiguredWho"}]

    monkeypatch.setenv("RECRUITMENT_SHEET_ID", "sheet-id")
    monkeypatch.setattr(recruitment, "afetch_records", fake_afetch_records)
    recruitment._CONFIG_CACHE = {}
    recruitment._CONFIG_CACHE_TS = 0

    assert asyncio.run(recruitment.get_role_map_tab_name_async()) == "ConfiguredWho"
    assert calls == [("sheet-id", "Config")]


def test_async_reservations_tab_name_uses_async_config(monkeypatch):
    async def fake_afetch_records(sheet_id, tab):
        return [{"Key": "reservations_tab", "Value": "ConfiguredReservations"}]

    monkeypatch.setenv("RECRUITMENT_SHEET_ID", "sheet-id")
    monkeypatch.setattr(recruitment, "afetch_records", fake_afetch_records)
    recruitment._CONFIG_CACHE = {}
    recruitment._CONFIG_CACHE_TS = 0

    assert asyncio.run(recruitment.get_reservations_tab_name_async()) == "ConfiguredReservations"
