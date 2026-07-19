from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

from shared.sheets import onboarding, onboarding_sessions


def test_async_session_helpers_use_async_primitives(monkeypatch) -> None:
    session = {"thread_id": "42", "step_index": 3}
    aload = AsyncMock(return_value=session)
    aload_all = AsyncMock(return_value=[session])
    asave = AsyncMock(return_value=True)
    rows = [
        ["thread_id", "thread_name", "updated_at", "completed"],
        ["42", "W0042-player", "2026-07-19T00:00:00Z", "false"],
    ]
    monkeypatch.setattr(onboarding_sessions, "aload", aload)
    monkeypatch.setattr(onboarding_sessions, "aload_all", aload_all)
    monkeypatch.setattr(onboarding_sessions, "asave", asave)
    monkeypatch.setattr(
        onboarding_sessions, "_aload_rows", AsyncMock(return_value=rows)
    )

    async def exercise() -> None:
        assert await onboarding_sessions.aget_by_thread_id(42) == session
        assert await onboarding_sessions.aload_all() == [session]
        assert await onboarding_sessions.aupdate_existing(42, {"step_index": 4})
        assert await onboarding_sessions.aupsert_session(
            thread_id=42, thread_name="W0042-player", user_id=7
        )
        assert await onboarding_sessions.amark_completed(42)
        assert await onboarding_sessions.amissing_columns(
            {"completed", "answers_json"}
        ) == {"answers_json"}

    asyncio.run(exercise())
    assert asave.await_count == 3


def test_async_finalization_helpers_never_load_sync_config(monkeypatch) -> None:
    config = {
        "welcome_finalization_status_header": "finalization_status",
        "welcome_reservation_status_header": "reservation_status",
        "welcome_clan_update_status_header": "clan_update_status",
        "welcome_finalization_note_header": "finalization_note",
        "promo_source_clan_tag_header": "source_clan_tag",
    }

    async def lookup(key: str, default=None):
        return config.get(key.lower(), default)

    def sync_forbidden(*_args, **_kwargs):
        raise AssertionError("sync Config helper reached from event loop")

    monkeypatch.setattr(onboarding, "_aconfig_lookup", lookup)
    monkeypatch.setattr(onboarding, "_config_lookup", sync_forbidden)
    monkeypatch.setattr(onboarding, "_load_config", sync_forbidden)

    async def exercise() -> None:
        headers = await onboarding.aget_finalization_headers("welcome")
        assert headers["finalization_status"] == "finalization_status"
        assert await onboarding.aget_promo_source_clan_tag_header() == "source_clan_tag"
        state = await onboarding.aget_ticket_finalization_state(
            "welcome",
            {
                "finalization_status": "done",
                "reservation_status": "released",
                "clan_update_status": "updated",
                "finalization_note": "ok",
            },
        )
        assert state["finalization_status"] == "done"

    asyncio.run(exercise())
