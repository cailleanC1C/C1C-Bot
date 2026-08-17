from __future__ import annotations

import asyncio

from shared.sheets import async_core


class _FakeBroker:
    def __init__(self) -> None:
        self.worksheet_invalidations: list[tuple[str, str]] = []
        self.workbook_invalidations: list[str] = []

    async def invalidate_worksheet(self, sheet_id: str, worksheet: str) -> int:
        self.worksheet_invalidations.append((sheet_id, worksheet))
        return 1

    async def invalidate_workbook(self, sheet_id: str) -> int:
        self.workbook_invalidations.append(sheet_id)
        return 1


class _Worksheet:
    spreadsheet_id = "sheet-123"
    title = "Config"

    def update(self, _range: str, _values: object) -> str:
        return "updated"

    def row_values(self, _row: int) -> list[str]:
        return ["header"]


class _Spreadsheet:
    id = "sheet-456"

    def batch_update(self, _body: dict[str, object]) -> str:
        return "updated"


def _install_fake_call(monkeypatch):
    async def _call(func, *args, **_kwargs):
        return func(*args)

    monkeypatch.setattr(async_core._core, "acall_with_backoff", _call)


def test_successful_worksheet_write_invalidates_worksheet(monkeypatch) -> None:
    fake_broker = _FakeBroker()
    monkeypatch.setattr(async_core, "broker", fake_broker)
    _install_fake_call(monkeypatch)

    worksheet = _Worksheet()
    result = asyncio.run(async_core.acall_with_backoff(worksheet.update, "A1", [["x"]]))

    assert result == "updated"
    assert fake_broker.worksheet_invalidations == [("sheet-123", "Config")]
    assert fake_broker.workbook_invalidations == []


def test_successful_spreadsheet_batch_update_invalidates_workbook(monkeypatch) -> None:
    fake_broker = _FakeBroker()
    monkeypatch.setattr(async_core, "broker", fake_broker)
    _install_fake_call(monkeypatch)

    spreadsheet = _Spreadsheet()
    result = asyncio.run(async_core.acall_with_backoff(spreadsheet.batch_update, {"requests": []}))

    assert result == "updated"
    assert fake_broker.workbook_invalidations == ["sheet-456"]
    assert fake_broker.worksheet_invalidations == []


def test_non_mutating_generic_call_does_not_invalidate(monkeypatch) -> None:
    fake_broker = _FakeBroker()
    monkeypatch.setattr(async_core, "broker", fake_broker)
    _install_fake_call(monkeypatch)

    worksheet = _Worksheet()
    result = asyncio.run(async_core.acall_with_backoff(worksheet.row_values, 1))

    assert result == ["header"]
    assert fake_broker.worksheet_invalidations == []
    assert fake_broker.workbook_invalidations == []


def test_failed_write_does_not_invalidate(monkeypatch) -> None:
    fake_broker = _FakeBroker()
    monkeypatch.setattr(async_core, "broker", fake_broker)

    async def _fail(*_args, **_kwargs):
        raise RuntimeError("write failed")

    monkeypatch.setattr(async_core._core, "acall_with_backoff", _fail)

    worksheet = _Worksheet()
    try:
        asyncio.run(async_core.acall_with_backoff(worksheet.update, "A1", [["x"]]))
    except RuntimeError as exc:
        assert str(exc) == "write failed"
    else:  # pragma: no cover - defensive
        raise AssertionError("expected write failure")

    assert fake_broker.worksheet_invalidations == []
    assert fake_broker.workbook_invalidations == []
