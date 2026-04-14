import asyncio
import datetime as dt

import pytest

from shared.sheets import fusion


def test_load_fusions_reads_fusion_prefixed_needed(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_fetch_records(_sheet_id: str, _tab_name: str):
        return [
            {
                "fusion_id": "f-1",
                "fusion_name": "Mavara",
                "champion": "Mavara",
                "fusion_type": "traditional",
                "fusion_structure": "",
                "reward_type": "fragments",
                "fusion.needed": "400",
                "fusion.available": "450",
                "start_at_utc": "2026-04-08T00:00:00Z",
                "end_at_utc": "2026-04-22T00:00:00Z",
                "status": "draft",
            }
        ]

    monkeypatch.setattr(fusion, "afetch_records", _fake_fetch_records)
    monkeypatch.setattr(fusion, "_resolve_tab_name", lambda _key: "Fusion")
    monkeypatch.setattr(fusion, "_sheet_id", lambda: "sheet-id")

    rows = asyncio.run(fusion._load_fusions())

    assert len(rows) == 1
    assert rows[0].needed == 400
    assert rows[0].available == 450
    assert rows[0].start_at_utc == dt.datetime(2026, 4, 8, tzinfo=dt.timezone.utc)
