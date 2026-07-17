from __future__ import annotations

import asyncio
import time

from shared.sheets import recruitment


def test_aget_clan_by_tag_uses_warm_index_without_fetch(monkeypatch):
    row = ["Clan One", "C1CE"]

    monkeypatch.setattr(recruitment, "_CACHE_TTL", 900)
    monkeypatch.setattr(recruitment, "_CLAN_TAG_INDEX", {"C1CE": row})
    monkeypatch.setattr(recruitment, "_CLAN_TAG_INDEX_TS", time.time())

    async def fail_fetch(*args, **kwargs):
        raise AssertionError("warm tag lookup should not fetch clan rows")

    monkeypatch.setattr(recruitment, "afetch_clans", fail_fetch)

    assert asyncio.run(recruitment.aget_clan_by_tag("c1ce")) is row


def test_aget_clan_by_tag_builds_index_after_async_fetch(monkeypatch):
    row = ["Clan One", "C1CE"]

    monkeypatch.setattr(recruitment, "_CACHE_TTL", 900)
    monkeypatch.setattr(recruitment, "_CLAN_TAG_INDEX", None)
    monkeypatch.setattr(recruitment, "_CLAN_TAG_INDEX_TS", 0)
    monkeypatch.setattr(recruitment, "_CLAN_HEADER_MAP", {"clan_tag": 1})

    async def fake_fetch(*, force=False):
        assert force is False
        return [row]

    monkeypatch.setattr(recruitment, "afetch_clans", fake_fetch)

    assert asyncio.run(recruitment.aget_clan_by_tag("c1ce")) is row
    assert recruitment._CLAN_TAG_INDEX == {"C1CE": row}
