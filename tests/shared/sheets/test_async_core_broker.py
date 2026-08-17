import asyncio

import shared.sheets.async_core as async_core
import shared.sheets.core as core
from shared.sheets.read_broker import STATIC_CONFIG, SheetsReadBroker


def _new_broker():
    return SheetsReadBroker(
        read_budget_rpm=1_000_000,
        rate_window_seconds=0.001,
        retry_attempts=1,
        retry_base_delay_sec=0,
    )


def test_concurrent_async_records_use_one_physical_read(monkeypatch):
    async def run():
        test_broker = _new_broker()
        monkeypatch.setattr(async_core, "broker", test_broker)
        core._WorkbookCache.clear()
        core._WorksheetCache.clear()

        client = object()
        workbook = object()
        worksheet = object()
        calls = {"open": 0, "worksheet": 0, "records": 0}

        async def fake_arun(func, *args, **kwargs):
            if func is core.get_service_account_client:
                return client
            return func(*args, **kwargs)

        async def open_spreadsheet(got_client, sheet_id, **kwargs):
            assert got_client is client
            assert sheet_id == "sheet"
            calls["open"] += 1
            await asyncio.sleep(0)
            return workbook

        async def worksheet_by_title(got_workbook, name, **kwargs):
            assert got_workbook is workbook
            assert name == "Config"
            calls["worksheet"] += 1
            await asyncio.sleep(0)
            return worksheet

        async def records_all(got_worksheet, **kwargs):
            assert got_worksheet is worksheet
            calls["records"] += 1
            await asyncio.sleep(0.01)
            return [{"Key": "A", "Value": "B"}]

        monkeypatch.setattr(core.async_adapter, "arun", fake_arun)
        monkeypatch.setattr(core.async_adapter, "aopen_spreadsheet", open_spreadsheet)
        monkeypatch.setattr(core.async_adapter, "aworksheet_by_title", worksheet_by_title)
        monkeypatch.setattr(core.async_adapter, "aworksheet_records_all", records_all)

        results = await asyncio.gather(
            *(async_core.afetch_records("sheet", "Config") for _ in range(50))
        )

        assert all(result == [{"Key": "A", "Value": "B"}] for result in results)
        assert calls == {"open": 1, "worksheet": 1, "records": 1}
        assert test_broker.snapshot()["coalesced_joins"] >= 49
        await test_broker.close()

    asyncio.run(run())


def test_default_reads_remain_fresh_required_but_static_config_reuses_cache(monkeypatch):
    async def run():
        test_broker = _new_broker()
        monkeypatch.setattr(async_core, "broker", test_broker)
        core._WorkbookCache.clear()
        core._WorksheetCache.clear()

        client = object()
        workbook = object()
        worksheet = object()
        record_calls = 0

        async def fake_arun(func, *args, **kwargs):
            if func is core.get_service_account_client:
                return client
            return func(*args, **kwargs)

        async def open_spreadsheet(*args, **kwargs):
            return workbook

        async def worksheet_by_title(*args, **kwargs):
            return worksheet

        async def records_all(*args, **kwargs):
            nonlocal record_calls
            record_calls += 1
            return [{"n": record_calls}]

        monkeypatch.setattr(core.async_adapter, "arun", fake_arun)
        monkeypatch.setattr(core.async_adapter, "aopen_spreadsheet", open_spreadsheet)
        monkeypatch.setattr(core.async_adapter, "aworksheet_by_title", worksheet_by_title)
        monkeypatch.setattr(core.async_adapter, "aworksheet_records_all", records_all)

        first = await async_core.afetch_records("sheet", "State")
        second = await async_core.afetch_records("sheet", "State")
        assert first == [{"n": 1}]
        assert second == [{"n": 2}]

        static_first = await async_core.afetch_records(
            "sheet", "Config", policy=STATIC_CONFIG
        )
        static_second = await async_core.afetch_records(
            "sheet", "Config", policy=STATIC_CONFIG
        )
        assert static_first == [{"n": 3}]
        assert static_second == [{"n": 3}]
        assert record_calls == 3
        await test_broker.close()

    asyncio.run(run())
